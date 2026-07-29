"""Who is on whose team, across a series where the sides swap.

**Forts swaps the sides between games.** The number in the log — side 1, side 2
— says which half of the map somebody was on in *that game*, not which team they
are in for the series. Three separate places had assumed otherwise, and each got
it wrong in its own way:

- The series list grouped players by side across all games, so the same two
  people ended up in both buckets and a duel read "A and B vs A and B".
- The score counted wins by side number, turning one team winning both games of
  a Bo3 into 1-1 — which then went into the report line people paste into
  Discord.
- The commander-reuse check split one player's history across two buckets, so a
  genuine reuse went unnoticed and an innocent one was flagged.

None of that is visible in a test whose fixture always puts the same player on
side 1, which is why it survived this long.

So: the first game with two sides decides who is with whom, and after that
people are followed by Steam ID whatever side they are playing. A player who
only appears later joins the team they did not play against, which is the best
available answer for a substitute.
"""

from __future__ import annotations


def team_map(matches: list[dict]) -> dict[str, int]:
    """Steam ID -> team (1 or 2)."""
    team: dict[str, int] = {}
    for m in matches:
        by_side: dict[int, list[dict]] = {}
        for p in m.get("players", []):
            side = p.get("side")
            if side and side > 0 and p.get("steam_id"):
                by_side.setdefault(side, []).append(p)
        if len(by_side) < 2:
            continue

        groups = [by_side[s] for s in sorted(by_side)]
        if not team:
            for p in groups[0]:
                team[p["steam_id"]] = 1
            for p in groups[1]:
                team[p["steam_id"]] = 2
            continue

        # A later game: recognise each side by whoever is already placed, then
        # put the newcomers with them.
        for group in groups:
            known = [team[p["steam_id"]] for p in group
                     if p["steam_id"] in team]
            if not known:
                continue
            n = max(set(known), key=known.count)
            for p in group:
                team.setdefault(p["steam_id"], n)
    return team


def team_of_side(match: dict, side: int, team: dict[str, int]) -> int:
    """Which team a side number belonged to in one game, or 0 if unknown."""
    for p in match.get("players", []):
        if p.get("side") == side and p.get("steam_id") in team:
            return team[p["steam_id"]]
    return 0


def commander_owner(match: dict, key: str, team: dict[str, int]) -> int:
    """The team behind a `commanders` key like "side1", or 0.

    The recorder writes commanders keyed by the side they were played on, so the
    same translation applies to them.
    """
    digits = "".join(c for c in key if c.isdigit())
    if not digits:
        return 0
    return team_of_side(match, int(digits), team)
