"""Record Forts matches from the game log.

The official leaderboard only covers the ranked queue. Anything played in a
custom lobby leaves no trace, which is why the scene still keeps its rating
by hand. This reads those matches out of `users/<steamid>/log.txt` and turns
them into JSON: map, mode, roster with Steam IDs and sides, both commanders,
who lost and when, replay file, lobby ID.

That log file is the whole input. Opening it for reading is the only thing
this module does to the game's directory.

**It has to run while you play.** Forts clears log.txt on every start, so a
match not read during the session is gone afterwards. Only what the game
copied itself (the logs under `desyncs/`) can be recovered later.

There is no "winner" line in the log — that only goes to the console.
Instead every loser is named individually:

    7:23 SomePlayer has been defeated!

Together with the roster that gives the winner: the side still standing.
Cases that cannot be derived are marked `unclear` rather than guessed — a
wrongly rated game costs more than a missing one.

    python -m ladder.recorder --watch            # follow live
    python -m ladder.recorder --from-file <log>  # parse an old log
    python -m ladder.recorder --backfill         # all desyncs/ copies
    python -m ladder.recorder --setup            # who owns this machine
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from .eligibility import Eligibility
from .identity import ensure_local_identity
from .paths import find_forts_dir, forts_dir_or_die

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "recorded"

# Line formats, all verified against real logs (1v1 multiplayer, 3v3, six-
# player lobby, skirmish vs the built-in AI). Order does not matter: the
# parser is stateful, not positional.
RE_STEAM_LOGIN = re.compile(r"Logged into Steam as (.+?) \((\d{17})\)")
RE_MODE = re.compile(r"^\s*Game mode: (.+?)\s*$")
RE_MAP = re.compile(r"^\s*Loading map (.+?)\s*$")
RE_MULTISTART = re.compile(r"OnMultiStart host (\d+), players (\d+)")
# 0: name, Id 1, Team 101, PlayerPlaying, join at 0, (Steam), SteamID 765..., , Local 1, ping 0.074
RE_ROSTER = re.compile(
    r"^\s*(\d+): (.+?), Id (\d+), Team (\d+), (\w+), join at \d+, "
    r"\((\w+)\), SteamID (\d{17}),.*?Local (\d), ping ([\d.]+)")
RE_CLIENT_SIDE = re.compile(r"^\s*Client (.+?), id (\d+), side (-?\d+), fortId (-?\d+)")
RE_CLIENT_CONN = re.compile(r"Client Connected: (.+?), index (\d+), id (\d+), side (-?\d+)")
RE_FORT_SELECT = re.compile(
    r"Fort Select: Client (.+?) added - fort (\d+) on side (\d+) "
    r"\(Allowed: (\d+), Type: (\d+), IsHost: (\d+)\)")
RE_COMMANDER = re.compile(r"^\s*Team(\d) commander: (\S+)")
RE_DEFEAT = re.compile(r"^\s*(\d+):(\d\d) (.+?) has been defeated!")
RE_REPLAY = re.compile(r"Replay saved as (\S+)")
RE_LOBBY = re.compile(r"Setting lobby (\d+) game server (\d+)")
RE_VERSION = re.compile(r"Forts (?:version )?v?(\d+\.\d+\.\d+(?:\.\d+)?)")

MATCH_END = "World::Execute mDone detected"


def side_of(team: int) -> int:
    """Forts does not number teams 1/2: side = teamId % 100.

    The lobby reports 1/2, the running game 101/102. Both must map to the
    same side or the same player is counted twice.
    """
    return team % 100 if team >= 100 else team


class Match:
    """A match under construction, finished on mDone or the replay line."""

    def __init__(self, started_at: float, fallback_time: float | None = None) -> None:
        self.started_at = started_at
        self.fallback_time = fallback_time
        self.map: str | None = None
        self.mode: str | None = None
        self.is_host: bool | None = None
        self.player_count: int | None = None
        self.lobby_id: int | None = None
        self.replay: str | None = None
        self.commanders: dict[int, str] = {}
        self.players: dict[str, dict] = {}      # name -> data
        self.defeats: list[dict] = []
        self.fort_select: list[dict] = []
        self.ended_at: float | None = None
        #: Set when the game reported the match over. Only the replay line is
        #: still accepted afterwards.
        self.closed = False

    # ----------------------------------------------------------------- Setup
    def note_roster(self, m: re.Match) -> None:
        name = m.group(2)
        team = int(m.group(4))
        p = self.players.setdefault(name, {"name": name})
        p["steam_id"] = m.group(7)
        p["client_id"] = int(m.group(3))
        p["side"] = side_of(team)
        p["team_raw"] = team
        p["local"] = m.group(8) == "1"
        p["ping"] = float(m.group(9))
        p["connection"] = m.group(6)

    def note_side(self, name: str, side: int, fort_id: int | None = None) -> None:
        p = self.players.setdefault(name, {"name": name})
        # Never overwrite a real side (>0) with the -1 placeholder.
        if side > 0:
            p["side"] = side
        if fort_id is not None and fort_id >= 0:
            p["fort_id"] = fort_id

    # ---------------------------------------------------------------- Parsing
    def outcome(self) -> dict:
        """Derive the winner from roster and defeats. Never guess."""
        defeated = {d["name"] for d in self.defeats}
        sides: dict[int, list[str]] = {}
        for name, p in self.players.items():
            s = p.get("side")
            if s and s > 0:
                sides.setdefault(s, []).append(name)

        alive = {s: [n for n in names if n not in defeated]
                 for s, names in sides.items()}
        surviving = [s for s, names in alive.items() if names]

        # Clean case: exactly one side still has players standing.
        if len(sides) >= 2 and len(surviving) == 1:
            return {"status": "decided", "winner_side": surviving[0],
                    "loser_sides": [s for s in sides if s != surviving[0]],
                    "basis": "defeat-lines"}

        # One side only means the opponent was the built-in AI, which has no
        # client. Irrelevant for a ladder, but named rather than treated as
        # an error.
        if len(sides) == 1:
            only = next(iter(sides))
            human_lost = all(n in defeated for n in sides[only])
            return {"status": "vs_ai",
                    "human_side": only,
                    "human_result": "loss" if human_lost else "win",
                    "basis": "defeat-lines, single-sided roster"}

        return {"status": "unclear",
                "reason": ("no defeat logged" if not defeated else
                           "more than one side has survivors"),
                "sides": sides, "defeated": sorted(defeated)}

    def duration_s(self) -> float | None:
        """Match time from the last defeat timestamp (MM:SS in the log).

        More reliable than wall clock: it comes from the simulation and is
        identical on both clients, which makes it usable for cross-checking
        two reports.
        """
        if self.defeats:
            return max(d["at_s"] for d in self.defeats)
        if self.ended_at:
            return round(self.ended_at - self.started_at, 1)
        return None

    def is_interesting(self) -> bool:
        """Skip empty fragments (menu changes, aborted loads)."""
        return bool(self.map and (self.players or self.defeats))

    def when(self) -> float:
        """Real match time, taken from the replay filename where possible.

        The log has no wall clock on its lines, but the replay name carries
        one (`v1.38.2_Vanilla_20260719_135021.fwr`). Without it every match
        parsed after the fact would be dated "today".
        """
        if self.replay:
            m = re.search(r"_(\d{8})_(\d{6})", self.replay)
            if m:
                try:
                    return time.mktime(time.strptime(m.group(1) + m.group(2),
                                                     "%Y%m%d%H%M%S"))
                except ValueError:
                    pass
        return self.fallback_time or self.started_at

    def key(self) -> str:
        """Stable identity of a match, whatever log it came from.

        Needed because every desync copy contains the whole session log so
        far: the same match appears in several files and would otherwise be
        counted more than once. The replay name is the best key; without it
        map, roster and defeats identify the game well enough.
        """
        if self.replay:
            return f"replay:{Path(self.replay).name}"
        ids = sorted(p.get("steam_id", p["name"]) for p in self.players.values())
        d = ",".join(f"{x['name']}@{x['at_s']}" for x in self.defeats)
        return f"{self.map}|{'+'.join(ids)}|{d}|{self.duration_s()}"

    def to_dict(self) -> dict:
        return {
            "map": self.map,
            "mode": self.mode,
            "hosted_locally": self.is_host,
            "lobby_id": self.lobby_id,
            "player_count": self.player_count,
            "commanders": {f"side{k}": v for k, v in sorted(self.commanders.items())},
            "players": sorted(self.players.values(),
                              key=lambda p: (p.get("side", 99), p["name"])),
            "defeats": self.defeats,
            "fort_select": self.fort_select,
            "duration_s": self.duration_s(),
            "outcome": self.outcome(),
            "replay": self.replay,
            "match_key": self.key(),
            "played_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(self.when())),
        }


class Parser:
    """State machine over the log lines, emitting finished matches."""

    def __init__(self, fallback_time: float | None = None,
                 elig: "Eligibility | None" = None) -> None:
        self.cur: Match | None = None
        self.local_name: str | None = None
        self.local_steam_id: str | None = None
        self.lobby_id: int | None = None
        # `Game mode:` is logged BEFORE `Loading map`, and `Loading map` is
        # what starts a new match — so writing the mode onto `cur` loses it
        # every time (it landed on the previous match or was dropped, which
        # left it empty in 14 of 15 recorded games). Sticky parser state,
        # stamped in `_begin`, the same way lobby_id already works.
        self.mode: str | None = None
        self.fallback_time = fallback_time
        self.done: list[Match] = []
        #: Optional. When the client armed a ladder lobby, the id it gets is
        #: only visible here — this is the one place the log and that
        #: declaration meet. Left out when parsing archived logs, or an old
        #: session would sanction lobbies after the fact.
        self.elig = elig
        self.sanctioned: list[int] = []

    def _begin(self) -> Match:
        self.cur = Match(time.time(), self.fallback_time)
        self.cur.lobby_id = self.lobby_id
        self.cur.mode = self.mode
        return self.cur

    def _finish(self) -> None:
        if self.cur is None:
            return
        self.cur.ended_at = time.time()
        if self.cur.is_interesting():
            self.done.append(self.cur)
        self.cur = None

    def feed(self, line: str) -> None:
        m = RE_STEAM_LOGIN.search(line)
        if m:
            self.local_name, self.local_steam_id = m.group(1), m.group(2)
            return

        m = RE_LOBBY.search(line)
        if m:
            self.lobby_id = int(m.group(1))
            if self.cur:
                self.cur.lobby_id = self.lobby_id
            if self.elig is not None and self.elig.observe_lobby(self.lobby_id):
                self.sanctioned.append(self.lobby_id)
            return

        # Handled before the "no match open" guard below, because this line
        # arrives while none is open yet.
        m = RE_MODE.match(line)
        if m:
            self.mode = m.group(1)
            if self.cur:
                self.cur.mode = self.mode
            return

        # `Loading map` is the most reliable start marker: `Game mode` also
        # fires while browsing the map menu.
        m = RE_MAP.match(line)
        if m:
            self._finish()
            cur = self._begin()
            raw = m.group(1)
            cur.map = Path(raw.replace("\\", "/")).stem
            cur.map_path = raw
            return

        if self.cur is None:
            # Roster lines can arrive before `Loading map` during the lobby
            # phase. Start a match and fill in the map later.
            if RE_ROSTER.match(line) or RE_FORT_SELECT.search(line):
                self._begin()
            else:
                return

        cur = self.cur
        assert cur is not None

        if cur.closed and not RE_REPLAY.search(line):
            # Match is over; the only thing still expected is its replay name.
            return

        m = RE_MULTISTART.search(line)
        if m:
            cur.is_host = m.group(1) == "1"
            cur.player_count = int(m.group(2))
            return
        m = RE_ROSTER.match(line)
        if m:
            cur.note_roster(m)
            return
        m = RE_CLIENT_SIDE.match(line)
        if m:
            cur.note_side(m.group(1), int(m.group(3)), int(m.group(4)))
            return
        m = RE_CLIENT_CONN.search(line)
        if m:
            cur.note_side(m.group(1), int(m.group(4)))
            return
        m = RE_FORT_SELECT.search(line)
        if m:
            cur.fort_select.append({
                "name": m.group(1), "fort": int(m.group(2)),
                "side": int(m.group(3)), "allowed": m.group(4) == "1",
                "type": int(m.group(5)), "is_host": m.group(6) == "1"})
            cur.note_side(m.group(1), int(m.group(3)))
            return
        m = RE_COMMANDER.match(line)
        if m:
            cur.commanders[int(m.group(1))] = m.group(2)
            return
        m = RE_DEFEAT.match(line)
        if m:
            at = int(m.group(1)) * 60 + int(m.group(2))
            cur.defeats.append({"name": m.group(3), "at_s": at,
                                "at": f"{m.group(1)}:{m.group(2)}"})
            return
        m = RE_REPLAY.search(line)
        if m:
            cur.replay = m.group(1)
            # The replay line is the real end: everything worth recording has
            # arrived by now.
            self._finish()
            return
        if MATCH_END in line:
            # NOT the end of parsing. `Replay saved as` follows about ten lines
            # later, and finishing here dropped it every single time — which
            # also cost the timestamp, since the replay filename is the only
            # wall clock in the log. Mark it closed instead: from now on only
            # the replay line is accepted, so lobby chatter from the next match
            # cannot attach itself to this one.
            cur.ended_at = time.time()
            cur.closed = True

    def flush(self) -> None:
        self._finish()


# ----------------------------------------------------------------- File I/O

def read_log_lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    enc = "utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-8"
    return raw.decode(enc, errors="replace").splitlines()


def emit(match: Match, out_dir: Path, quiet: bool = False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = match.to_dict()
    stamp = d["played_at"].replace(":", "").replace("-", "").replace("T", "_")
    safe_map = re.sub(r"[^\w.-]+", "_", d["map"] or "unknown")
    # Timestamp plus map is not enough: without a replay name several games
    # date to the same log file and the second silently overwrote the first
    # (that is how a decided match went missing). The key hash makes the name
    # unique while staying the same on a re-run, so this remains idempotent.
    tag = hashlib.sha1(d["match_key"].encode("utf-8")).hexdigest()[:8]
    path = out_dir / f"{stamp}_{safe_map}_{tag}.json"
    path.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    if not quiet:
        print(summary(d))
        print(f"   -> {path.name}")
    return path


def summary(d: dict) -> str:
    out = d["outcome"]
    who = ", ".join(
        f"{p['name']}(S{p.get('side','?')}"
        + (f"/{p['steam_id'][-4:]}" if p.get("steam_id") else "") + ")"
        for p in d["players"]) or "-"
    dur = f"{d['duration_s']:.0f}s" if d.get("duration_s") else "?"
    if out["status"] == "decided":
        res = f"side {out['winner_side']} wins"
    elif out["status"] == "vs_ai":
        res = f"gegen KI ({out['human_result']})"
    else:
        res = f"UNKLAR ({out['reason']})"
    host = {True: "host", False: "gast", None: "?"}[d.get("hosted_locally")]
    return (f"{d['played_at']}  {d['map']}  [{d.get('mode') or '?'}]  {dur}  "
            f"{host}\n   {who}\n   {res}"
            + (f"\n   replay: {d['replay']}" if d.get("replay") else ""))


def parse_file(path: Path, out_dir: Path | None, quiet: bool = False,
               seen: set[str] | None = None) -> list[dict]:
    # Without a replay name the match is dated to the log file rather than
    # to "now" — the best available anchor when parsing after the fact.
    p = Parser(fallback_time=path.stat().st_mtime)
    for line in read_log_lines(path):
        p.feed(line)
    p.flush()
    results = []
    for m in p.done:
        if seen is not None:
            k = m.key()
            if k in seen:
                continue
            seen.add(k)
        d = m.to_dict()
        results.append(d)
        if out_dir:
            emit(m, out_dir, quiet)
        elif not quiet:
            print(summary(d), "\n")
    return results


def cmd_from_file(args) -> int:
    path = Path(args.from_file)
    if not path.exists():
        sys.exit(f"not found: {path}")
    res = parse_file(path, Path(args.out) if args.out else None)
    print(f"\n{len(res)} Match(es) aus {path.name}")
    stat = {}
    for d in res:
        stat[d["outcome"]["status"]] = stat.get(d["outcome"]["status"], 0) + 1
    print("Status:", stat)
    return 0


def cmd_backfill(args) -> int:
    """Parse everything the game copied itself (desyncs/)."""
    forts = forts_dir_or_die()
    logs = sorted(forts.glob("users/*/desyncs/*/*.txt"))
    logs = [p for p in logs if "log" in p.name.lower()
            and "world-dump" not in p.name and "checksum" not in p.name]
    print(f"{len(logs)} kopierte Logs gefunden\n")
    # Each desync copy holds the whole session log so far, so the same game
    # appears in several files: deduplicate across files, not per file.
    seen: set[str] = set()
    total, stat, dupes = 0, {}, 0
    for p in logs:
        before = len(seen)
        # Defaults to the normal output directory. Passing None here meant
        # --backfill printed "N new" for every log and wrote nothing at all,
        # which is worse than doing nothing: it looks like it worked.
        res = parse_file(p, Path(args.out) if args.out else OUT_DIR, quiet=True,
                         seen=seen)
        for d in res:
            stat[d["outcome"]["status"]] = stat.get(d["outcome"]["status"], 0) + 1
        if res:
            print(f"{p.parent.name[:44]:44s} {len(res):2d} new")
        elif len(seen) == before:
            dupes += 1
        total += len(res)
    print(f"\n{total} distinct match(es) written, status: {stat}")
    print(f"{dupes} logs held only games that were already known")
    return 0


def cmd_watch(args) -> int:
    """Follow the log live, surviving a game restart (log.txt is cleared)."""
    out_dir = Path(args.out) if args.out else OUT_DIR
    forts = forts_dir_or_die()
    # Live only. The eligibility is passed here and NOT to the archive
    # parsers: an armed declaration must apply to the lobby being hosted
    # now, never to lobby ids replayed out of an old log.
    elig = Eligibility.load()
    print(f"watching {forts / 'users'}/*/log.txt")
    print(f"output:   {out_dir}")
    if elig.armed is not None:
        print(f"armed:    next lobby counts for the ladder"
              + (f" (series {elig.armed.series_id})" if elig.armed.series_id else ""))
    print("Ctrl+C stops.\n")

    parser = Parser(elig=elig)
    cur_path: Path | None = None
    offset = 0
    pending = b""

    while True:
        logs = list(forts.glob("users/*/log.txt"))
        if not logs:
            time.sleep(2.0)
            continue
        # The active account is the one with the most recent log.
        path = max(logs, key=lambda p: p.stat().st_mtime)
        try:
            fh = path.open("rb")
        except OSError:
            time.sleep(1.0)
            continue
        with fh:
            size = os.fstat(fh.fileno()).st_size
            if path != cur_path:
                print(f"[{time.strftime('%H:%M:%S')}] Log: {path.parent.name}")
                cur_path, offset, pending = path, 0, b""
                parser = Parser(elig=elig)
            if size < offset:
                # File was cleared: a new session started.
                print(f"[{time.strftime('%H:%M:%S')}] log cleared, new session")
                parser.flush()
                for m in parser.done:
                    emit(m, out_dir)
                parser = Parser(elig=elig)
                offset, pending = 0, b""
            if size > offset:
                fh.seek(offset)
                chunk = fh.read(size - offset)
                offset = size
                data = pending + chunk
                # UTF-16 only decodes on an even byte count, and the last
                # line may be cut off mid-write.
                if len(data) % 2:
                    pending, data = data[-1:], data[:-1]
                else:
                    pending = b""
                text = data.decode("utf-16-le", errors="replace")
                if not text.endswith("\n"):
                    text, _, rest = text.rpartition("\n")
                    pending = rest.encode("utf-16-le") + pending
                for line in text.splitlines():
                    line = line.lstrip("\ufeff")
                    before = len(parser.done)
                    parser.feed(line)
                    if parser.sanctioned:
                        # Persist immediately: the id is only in this line, and
                        # a crash before the match ends would lose it.
                        for lid in parser.sanctioned:
                            print(f"[{time.strftime('%H:%M:%S')}] lobby {lid} "
                                  f"counts for the ladder")
                        parser.sanctioned.clear()
                        elig.save()
                    if len(parser.done) > before:
                        for m in parser.done[before:]:
                            emit(m, out_dir)
        time.sleep(args.interval)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--watch", action="store_true", help="record live")
    g.add_argument("--from-file", help="einzelnes Log auswerten")
    g.add_argument("--backfill", action="store_true",
                   help="parse every log Forts copied into desyncs/")
    g.add_argument("--setup", action="store_true",
                   help="run only the first-run dialog")
    ap.add_argument("--out", help="output directory (default: data/recorded)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="polling interval in seconds (--watch)")
    ap.add_argument("--no-setup", action="store_true",
                    help="skip the first-run dialog (for services)")
    args = ap.parse_args()

    if args.setup:
        ensure_local_identity(force=True)
        return 0
    if args.watch:
        try:
            return cmd_watch(args)
        except KeyboardInterrupt:
            print("\nbeendet")
            return 0
    if args.from_file:
        return cmd_from_file(args)
    return cmd_backfill(args)


if __name__ == "__main__":
    sys.exit(main())
