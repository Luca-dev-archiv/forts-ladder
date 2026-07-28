"""The open ladder: same method, playable any time.

The league caps play at one duel and one brawl per calendar month. That is
an administrative limit, not a rating rule — every series is declared,
checked against replays and typed in by hand. Automating that removes the
reason for the cap.

It does not remove the reason for the K factors. Those are calibrated for
roughly twelve series a year: a Bo5 sweep moves +90 points, which is a
sensible resolution monthly and a random number generator at five a week.
So K is quartered here.

Farming turned out to matter less than expected and needs no opponent
restriction: 600 points above your opponent earns 1.07 points per game,
while a single loss costs 34 of them back. The flat scaling (500 instead of
400) handles it. That is why the "own group or adjacent" rule is dropped —
it was the second reason you could not simply play whenever you wanted.

Three differences in total: K quartered, a starting phase where the first
games count double, and a weekly cap **per pairing** rather than per player.
Limiting how often the same pairing counts is the only way to stop two
people arranging results without stopping everyone else from playing.

This number is never mixed with the league's. Two columns, like classical
and blitz in chess: same formula, different pool, different cadence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .ratings import (
    DEFAULT_RATING, SCALING_1V1, SCALING_TEAM, PlayerState, expected,
    k_factor, team_rating, tier_of,
)


@dataclass(frozen=True)
class OpenConfig:
    """The knobs. Deliberately few; the reasoning is in the module docstring."""

    # League K divided by this. Four, because the open ladder sees roughly
    # five times the games, keeping yearly movement comparable.
    k_divisor: float = 4.0

    # Starting phase: below this many rated games, everything counts double.
    provisional_games: int = 10
    provisional_factor: float = 2.0

    # Rated games per pairing per calendar week. Twelve is two Bo5s and
    # change — plenty for a long evening, too few for an arranged loop.
    max_games_per_pair_per_week: int = 12


DEFAULT = OpenConfig()


def _iso_week(day: str) -> str:
    try:
        d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        d = date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


@dataclass
class OpenState(PlayerState):
    rated_games: int = 0
    unrated_games: int = 0


@dataclass
class OpenResult:
    players: dict[str, OpenState] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def table(self) -> list[dict]:
        ordered = sorted(self.players.values(), key=lambda p: -p.rating)
        return [{"rank": i, "title": tier_of(p.rating).title, "name": p.name,
                 "rating": round(p.rating, 1), "peak": round(p.peak, 1),
                 "games": p.games, "rated_games": p.rated_games,
                 "unrated_games": p.unrated_games,
                 "provisional": p.rated_games < DEFAULT.provisional_games}
                for i, p in enumerate(ordered, start=1)]


def effective_k(rating: float, mode: str, rated_games: int,
                cfg: OpenConfig = DEFAULT) -> float:
    k = k_factor(rating, mode) / cfg.k_divisor
    if rated_games < cfg.provisional_games:
        k *= cfg.provisional_factor
    return k


def recompute(events: list[dict], seed: dict[str, float] | None = None,
              cfg: OpenConfig = DEFAULT,
              default_rating: float = DEFAULT_RATING,
              allow=None) -> OpenResult:
    """Open ladder standings from the event chain.

    A separate loop rather than a flag in `ratings.recompute`: that module
    is the verified reproduction of the league formula and should not be
    diluted by a second rating's special cases. The building blocks are
    imported, not copied.

    `allow(name) -> bool` gates who may be rated at all. It takes a plain
    predicate rather than a consent registry so this module stays independent
    of that plumbing — see `eligibility.consent_filter`. An event with any
    disallowed participant is skipped entirely, which is also what makes
    withdrawal retroactive: recomputing simply stops seeing it.
    """
    seed = seed or {}
    res = OpenResult()
    pair_games: dict[tuple[str, str, str], int] = {}   # (a, b, week) -> games

    def get(name: str) -> OpenState:
        if name not in res.players:
            r = seed.get(name, default_rating)
            res.players[name] = OpenState(name=name, rating=r, peak=r)
        return res.players[name]

    for ev in sorted(events, key=lambda e: (e.get("date") or "",
                                            str(e.get("event_id") or ""))):
        day = ev.get("date") or ""
        week = _iso_week(day)
        games = int(ev["games"])

        if ev["kind"] == "1v1":
            side_a, side_b = [ev["a"]], [ev["b"]]
        elif ev["kind"] == "team":
            side_a, side_b = list(ev["team_a"]), list(ev["team_b"])
        else:
            raise ValueError(f"unknown event kind {ev['kind']!r}")

        if allow is not None:
            blocked = [n for n in side_a + side_b if not allow(n)]
            if blocked:
                # Skipped before `get()`, so a non-consenting player does not
                # even get a row in the standings.
                res.notes.append(
                    f"{day}: {'+'.join(side_a)} vs {'+'.join(side_b)} — "
                    f"not counted, no consent on record for "
                    f"{', '.join(sorted(blocked))}")
                continue

        for n in side_a + side_b:
            get(n)

        # For teams the line-up decides, not the individual player, or the
        # cap could be dodged by swapping partners.
        key = (_pair_key("+".join(sorted(side_a)), "+".join(sorted(side_b)))
               + (week,))
        already = pair_games.get(key, 0)
        rated = max(0, min(games, cfg.max_games_per_pair_per_week - already))
        pair_games[key] = already + games

        if rated < games:
            res.notes.append(
                f"{day}: {'+'.join(side_a)} vs {'+'.join(side_b)} — "
                f"{games - rated} of {games} games unrated "
                f"(weekly per-pairing cap reached)")

        if rated == 0:
            for n in side_a + side_b:
                p = get(n)
                p.games += games
                p.unrated_games += games
            continue

        # Only the rated share counts, and the score is taken pro rata: a
        # 4:1 with three rated games is credited in the same proportion.
        score_a_full = int(ev["score_a"])
        share = rated / games
        score_a = score_a_full * share
        mode = "1v1" if ev["kind"] == "1v1" else (
            "coop" if ev.get("coop") else "tdm")

        if ev["kind"] == "1v1":
            ra, rb = get(ev["a"]).rating, get(ev["b"]).rating
            e_a = expected(ra, rb, SCALING_1V1)
        else:
            ra = team_rating([get(n).rating for n in side_a])
            rb = team_rating([get(n).rating for n in side_b])
            e_a = expected(ra, rb, SCALING_TEAM)
        e_b = 1.0 - e_a
        score_b = rated - score_a

        for names, score, e in ((side_a, score_a, e_a), (side_b, score_b, e_b)):
            for n in names:
                p = get(n)
                k = effective_k(p.rating, mode, p.rated_games, cfg)
                p.rating += k * (score - e * rated)
                p.peak = max(p.peak, p.rating)
                p.games += games
                p.rated_games += rated
                p.unrated_games += games - rated
                p.series += 1
                p.history.append((day, p.rating))

    return res
