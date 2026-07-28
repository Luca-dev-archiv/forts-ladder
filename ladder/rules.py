"""The league rules, as far as a machine can check them.

Source: the rule set published by the UFER maintainer (duels, brawls and
the FPL pool). This module is an implementation, not a variant — where they
disagree, the published text wins.

Some of these rules are currently kept from memory and reconstructed from
replays when disputed. The recorder can decide exactly those cases, because
the log holds the map, both commanders and the result of every game. The
point is saving work, not surveillance: "commander played twice" becomes one
line in a log instead of half an hour of replay review.

Not covered, because the log does not show it: deliberate core drops, moved
cores, restart entitlements and the reward point system. Those stay with
human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .ratings import tier_of

# Duels use the season's ranked pool minus one permanently banned map. The
# pool itself is read from the game install at runtime (ladder/game_data.py)
# so it cannot go stale and no game data lands in the repository.
PERMANENTLY_BANNED_DUEL_MAPS = frozenset({"Hillfort"})

# Brawls and FPL use their own pool. It is listed here because it cannot be
# derived from the game files.
FPL_MAP_POOL = (
    "Boonies 2v2 revamped",
    "Caverns MS",
    "Up & Down 2v2 MS",
    "Vanilla Large MS",
    "Wingman 2v2 MS",
    "Green Breeze",
    "Heavy Rain",
    "Stalactites 2v2 MS",
    "Snow Leopards 2v2 MS",
    "Fort Falls 2v2 (High Seas)",
    "Windswept (High Seas)",
)

MAX_TIMES_PER_MAP = 2          # "Each map cannot be played more than twice"
MAX_GAMES_PER_SERIES = 9       # Bo1..Bo9
MAX_FLAT_GAMES = 6             # or a flat game count up to 6

# Mandatory lobby settings from the rules, keyed exactly as in
# `multiplayer.lua` so a launcher can apply them directly.
REQUIRED_LOBBY_SETTINGS = {
    "ArtificialHostLag": True,     # removes the host's latency advantage
}
REQUIRED_LOBBY_SETTINGS_BRAWL = {
    "ArtificialHostLag": True,
    "CoopOnElimination": True,     # brawl rule: "coop on death enabled"
}
REQUIRED_MODS = ("Commanders", "ToG", "MS", "Portals", "Repair Station")
OPTIONAL_MODS = ("Firebird",)


@dataclass
class Violation:
    rule: str
    detail: str
    severity: str = "warn"        # "warn" = note, "block" = series invalid


_TIER_ORDER = ["Novice", "Intermediate", "Adept", "Master", "Grand Master"]


def _tier_index(rating: float) -> int:
    return _TIER_ORDER.index(tier_of(rating).title)


def duel_pairing_allowed(rating_a: float, rating_b: float) -> tuple[bool, str]:
    """Same group or one adjacent; Grand Masters only among themselves.

    Not symmetric: the Grand Master rule also excludes a Master challenging
    upwards, even though that would be "one group above" for them.
    """
    ia, ib = _tier_index(rating_a), _tier_index(rating_b)
    gm = _TIER_ORDER.index("Grand Master")
    if (ia == gm) != (ib == gm):
        return False, "Grand Masters only duel other Grand Masters"
    if abs(ia - ib) > 1:
        return False, (f"{_TIER_ORDER[ia]} vs {_TIER_ORDER[ib]} is more than "
                       "one group apart")
    return True, "allowed"


def month_key(d: date | str) -> str:
    """Quotas follow the calendar month, not a rolling 30 days.

    Explicit in the rules: a duel on 31 August allows the next on 1 September.
    """
    if isinstance(d, str):
        return d[:7]
    return f"{d.year:04d}-{d.month:02d}"


def quota_ok(player: str, kind: str, when: date | str,
             played: list[dict]) -> tuple[bool, str]:
    """One duel *and* one brawl per calendar month — separate quotas."""
    if kind not in ("duel", "brawl"):
        return True, "no quota for this kind"
    mk = month_key(when)
    same = [p for p in played
            if p.get("kind") == kind and month_key(p.get("date", "")) == mk
            and player in p.get("players", [])]
    if same:
        return False, f"{player} already played a {kind} in {mk}"
    return True, f"{kind} quota for {mk} is free"


@dataclass
class SeriesCheck:
    violations: list[Violation] = field(default_factory=list)
    maps_played: dict[str, int] = field(default_factory=dict)
    commander_wins: dict[str, set[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(v.severity == "block" for v in self.violations)


def check_series(games: list[dict], *, pool: list[str] | None = None,
                 brawl: bool = False) -> SeriesCheck:
    """Check a series against the mechanically verifiable rules.

    `games` are recorder entries in game order, each with `map`,
    `commanders` and `outcome`.

    Checked: map in the allowed pool, no map more than twice, series length,
    and commander reuse after a win. That last one is why this exists —
    verifying it by hand means remembering nine games.
    """
    chk = SeriesCheck()
    allowed = set(pool) if pool else (set(FPL_MAP_POOL) if brawl else None)

    if len(games) > MAX_GAMES_PER_SERIES:
        chk.violations.append(Violation(
            "series length",
            f"{len(games)} games, at most {MAX_GAMES_PER_SERIES} allowed",
            "block"))

    for i, g in enumerate(games, start=1):
        map_name = g.get("map") or "?"
        chk.maps_played[map_name] = chk.maps_played.get(map_name, 0) + 1

        if allowed is not None and map_name not in allowed:
            chk.violations.append(Violation(
                "map pool", f"game {i}: {map_name} is not in the "
                f"{'FPL' if brawl else 'ranked'} pool", "block"))
        if not brawl and map_name in PERMANENTLY_BANNED_DUEL_MAPS:
            chk.violations.append(Violation(
                "map ban", f"game {i}: {map_name} is permanently banned "
                "in duels", "block"))
        if chk.maps_played[map_name] > MAX_TIMES_PER_MAP:
            chk.violations.append(Violation(
                "map repeat",
                f"game {i}: {map_name} played "
                f"{chk.maps_played[map_name]} times (allowed: "
                f"{MAX_TIMES_PER_MAP})", "block"))

        # A commander is spent for a side once they win with it.
        commanders = g.get("commanders") or {}
        winner = (g.get("outcome") or {}).get("winner_side")
        for key, cmdr in commanders.items():
            side = key.replace("side", "")
            used = chk.commander_wins.setdefault(side, set())
            if cmdr in used:
                chk.violations.append(Violation(
                    "commander reuse",
                    f"game {i}: side {side} plays {cmdr} again after already "
                    "winning with it", "block"))
            if winner is not None and str(winner) == side:
                used.add(cmdr)

    return chk


def report_line(kind: str, own: list[str], opponents: list[str],
                score: tuple[int, int] | None = None) -> str:
    """The report line the rules prescribe for the Discord channel.

    The score is optional per the rules; it is included because the recorder
    knows it anyway and it saves the maintainer counting.
    """
    label = "UFER Brawl" if kind == "brawl" else "UFER Duel"
    line = f"{label}: {', '.join(own)} vs {', '.join(opponents)}"
    if score:
        line += f" {score[0]}-{score[1]}"
    return line
