"""Game modes: what is rated, how big the teams are, how the draft runs.

"1v1, rated, Bo3" used to be hardcoded everywhere. With 2v2, tournaments
with their own format and unrated games, that has to be a setting.

A mode fixes three things: team size (which decides the K table and the map
pool), whether it is rated, and the draft settings.

Unrated games are still recorded in full. A game that does not count is
still interesting — head-to-head records, map statistics, commander picks —
and storing only rated games throws away exactly the data needed to
re-tune the K factors later.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    team_size: int
    rated: bool = True
    coop: bool = False              # team co-op instead of team deathmatch
    best_of: int = 3
    commander_bans_per_side: int = 1
    draft_seconds: float | None = 30.0
    # Unrated games need no draft: someone who wants a quick game should not
    # have to click through twelve tiles first.
    draft_enabled: bool = True

    @property
    def players_per_match(self) -> int:
        return self.team_size * 2

    @property
    def rating_mode(self) -> str:
        """Which K table from `ratings` applies."""
        if self.team_size == 1:
            return "1v1"
        return "coop" if self.coop else "tdm"

    @property
    def needs_map_pool(self) -> str:
        """Which map pool: `duel` (ranked minus the ban) or `fpl` (2v2+)."""
        return "duel" if self.team_size == 1 else "fpl"

    def with_(self, **changes) -> "Mode":
        """Derive a variant, e.g. a tournament with its own format."""
        return replace(self, **changes)

    def describe(self) -> str:
        parts = [f"{self.team_size}v{self.team_size}",
                 "rated" if self.rated else "unrated",
                 f"Bo{self.best_of}"]
        if self.coop:
            parts.insert(1, "Co-op")
        if not self.draft_enabled:
            parts.append("no draft")
        return ", ".join(parts)


# `key` is stable and ends up in the match files, so renaming one would make
# old recordings unreadable.
RANKED_1V1 = Mode("ranked_1v1", "Ranked 1v1", team_size=1)
RANKED_2V2 = Mode("ranked_2v2", "Ranked 2v2", team_size=2, best_of=5)
RANKED_3V3 = Mode("ranked_3v3", "Ranked 3v3", team_size=3, best_of=5)
COOP_2V2 = Mode("coop_2v2", "Co-op 2v2", team_size=2, coop=True, best_of=5)

UNRANKED_1V1 = Mode("unranked_1v1", "Unranked 1v1", team_size=1, rated=False,
                    best_of=1, draft_enabled=False)
UNRANKED_2V2 = Mode("unranked_2v2", "Unranked 2v2", team_size=2, rated=False,
                    best_of=1, draft_enabled=False)

# Tournaments get longer series and more ban time: people sit prepared, and
# a wrong call costs more.
TOURNAMENT_1V1 = Mode("tournament_1v1", "Tournament 1v1", team_size=1, best_of=5,
                      commander_bans_per_side=2, draft_seconds=45.0)
TOURNAMENT_2V2 = Mode("tournament_2v2", "Tournament 2v2", team_size=2, best_of=5,
                      commander_bans_per_side=2, draft_seconds=45.0)

ALL: tuple[Mode, ...] = (
    RANKED_1V1, RANKED_2V2, RANKED_3V3, COOP_2V2,
    UNRANKED_1V1, UNRANKED_2V2, TOURNAMENT_1V1, TOURNAMENT_2V2,
)

BY_KEY = {m.key: m for m in ALL}


def get(key: str) -> Mode:
    if key not in BY_KEY:
        raise KeyError(f"unknown mode {key!r}. Known: "
                       + ", ".join(sorted(BY_KEY)))
    return BY_KEY[key]


def rated_modes() -> list[Mode]:
    return [m for m in ALL if m.rated]
