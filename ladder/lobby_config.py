"""Test whether a league lobby can be preconfigured without a custom client.

The host screen's lobby settings live as plain-text Lua in
`users/<steamid>/multiplayer.lua`. If Forts reads that file AT STARTUP, an
external launcher can write the league settings before the game runs — then
no bot client has to host the lobby and no player can change anything
in-game:

    TeamsUnlocked = false   -> side switching locked (LockTeamsCheck)
    FortsUnlocked = false   -> fort/slot switching locked (LockFortsCheck)
    Password      = L"..."  -> nobody uninvited gets in
    MaxPlayers    = N       -> exact lobby size

And `users/<steamid>/lobby.dat` holds the Steam lobby ID (8 bytes, little
endian). If so, the launcher reads that file and builds the join link from
it — two plain files are the whole mechanism, with no Steam API involved.

Two assumptions this checks:

  A) Does Forts read multiplayer.lua at startup (preconfiguration works), or
     overwrite it when hosting (it does not)?
  B) Does lobby.dat get a fresh ID when HOSTING, not only when joining?

Plus a third: MaxPlayers = 9. The host screen's spinner stops at 8, but nine
clients do connect — tested in game, and a tenth is refused. So if the file
gets a 9 past the UI there is exactly one extra slot, which is the caster
seat in a 4v4. Ten is not a limit anyone can raise.

    python -m ladder.lobby_config arm       # back up + write the test config
    ... start Forts, host a lobby, stay in it ...
    python -m ladder.lobby_config check     # read back and report
    python -m ladder.lobby_config restore   # put the original back

Forts must be CLOSED during `arm`, or the game rewrites the file on exit and
the test proves nothing.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import shutil
import struct
import sys
import time

FORTS_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Forts"
APP_ID = 410900

# What a league would prescribe. Everything else in the existing file is
# left alone — the point is to measure the difference, not to rearrange the
# user's settings.
TEST_OVERRIDES = {
    "TeamsUnlocked": "false",
    "FortsUnlocked": "false",
    "MaxPlayers": "9",                       # UI allows 8 -> the actual test
    "Password": 'L"leaguetest"',
    "ServerName": 'L"[LEAGUETEST] please ignore"',
}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "lobby_config_test_state.json")


# -------------------------------------------------------------------- Helpers

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def forts_running() -> bool:
    """Process list via the Windows API rather than `tasklist`.

    On a localised Windows, tasklist emits OEM-codepage bytes while Python
    decodes subprocess output as cp1252. That raises in the reader thread
    ('charmap' codec can't decode byte 0x81) — somewhere a try/except here
    would never see. Toolhelp32 returns UTF-16 and is codepage-independent.
    """
    TH32CS_SNAPPROCESS = 0x2
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(_PROCESSENTRY32W)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        # Better not to claim the game is closed — otherwise we write into a
        # file Forts will overwrite again on exit.
        print("WARNING: process list unreadable, skipping the Forts check.")
        return False
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile.lower() == "forts.exe":
                return True
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                return False
    finally:
        k32.CloseHandle(snap)


def user_dirs() -> list[str]:
    root = os.path.join(FORTS_DIR, "users")
    if not os.path.isdir(root):
        sys.exit(f"users directory not found: {root}")
    return [os.path.join(root, d) for d in os.listdir(root)
            if re.fullmatch(r"\d{17}", d)]


def pick_user(explicit: str | None) -> str:
    dirs = user_dirs()
    if explicit:
        for d in dirs:
            if os.path.basename(d) == explicit:
                return d
        sys.exit(f"SteamID {explicit} not found under users/")
    # The active account is the one with the most recent log.txt.
    def logtime(d: str) -> float:
        p = os.path.join(d, "log.txt")
        return os.path.getmtime(p) if os.path.exists(p) else 0.0
    dirs.sort(key=logtime, reverse=True)
    if not dirs:
        sys.exit("no user directory found")
    return dirs[0]


def parse_multiplayer(path: str) -> tuple[dict[str, str], list[str]]:
    """Read `data = { Key = value, ... }`, keeping values as RAW TEXT.

    Deliberately not a Lua parser: `L"..."` is LuaPlus syntax for wide
    strings, and the file has to be written back looking byte-for-byte like
    it did, or a failure could just as well be about the format as the
    content.
    """
    text = open(path, "r", encoding="utf-8", errors="replace").read()
    keys: dict[str, str] = {}
    order: list[str] = []
    for m in re.finditer(r"^\t(\w+)\s*=\s*(.*?),\s*$", text, re.M):
        keys[m.group(1)] = m.group(2)
        order.append(m.group(1))
    return keys, order


def render_multiplayer(keys: dict[str, str], order: list[str]) -> str:
    lines = ["data = ", "{"]
    for k in order:
        lines.append(f"\t{k} = {keys[k]},")
    lines.append("}")
    # The game writes three blank lines at the end — match that.
    return "\n".join(lines) + "\n\n\n\n"


def decode_lobby_dat(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    raw = open(path, "rb").read()
    if len(raw) < 8:
        return {"raw": raw.hex(), "note": "too short for a SteamID"}
    lid = struct.unpack("<Q", raw[:8])[0]
    return {
        "lobby_id": lid,
        "hex": hex(lid),
        "universe": (lid >> 56) & 0xFF,
        "account_type": (lid >> 52) & 0xF,     # 8 = chat -> Steam lobby
        "instance": (lid >> 32) & 0xFFFFF,
        "account_id": lid & 0xFFFFFFFF,
        "tail": raw[8:].hex(),
        "mtime": os.path.getmtime(path),
    }


def log_tail(path: str, patterns: list[str], limit: int = 40) -> list[str]:
    if not os.path.exists(path):
        return []
    text = open(path, "rb").read().decode("utf-16-le", errors="replace")
    hits = [l.strip() for l in text.splitlines()
            if any(p in l for p in patterns)]
    return hits[-limit:]


LOG_PATTERNS = [
    "Hosting new lobby", "Connected to existing lobby", "Connecting to lobby",
    "Valid Steam lobby ID", "Filter Lobby Name", "adjusting extra lobby slots",
    "Rejecting lobby", "Ignoring lobby", "FailedToCreateLobby",
    "host is dedicated", "LoginResult", "Setting lobby",
]


# ------------------------------------------------------------------ Commands

def cmd_arm(args) -> int:
    if forts_running():
        print("ABORTED: Forts is running. The game rewrites multiplayer.lua")
        print("on exit -- the test would prove nothing.")
        print("Close Forts, then run `arm` again.")
        return 2

    udir = pick_user(args.user)
    steamid = os.path.basename(udir)
    mp = os.path.join(udir, "multiplayer.lua")
    print(f"User: {steamid}")

    if not os.path.exists(mp):
        print(f"NOTE: {mp} does not exist yet.")
        print("That is a result in itself: the file only appears after the")
        print("first hosted lobby, so a launcher could overwrite it but not")
        print("create it -- unless the game accepts one we wrote. Which is")
        print("exactly what this tries.")
        keys, order = {}, []
        for k in ("ArtificialHostLag", "CoopOnElimination", "FortsUnlocked",
                  "LobbyType", "MaxPlayers", "Password", "PlayTimeType",
                  "ServerName", "ShowCursors", "TeamsUnlocked"):
            order.append(k)
            keys[k] = {"ArtificialHostLag": "false", "CoopOnElimination": "false",
                       "LobbyType": "0", "PlayTimeType": "0",
                       "ShowCursors": "false"}.get(k, "false")
    else:
        keys, order = parse_multiplayer(mp)
        print("before:")
        for k in order:
            print(f"   {k} = {keys[k]}")

    backups = {}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for name in ("multiplayer.lua", "lobby.dat", "favourites.lua"):
        src = os.path.join(udir, name)
        if os.path.exists(src):
            dst = src + f".backup_{stamp}"
            shutil.copy2(src, dst)
            backups[name] = dst
    print(f"backed up: {list(backups)}")

    for k, v in TEST_OVERRIDES.items():
        keys[k] = v
        if k not in order:
            order.append(k)
    order.sort()  # the game writes them alphabetically

    open(mp, "w", encoding="utf-8", newline="\n").write(
        render_multiplayer(keys, order))
    print("\nafter (written):")
    for k in order:
        mark = "  <== test" if k in TEST_OVERRIDES else ""
        print(f"   {k} = {keys[k]}{mark}")

    before_lobby = decode_lobby_dat(os.path.join(udir, "lobby.dat"))
    state = {
        "steamid": steamid,
        "user_dir": udir,
        "armed_at": time.time(),
        "backups": backups,
        "written": {k: keys[k] for k in TEST_OVERRIDES},
        "multiplayer_mtime": os.path.getmtime(mp),
        "lobby_dat_before": before_lobby,
    }
    json.dump(state, open(STATE_FILE, "w"), indent=2)

    print("\n--- over to you ---")
    print("1. Start Forts.")
    print('2. Open Multiplayer -> "Host New Game". Click NOTHING, just look:')
    print("   - are Lock Teams AND Lock Forts both ticked?")
    print("   - does the server name read [LEAGUETEST] please ignore?")
    print("   - is there a password?")
    print("   - what does MaxPlayers show: 9, or reset to 8?")
    print("3. Pick a map, actually host the lobby, and stay in it.")
    print("4. Back here: python -m ladder.lobby_config check")
    return 0


def cmd_check(args) -> int:
    if not os.path.exists(STATE_FILE):
        return cmd_report(args)      # never armed: just take stock
    state = json.load(open(STATE_FILE))
    udir = state["user_dir"]
    steamid = state["steamid"]
    mp = os.path.join(udir, "multiplayer.lua")

    print(f"User: {steamid}")
    print(f"armed: {time.strftime('%H:%M:%S', time.localtime(state['armed_at']))}")

    print("\n=== A) did Forts take our values? ===")
    if not os.path.exists(mp):
        print("multiplayer.lua is gone -- the game deleted or replaced it.")
        return 1
    keys, order = parse_multiplayer(mp)
    changed_by_game = os.path.getmtime(mp) > state["multiplayer_mtime"] + 0.5
    print(f"rewritten by the game: {'YES' if changed_by_game else 'no'}")
    verdict_kept = True
    for k, want in state["written"].items():
        have = keys.get(k, "<missing>")
        ok = (have == want)
        verdict_kept &= ok
        print(f"   {k}: wrote {want!r} -> now {have!r} "
              f"{'OK' if ok else 'DIVERGED'}")
    if verdict_kept and changed_by_game:
        print("=> the file is read AND our values survive hosting.")
    elif verdict_kept:
        print("=> values unchanged. Whether they reached the host screen is "
              "only answered by looking at the checkboxes (step 2).")
    else:
        print("=> the game corrected at least one value. For MaxPlayers that "
              "means the 9 does not hold and the limit stays 8.")

    print("\n=== B) lobby.dat ===")
    before = state.get("lobby_dat_before")
    after = decode_lobby_dat(os.path.join(udir, "lobby.dat"))
    if after is None:
        print("lobby.dat does not exist -> hosting does NOT write it.")
        print("The lobby ID then has to come from elsewhere (log, rich "
              "presence, Steam API).")
    else:
        fresh = (before is None
                 or before.get("lobby_id") != after.get("lobby_id")
                 or after["mtime"] > state["armed_at"])
        print(f"   ID       : {after['lobby_id']}  ({after['hex']})")
        print(f"   universe : {after['universe']} (expected 1)")
        print(f"   type     : {after['account_type']} (8 = chat/lobby)")
        print(f"   instance : {hex(after['instance'])}")
        print(f"   trailing : {after['tail']}")
        print(f"   fresh    : {'YES' if fresh else 'no (stale value)'}")
        if after["account_type"] == 8:
            print("\n   Join link for the launcher:")
            print(f"   steam://joinlobby/{APP_ID}/{after['lobby_id']}/{steamid}")
            print(f"   Launch option:  Forts.exe +connect_lobby {after['lobby_id']}")

    print("\n=== C) what the log says about it ===")
    for line in log_tail(os.path.join(udir, "log.txt"), LOG_PATTERNS):
        print("   ", line[:190])
    return 0


def cmd_report(args) -> int:
    """Take stock without a prior `arm` — read-only."""
    udir = pick_user(args.user)
    steamid = os.path.basename(udir)
    print(f"User: {steamid}\n")
    mp = os.path.join(udir, "multiplayer.lua")
    if os.path.exists(mp):
        keys, order = parse_multiplayer(mp)
        print("multiplayer.lua:")
        for k in order:
            print(f"   {k} = {keys[k]}")
    else:
        print("multiplayer.lua: does not exist")
    fav = os.path.join(udir, "favourites.lua")
    if os.path.exists(fav):
        print("\nfavourites.lua:")
        print("   " + open(fav, encoding="utf-8", errors="replace").read().strip())
    print("\nlobby.dat:", json.dumps(decode_lobby_dat(
        os.path.join(udir, "lobby.dat")), indent=2))
    print("\nLog:")
    for line in log_tail(os.path.join(udir, "log.txt"), LOG_PATTERNS, 25):
        print("   ", line[:190])
    return 0


def cmd_restore(args) -> int:
    if not os.path.exists(STATE_FILE):
        sys.exit("no state saved -- nothing to restore")
    if forts_running():
        print("WARNING: Forts is running and will overwrite on exit. "
              "Better to close it first.")
    state = json.load(open(STATE_FILE))
    for name, backup in state["backups"].items():
        dst = os.path.join(state["user_dir"], name)
        if os.path.exists(backup):
            shutil.copy2(backup, dst)
            print(f"restored: {name}")
    os.remove(STATE_FILE)
    print("done. The .backup_* files are left in place in case you still "
          "want to compare them.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["arm", "check", "report", "restore"])
    ap.add_argument("--user",
                    help="SteamID64 of the account (default: newest log.txt)")
    args = ap.parse_args()
    return {"arm": cmd_arm, "check": cmd_check,
            "report": cmd_report, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
