"""Turn recorded games into a finished league report.

The rules ask for a message in a Discord channel after every duel or brawl,
in a fixed shape, with the replays attached. Doing that by hand means
recalling the score and finding the right files among hundreds named by map
and timestamp. The recorder already knows all of it.

A *series* here is consecutive games with the same set of players (by Steam
ID, not by name) without a long break. That is a heuristic and treated as
one: `--gap` adjusts the grouping, and `show` lists every game so a badly
cut series is visible before it gets reported.

Nothing is reported automatically. This prepares the message and the
replays; a human sends them after looking.

    python -m ladder.report list
    python -m ladder.report show 1
    python -m ladder.report collect 1 --out ./report
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .eligibility import Eligibility
from .identity import Registry
from .paths import active_user_dir, user_dirs
from .rules import check_series, report_line

REPO = Path(__file__).resolve().parent.parent
RECORDED = REPO / "data" / "recorded"

# Largest gap inside a series. Three hours is generous: a Bo9 with breaks
# and restarts fits, a new evening does not.
DEFAULT_GAP_S = 3 * 3600


def _own_steam_id() -> str | None:
    """Steam ID of the account this machine belongs to."""
    d = active_user_dir()
    return d.name if d else None


@dataclass
class Series:
    matches: list[dict] = field(default_factory=list)

    # -------------------------------------------------------------- Properties
    @property
    def steam_ids(self) -> tuple[str, ...]:
        ids = {p["steam_id"] for m in self.matches
               for p in m.get("players", []) if p.get("steam_id")}
        return tuple(sorted(ids))

    @property
    def played_at(self) -> str:
        return min(m.get("played_at", "") for m in self.matches)

    @property
    def maps(self) -> list[str]:
        return [m.get("map") or "?" for m in self.matches]

    @property
    def is_team(self) -> bool:
        return len(self.steam_ids) > 2

    def sides(self) -> dict[int, list[dict]]:
        """Players per side, collected across the whole series.

        Across all games because a single one can be missing a player —
        roster lines are sometimes absent during loading.
        """
        out: dict[int, dict[str, dict]] = {}
        for m in self.matches:
            for p in m.get("players", []):
                s = p.get("side")
                if s and s > 0 and p.get("steam_id"):
                    out.setdefault(s, {})[p["steam_id"]] = p
        return {s: list(v.values()) for s, v in sorted(out.items())}

    def score(self) -> tuple[dict[int, int], int]:
        """(wins per side, number of undecided games)."""
        wins: dict[int, int] = {}
        unclear = 0
        for m in self.matches:
            out = m.get("outcome") or {}
            if out.get("status") == "decided":
                w = out["winner_side"]
                wins[w] = wins.get(w, 0) + 1
            else:
                unclear += 1
        for s in self.sides():
            wins.setdefault(s, 0)
        return wins, unclear

    def local_side(self, my_steam_id: str | None = None) -> int | None:
        """Which side is "mine"?

        Not decided by the `local` flag where avoidable: logs parsed after
        the fact sometimes come from the *opponent* (the game stores copies
        of both clients on a desync) and mark them as local. That produced
        "vs <your own name>" with the score the wrong way round.
        """
        my_steam_id = my_steam_id or _own_steam_id()
        if my_steam_id:
            for m in self.matches:
                for p in m.get("players", []):
                    if p.get("steam_id") == my_steam_id and p.get("side"):
                        return p["side"]
        for m in self.matches:
            for p in m.get("players", []):
                if p.get("local") and p.get("side"):
                    return p["side"]
        return None

    # ------------------------------------------------------------- Rendering
    def names(self, reg: Registry, side: int) -> list[str]:
        """Ladder names where known, Steam names otherwise — never blank."""
        out = []
        for p in self.sides().get(side, []):
            out.append(reg.ufer_name_for(p["steam_id"]) or p.get("name") or "?")
        return sorted(out)

    def report(self, reg: Registry,
               elig: Eligibility | None = None) -> tuple[str, list[str]]:
        """(report line, warnings). Warnings are never silent.

        Passing `elig` turns this into the gated path: no line is produced
        for a series that does not count, so an opponent who never opted in
        cannot end up in a message. `list` and `show` leave it off — reading
        back your own logs is not publishing anything.
        """
        warnings: list[str] = []
        sides = self.sides()
        if len(sides) != 2:
            warnings.append(f"{len(sides)} sides detected — not a clean series")
            return "", warnings

        if elig is not None:
            verdict = elig.check_series(self.matches)
            if not verdict:
                return "", warnings + verdict.reasons

        own_side = self.local_side() or min(sides)
        other = next(s for s in sides if s != own_side)
        wins, unclear = self.score()
        if unclear:
            warnings.append(
                f"{unclear} game(s) without a usable result — the score "
                "below is incomplete")

        kind = "brawl" if self.is_team else "duel"
        line = report_line(kind, self.names(reg, own_side),
                           self.names(reg, other),
                           (wins.get(own_side, 0), wins.get(other, 0)))

        lobbies = {m.get("lobby_id") for m in self.matches if m.get("lobby_id")}
        if len(lobbies) > 1:
            # Not an error, but worth surfacing: the series ran in several
            # lobbies, so the host probably changed.
            warnings.append(
                f"{len(lobbies)} lobbies — probably a host change or crash; "
                "please check this really was one series")

        chk = check_series(self.matches, brawl=self.is_team)
        warnings.extend(f"{v.rule}: {v.detail}" for v in chk.violations)
        missing = [p.get("name") for s in sides for p in sides[s]
                   if not reg.ufer_name_for(p.get("steam_id", ""))]
        if missing:
            warnings.append(
                "no ladder name linked, Steam name used: "
                + ", ".join(sorted(set(m for m in missing if m))))
        return line, warnings

    def replay_files(self) -> tuple[list[Path], list[str]]:
        """(replay files found, missing entries) in game order."""
        found: list[Path] = []
        missing: list[str] = []
        roots = user_dirs()
        for m in sorted(self.matches, key=lambda x: x.get("played_at", "")):
            rel = m.get("replay")
            if not rel:
                missing.append(f"{m.get('map')} ({m.get('played_at')}): "
                               "no replay recorded in the log")
                continue
            name = Path(rel.replace("\\", "/")).name
            hit = next((r / "replays" / name for r in roots
                        if (r / "replays" / name).exists()), None)
            if hit:
                found.append(hit)
            else:
                missing.append(f"{name}: file not found")
        return found, missing


def load_matches(folder: Path = RECORDED) -> list[dict]:
    out = []
    if not folder.exists():
        return out
    for p in sorted(folder.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _ts(match: dict) -> float:
    try:
        return time.mktime(time.strptime(match.get("played_at", ""),
                                         "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def group_series(matches: list[dict], gap_s: int = DEFAULT_GAP_S) -> list[Series]:
    """Group games into series, in this order of preference.

    1. **League series id.** If the game was started through this program we
       know the series because we created it, and nothing is guessed.
    2. **Lobby id.** Even without the league the game supplies a key: a Bo5
       stays in the same Steam lobby between games, and the log records it.
       Confirmed on real data — two games six minutes apart shared a lobby.
    3. **Roster plus time window.** Only a fallback for old data and for log
       fragments without a lobby line.

    The player set is part of the key in case 2 as well: a lobby can stay
    open all evening while the opponents change.

    Only games with at least two human sides; a skirmish against the AI is
    not a reportable series.
    """
    playable = []
    for m in matches:
        ids = {p["steam_id"] for p in m.get("players", []) if p.get("steam_id")}
        sides = {p.get("side") for p in m.get("players", []) if p.get("side")}
        if len(ids) >= 2 and len(sides) >= 2:
            playable.append(m)
    playable.sort(key=_ts)

    def roster(m: dict) -> tuple[str, ...]:
        return tuple(sorted({p["steam_id"] for p in m["players"]
                             if p.get("steam_id")}))

    def hard_key(m: dict):
        """A definitive key where one exists, otherwise None."""
        if m.get("league_series_id"):
            return ("league", m["league_series_id"])
        if m.get("lobby_id"):
            return ("lobby", m["lobby_id"], roster(m))
        return None

    series: list[Series] = []
    by_key: dict[tuple, Series] = {}

    for m in playable:
        key = hard_key(m)
        if key is not None:
            s = by_key.get(key)
            if s is None:
                by_key[key] = s = Series()
                series.append(s)
            s.matches.append(m)
            continue

        # Fallback: append to the most recent series with the same roster if
        # the timing fits. Only the latest one, or games drift into a series
        # from two days ago.
        prev = series[-1] if series else None
        if prev and prev.steam_ids == roster(m) and \
                _ts(m) - _ts(prev.matches[-1]) <= gap_s:
            prev.matches.append(m)
        else:
            s = Series()
            s.matches.append(m)
            series.append(s)

    for s in series:
        s.matches.sort(key=_ts)
    series.sort(key=lambda s: s.played_at)

    # Merge across a host change. The lobby id may join games but must never
    # split them: if the host crashes the series continues in a NEW lobby,
    # and the rules explicitly cover that case. Splitting by lobby id would
    # report one series as two duels.
    #
    # Only merged for an identical roster without a long break. Two separate
    # duels of the same pairing on one evening do not exist under the rules.
    merged: list[Series] = []
    for s in series:
        prev = merged[-1] if merged else None
        if prev and prev.steam_ids == s.steam_ids and \
                _ts(s.matches[0]) - _ts(prev.matches[-1]) <= gap_s:
            prev.matches.extend(s.matches)
            prev.matches.sort(key=_ts)
        else:
            merged.append(s)
    return merged


# ------------------------------------------------------------------- Befehle
def cmd_list(args) -> int:
    reg = Registry.load()
    series = group_series(load_matches(Path(args.dir) if args.dir else RECORDED),
                          args.gap)
    if not series:
        print("No series found.")
        print(f"Recorded games are stored in {RECORDED}.")
        print("Record with:  python -m ladder.recorder --watch")
        return 0
    print(f"{len(series)} series:\n")
    for i, s in enumerate(series, start=1):
        line, warn = s.report(reg)
        wins, unclear = s.score()
        print(f"[{i}] {s.played_at[:16].replace('T', ' ')}  "
              f"{len(s.matches)} game(s)  {', '.join(sorted(set(s.maps)))}")
        print(f"    {line or '(no report line derivable)'}")
        for w in warn:
            print(f"    ! {w}")
        print()
    print("A single series:  python -m ladder.report show <n>")
    return 0


def cmd_show(args) -> int:
    reg = Registry.load()
    series = group_series(load_matches(Path(args.dir) if args.dir else RECORDED),
                          args.gap)
    if not 1 <= args.n <= len(series):
        print(f"There is no series {args.n} (1..{len(series)}).")
        return 1
    s = series[args.n - 1]
    line, warn = s.report(reg)
    wins, unclear = s.score()

    print(f"Series {args.n} -- {s.played_at.replace('T', ' ')}")
    print(f"{'Team' if s.is_team else 'Duel'}, {len(s.matches)} game(s)\n")
    for side, players in s.sides().items():
        who = ", ".join(f"{reg.ufer_name_for(p['steam_id']) or p.get('name')}"
                        f" ({p['steam_id']})" for p in players)
        print(f"  Side {side}: {who}  -- {wins.get(side, 0)} win(s)")
    print("\nGames:")
    for i, m in enumerate(s.matches, start=1):
        out = m.get("outcome") or {}
        res = (f"side {out['winner_side']}" if out.get("status") == "decided"
               else out.get("status", "?"))
        cmd = ", ".join(f"{k}={v}" for k, v in (m.get("commanders") or {}).items())
        dur = f"{m['duration_s']:.0f}s" if m.get("duration_s") else "?"
        print(f"  {i}. {m.get('map'):<22} {dur:>6}  -> {res}"
              + (f"   [{cmd}]" if cmd else ""))
    print(f"\nReport line:\n  {line or '(not derivable)'}")
    for w in warn:
        print(f"  ! {w}")
    found, missing = s.replay_files()
    print(f"\nReplays: {len(found)} found"
          + (f", {len(missing)} missing" if missing else ""))
    for f in found:
        print(f"  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")
    for mi in missing:
        print(f"  ! {mi}")
    if found:
        print(f"\nCollect with:  python -m ladder.report collect {args.n} "
              f"--out <folder>")
    return 0


def cmd_collect(args) -> int:
    reg = Registry.load()
    series = group_series(load_matches(Path(args.dir) if args.dir else RECORDED),
                          args.gap)
    if not 1 <= args.n <= len(series):
        print(f"There is no series {args.n} (1..{len(series)}).")
        return 1
    s = series[args.n - 1]
    # `collect` is the step that prepares something to send, so this is where
    # eligibility applies. `list` and `show` stay ungated on purpose.
    line, warn = s.report(reg, elig=Eligibility.load())
    if not line:
        print("This series does not count for the ladder:")
        for w in warn:
            print(f"  ! {w}")
        print("\nNothing was collected. The replays are still in your Forts "
              "folder;\nreporting them by hand is your call, not this tool's.")
        return 1
    found, missing = s.replay_files()
    if not found:
        print("No replay files found -- nothing to collect.")
        for mi in missing:
            print(f"  ! {mi}")
        return 1

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    copied = []
    for i, f in enumerate(found, start=1):
        # Numbered prefix so the game order stays visible in the folder —
        # the original names sort by map, not by game.
        dst = out / f"{i:02d}_{f.name}"
        if not dst.exists():
            shutil.copy2(f, dst)
        copied.append(dst)

    (out / "report.txt").write_text(
        line + "\n\n" + "\n".join(f"! {w}" for w in warn), encoding="utf-8")

    total = sum(p.stat().st_size for p in copied) / 1e6
    print(f"{len(copied)} Replay(s), {total:.1f} MB -> {out}")
    for p in copied:
        print(f"  {p.name}")
    print(f"\nReport line (also written to {out / 'report.txt'}):\n\n  {line}\n")
    for w in warn:
        print(f"  ! {w}")
    if missing:
        print("\nMissing replays:")
        for mi in missing:
            print(f"  ! {mi}")
    print("\nSubmitting is up to you -- read it over first.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="folder holding the recorded games")
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP_S,
                    help="longest pause within one series (seconds)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every series detected")
    p = sub.add_parser("show", help="one series in detail")
    p.add_argument("n", type=int)
    p = sub.add_parser("collect", help="copy replays and report into a folder")
    p.add_argument("n", type=int)
    p.add_argument("--out", required=True)
    args = ap.parse_args()
    return {"list": cmd_list, "show": cmd_show,
            "collect": cmd_collect}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
