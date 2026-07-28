"""Tests for the open ladder.

The question these have to answer: does a ranking stay useful once the
monthly cap is removed? So they check not just that it computes, but that
the magnitudes are right — a rating where one evening moves 300 points is
not a ranking.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.open_ladder import (  # noqa: E402
    DEFAULT, OpenConfig, effective_k, recompute,
)
from ladder.ratings import k_factor, series_1v1  # noqa: E402


def duel(day, a, b, games, score_a, eid=None):
    return {"kind": "1v1", "date": day, "event": "open", "a": a, "b": b,
            "games": games, "score_a": score_a, "event_id": eid}


def test_a_single_series_moves_far_less_than_in_ufer():
    """A Bo5 sweep must not move 90 points if you play every day."""
    ufer = series_1v1("A", 1450, "B", 1450, games=5, score_a=5).deltas["A"]
    res = recompute([duel("2026-08-01", "A", "B", 5, 5)],
                    seed={"A": 1450, "B": 1450})
    # The placement phase doubles it, so compare against twice the quarter.
    open_ = res.players["A"].rating - 1450
    assert abs(ufer - 90) < 1, ufer
    assert open_ < ufer / 1.5, f"the open rating moves too much: {open_:+.1f}"


def test_k_is_a_quarter_of_ufer_after_the_starting_phase():
    settled = DEFAULT.provisional_games + 1
    assert abs(effective_k(1450, "1v1", settled) - k_factor(1450, "1v1") / 4) < 1e-9
    # Doubled during placement so newcomers land near their level fast.
    assert effective_k(1450, "1v1", 0) == 2 * effective_k(1450, "1v1", settled)


def test_a_newcomer_converges_within_days_not_a_year():
    """An underrated player has to climb quickly.

    The spreadsheet solves this by hand today, via a scoring form
    ("Suggested Rating"). Mechanically, the placement phase does it.
    """
    events = [duel(f"2026-08-{d:02d}", "Rookie", f"Opponent{d}", 5, 5)
              for d in range(1, 11)]
    seed = {"Rookie": 1000.0}
    seed.update({f"Opponent{d}": 1600.0 for d in range(1, 11)})
    res = recompute(events, seed=seed)
    assert res.players["Rookie"].rating > 1400, res.players["Rookie"].rating


def test_the_same_pairing_stops_counting_after_the_weekly_cap():
    """Two people agreeing to trade points get nowhere."""
    events = [duel(f"2026-08-0{d}", "A", "B", 9, 9, eid=f"e{d}")
              for d in (3, 4, 5)]      # all in the same calendar week
    res = recompute(events, seed={"A": 1500, "B": 1500})
    assert res.players["A"].rated_games == DEFAULT.max_games_per_pair_per_week
    assert res.players["A"].unrated_games == 27 - DEFAULT.max_games_per_pair_per_week
    assert any("unrated" in n for n in res.notes)


def test_the_cap_is_per_pairing_not_per_player():
    """Playing a lot must not be punished — only playing the same opponent."""
    events = [duel("2026-08-03", "A", f"Opponent{i}", 9, 5, eid=f"e{i}")
              for i in range(6)]
    res = recompute(events, seed={"A": 1500})
    assert res.players["A"].unrated_games == 0, "a frequent player got capped"
    assert res.players["A"].rated_games == 54


def test_the_cap_cannot_be_dodged_by_swapping_partners_in_teams():
    """For teams the line-up counts, not the individual player."""
    ev = {"kind": "team", "date": "2026-08-03", "event": "open",
          "team_a": ["A", "B"], "team_b": ["C", "D"],
          "games": 9, "score_a": 9, "event_id": "x"}
    res = recompute([ev, dict(ev, event_id="y")],
                    seed={n: 1500.0 for n in "ABCD"})
    assert res.players["A"].unrated_games > 0


def test_grinding_a_much_weaker_opponent_barely_pays():
    """The flat scaling handles farming on its own — which is why there is
    no opponent quota here, unlike in the spreadsheet."""
    events = [duel(f"2026-{m:02d}-{d:02d}", "Strong", f"Weak{m}{d}", 1, 1)
              for m in range(1, 7) for d in range(1, 11)]
    seed = {"Strong": 1900.0}
    seed.update({f"Weak{m}{d}": 1300.0 for m in range(1, 7) for d in range(1, 11)})
    res = recompute(events, seed=seed)
    gain = res.players["Strong"].rating - 1900
    assert gain < 60, f"60 wins over far weaker players paid {gain:+.0f}"


def test_ufer_ratings_are_never_touched():
    """Two columns, no cross-talk. The seed value stays the seed value."""
    seed = {"A": 1450.0, "B": 1450.0}
    before = dict(seed)
    recompute([duel("2026-08-01", "A", "B", 5, 5)], seed=seed)
    assert seed == before


def test_config_is_adjustable_without_touching_the_formula():
    strict = OpenConfig(k_divisor=8.0, provisional_games=0,
                        max_games_per_pair_per_week=4)
    res = recompute([duel("2026-08-01", "A", "B", 9, 9)],
                    seed={"A": 1500, "B": 1500}, cfg=strict)
    assert res.players["A"].rated_games == 4


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e or 'assertion failed'}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
