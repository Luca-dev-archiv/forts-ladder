"""The UFER rating system, reproduced rather than replaced.

UFER (Unofficial Forts Elo Ranking) is the ladder the competitive scene
actually uses. A system that produces different numbers is not a successor,
it is a competitor — and it loses. So this module reproduces UFER's numbers
to the decimal.

The formula was never written down anywhere. It was reconstructed from the
spreadsheet and verified against four real rows, which tests/test_ratings.py
keeps as golden tests.

The reconstruction has since been confirmed by the person who maintains the
sheet: "Regarding the variables for elo calculation … you got it right. What
you already have is correct." (2026-07-28). The sheet has a "Variables" tab
that holds the same constants; it was hidden at the time this was worked out.
So these numbers are no longer an inference — but the golden tests stay,
because that is what would catch a change to them.

The system in short:

1. Elo logistic, but flatter: E = 1 / (1 + 10^((R_opponent - R_own) / S)),
   with S = 500 for 1v1 and 600 for team modes. Classic Elo uses 400; the
   larger value dampens how strongly a rating gap predicts the outcome.
2. The **series** is the unit, not the game:
   delta = K * (games_won - E * games_played)
3. K depends on the player's title and the mode — 48 for a novice down to 9
   at the top. This is a step function standing in for the rating uncertainty
   that Glicko-2 models explicitly.
4. Team rating = mean of members * scaling of the mean's tier.

Two properties worth knowing:

- **It is not zero-sum.** Every player gets their own K, so rating is created
  or destroyed when unequal players meet. Harmless for a scene ladder, worth
  knowing for long-term comparisons.
- In 1v1 that is unverified: both sample rows had two players of the same
  title. `per_player_k=False` switches to a shared K should the spreadsheet
  ever show otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tier:
    title: str
    lo: float
    hi: float
    k_1v1: int
    k_team_tdm: int
    k_team_coop: int
    team_scaling: float


# From the spreadsheet's `Variables` sheet. It records two K-factor changes
# (2025-09-13 and 2025-09-15); this is the state after them. If the sheet
# changes again, update here AND note the date, or old and new recomputations
# will disagree.
TIERS: tuple[Tier, ...] = (
    Tier("Novice",        0,    999.0,  48, 42, 32, 0.50),
    Tier("Intermediate",  1000, 1299.0, 42, 37, 28, 0.75),
    Tier("Adept",         1300, 1599.0, 36, 32, 24, 0.75),
    Tier("Master",        1600, 1899.0, 27, 24, 18, 0.80),
    Tier("Grand Master",  1900, 2199.0, 18, 16, 12, 0.80),
    # Above 2200 the sheet has another row, also titled "Grand Master", with
    # halved factors. Not a separate title — a cap on movement at the top.
    Tier("Grand Master",  2200, float("inf"), 9, 8, 6, 0.80),
)

SCALING_1V1 = 500.0
SCALING_TEAM = 600.0
BASE = 10.0

# Fallback only. The spreadsheet estimates newcomers through a scoring sheet
# (map control, build order, defense, weapons built, metal float) rather than
# starting everyone at a fixed value.
DEFAULT_RATING = 1000.0


def tier_of(rating: float) -> Tier:
    for t in TIERS:
        if t.lo <= rating <= t.hi:
            return t
    return TIERS[0]


def k_factor(rating: float, mode: str) -> int:
    t = tier_of(rating)
    if mode == "1v1":
        return t.k_1v1
    if mode == "coop":
        return t.k_team_coop
    if mode == "tdm":
        return t.k_team_tdm
    raise ValueError(f"unknown mode {mode!r} (expected: 1v1, tdm, coop)")


def expected(own: float, opponent: float, scaling: float) -> float:
    """Expected share of games won. Both sides sum to 1."""
    return 1.0 / (1.0 + BASE ** ((opponent - own) / scaling))


def team_rating(members: list[float]) -> float:
    """Mean of the members, scaled by the tier the mean falls into.

    Verified against `Elo Algorithm 2v2 TDM`: (1149.6, 1340.6) -> 933.8 and
    (1287.3, 1312.9) -> 975.1. Both means land in tiers with the same scaling,
    so those rows cannot tell whether the lookup uses the mean or a member;
    the mean is the only reading that needs no special case for mixed teams.
    """
    if not members:
        raise ValueError("team without members")
    avg = sum(members) / len(members)
    return avg * tier_of(avg).team_scaling


@dataclass
class SeriesResult:
    """Per-player deltas plus the intermediate values.

    E and K are returned because a ladder has to be checkable: someone
    doubting a number should be able to see them without reading the code.
    """
    deltas: dict[str, float]
    expected_a: float
    expected_b: float
    rating_a: float
    rating_b: float
    k_used: dict[str, int] = field(default_factory=dict)


def series_1v1(player_a: str, rating_a: float,
               player_b: str, rating_b: float,
               games: int, score_a: int,
               per_player_k: bool = True) -> SeriesResult:
    """Evaluate a 1v1 series (a Bo1 is just games=1)."""
    if games <= 0:
        raise ValueError("a series without games has no result")
    if not 0 <= score_a <= games:
        raise ValueError(f"score_a={score_a} does not fit games={games}")

    e_a = expected(rating_a, rating_b, SCALING_1V1)
    e_b = 1.0 - e_a
    score_b = games - score_a

    k_a = k_factor(rating_a, "1v1")
    k_b = k_factor(rating_b, "1v1") if per_player_k else k_a

    return SeriesResult(
        deltas={player_a: k_a * (score_a - e_a * games),
                player_b: k_b * (score_b - e_b * games)},
        expected_a=e_a, expected_b=e_b,
        rating_a=rating_a, rating_b=rating_b,
        k_used={player_a: k_a, player_b: k_b})


def series_team(team_a: dict[str, float], team_b: dict[str, float],
                games: int, score_a: int, coop: bool = False) -> SeriesResult:
    """Evaluate a team series. `team_x` maps player name to rating.

    Every player gets their team's delta scaled by their **own** K, which is
    why amounts differ inside one team. Verified against a real row: team
    933.8 vs 975.1, 6 games, 2:4, E=0.4605 gave -28.2 (K=37) and -24.4 (K=32).
    """
    if games <= 0:
        raise ValueError("a series without games has no result")
    if not team_a or not team_b:
        raise ValueError("both teams need members")

    r_a = team_rating(list(team_a.values()))
    r_b = team_rating(list(team_b.values()))
    e_a = expected(r_a, r_b, SCALING_TEAM)
    e_b = 1.0 - e_a
    score_b = games - score_a
    mode = "coop" if coop else "tdm"

    deltas: dict[str, float] = {}
    k_used: dict[str, int] = {}
    for name, rating in team_a.items():
        k = k_factor(rating, mode)
        k_used[name] = k
        deltas[name] = k * (score_a - e_a * games)
    for name, rating in team_b.items():
        k = k_factor(rating, mode)
        k_used[name] = k
        deltas[name] = k * (score_b - e_b * games)

    return SeriesResult(deltas=deltas, expected_a=e_a, expected_b=e_b,
                        rating_a=r_a, rating_b=r_b, k_used=k_used)


@dataclass
class PlayerState:
    name: str
    rating: float
    peak: float
    games: int = 0
    series: int = 0
    history: list[tuple[str, float]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return tier_of(self.rating).title


def recompute(events: list[dict],
              seed: dict[str, float] | None = None,
              default_rating: float = DEFAULT_RATING) -> dict[str, PlayerState]:
    """Apply all events in order and return the final standings.

    Ratings are derived from the match records rather than stored, so anyone
    can run this over the same files and get the same numbers. Peak rating and
    history fall out for free.

    An event looks like:
        {"kind": "1v1", "date": "2026-07-21", "a": ..., "b": ...,
         "games": 4, "score_a": 3}
        {"kind": "team", "team_a": [...], "team_b": [...], "coop": False, ...}

    Series sharing an `event_id` form one event, and inside it every match is
    rated with the rating the player *entered* with; deltas are collected and
    booked at the end. That is an explicit UFER rule, not a design choice
    ("Player rated 2000 won first match 2:0, next match still counts as
    2000"). Without it, tournament results diverge from round two onwards.

    Order matters because K depends on the current rating, so events are
    sorted by date rather than left to file order.
    """
    seed = seed or {}
    players: dict[str, PlayerState] = {}

    def get(name: str) -> PlayerState:
        if name not in players:
            r = seed.get(name, default_rating)
            players[name] = PlayerState(name=name, rating=r, peak=r)
        return players[name]

    ordered = sorted(events, key=lambda e: (e.get("date") or "",
                                            str(e.get("event_id") or ""),
                                            e.get("event") or ""))

    cur_event: object = object()
    entry: dict[str, float] = {}
    pending: dict[str, float] = {}
    pending_games: dict[str, int] = {}
    pending_series: dict[str, int] = {}
    last_date = ""

    def commit() -> None:
        for name, delta in pending.items():
            p = get(name)
            p.rating += delta
            p.peak = max(p.peak, p.rating)
            p.games += pending_games.get(name, 0)
            p.series += pending_series.get(name, 0)
            p.history.append((last_date, p.rating))
        pending.clear()
        pending_games.clear()
        pending_series.clear()
        entry.clear()

    for ev in ordered:
        # No event_id means the series is its own event, so a fresh object()
        # guarantees it never matches the previous one.
        eid = ev.get("event_id") or object()
        if eid != cur_event:
            commit()
            cur_event = eid
        last_date = ev.get("date") or last_date

        def rating_at_entry(name: str) -> float:
            if name not in entry:
                entry[name] = get(name).rating
            return entry[name]

        if ev["kind"] == "1v1":
            ra = rating_at_entry(ev["a"])
            rb = rating_at_entry(ev["b"])
            res = series_1v1(ev["a"], ra, ev["b"], rb,
                             ev["games"], ev["score_a"])
        elif ev["kind"] == "team":
            ta = {n: rating_at_entry(n) for n in ev["team_a"]}
            tb = {n: rating_at_entry(n) for n in ev["team_b"]}
            res = series_team(ta, tb, ev["games"], ev["score_a"],
                              coop=ev.get("coop", False))
        else:
            raise ValueError(f"unknown event kind {ev['kind']!r}")

        for name, delta in res.deltas.items():
            pending[name] = pending.get(name, 0.0) + delta
            pending_games[name] = pending_games.get(name, 0) + ev["games"]
            pending_series[name] = pending_series.get(name, 0) + 1

    commit()
    return players


def table(players: dict[str, PlayerState]) -> list[dict]:
    """Standings in the shape of the UFER table."""
    ordered = sorted(players.values(), key=lambda p: -p.rating)
    return [{"rank": i, "title": p.title, "name": p.name,
             "rating": round(p.rating, 1), "peak": round(p.peak, 1),
             "games": p.games, "series": p.series}
            for i, p in enumerate(ordered, start=1)]
