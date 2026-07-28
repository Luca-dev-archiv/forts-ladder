"""Golden tests against the real ranking workbook.

These four cases were copied out of the workbook, not generated from the
code. They are the only proof that this project produces the same numbers as
the list the scene already uses — and therefore the precondition for anyone
switching to it. If one fails, the ranking is incompatible, however clean
the rest is.

Quelle: "Unofficial Forts Elo Ranking - UFER.xlsx",
Sheets `Variables`, `Elo Algorithm 1v1`, `Elo Algorithm 2v2 TDM`.

    python -m pytest tests/ -v      (or simply: python tests/test_ratings.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.ratings import (  # noqa: E402
    expected, k_factor, recompute, series_1v1, series_team, team_rating,
    tier_of, SCALING_1V1,
)

TOL = 0.05          # the workbook shows one decimal place


def test_tiers_from_variables_sheet():
    assert tier_of(999).title == "Novice"
    assert tier_of(1000).title == "Intermediate"
    assert tier_of(1299).title == "Intermediate"
    assert tier_of(1300).title == "Adept"
    assert tier_of(1599).title == "Adept"
    assert tier_of(1600).title == "Master"
    assert tier_of(1899).title == "Master"
    assert tier_of(1900).title == "Grand Master"
    assert k_factor(1518.7, "1v1") == 36          # Adept
    assert k_factor(1149.6, "tdm") == 37          # Intermediate
    assert k_factor(1340.6, "tdm") == 32          # Adept
    assert k_factor(2500, "1v1") == 9             # Deckel ab 2200


def test_1v1_baumstaender_vs_brucepixar():
    """Sheet `Elo Algorithm 1v1`, row 1: Bo4, 3:1, event "duel"."""
    r = series_1v1("Duelist1", 1518.7, "Duelist2", 1504.3,
                   games=4, score_a=3)
    assert abs(r.expected_a - 0.5166) < 0.0001
    assert abs(r.deltas["Duelist1"] - 33.6) < TOL
    assert abs(r.deltas["Duelist2"] + 33.6) < TOL


def test_1v1_voprof_vs_pilar43():
    """Sheet `Elo Algorithm 1v1`, row 2: 7 games, 5:2."""
    r = series_1v1("Duelist3", 1149.6, "Duelist4", 1163.4, games=7, score_a=5)
    assert abs(r.expected_a - 0.4841) < 0.0001
    assert abs(r.deltas["Duelist3"] - 67.7) < TOL
    assert abs(r.deltas["Duelist4"] + 67.7) < TOL


def test_team_rating_is_average_times_scaling():
    """Blatt `Elo Algorithm 2v2 TDM`, Spalte "Rating T1"/"Rating T2"."""
    assert abs(team_rating([1149.6, 1340.6]) - 933.8) < TOL
    assert abs(team_rating([1287.3, 1312.9]) - 975.1) < TOL


def test_2v2_deltas_use_each_players_own_k():
    """Same game, different amounts — the proof of a per-player K.

    Sheet `Elo Algorithm 2v2 TDM`: 6 games, 2:4 from team 1's point of view.
    """
    r = series_team({"Duelist3": 1149.6, "Duelist5": 1340.6},
                    {"Duelist6": 1287.3, "Duelist7": 1312.9},
                    games=6, score_a=2)
    assert abs(r.expected_a - 0.4605) < 0.0001
    assert abs(r.deltas["Duelist3"] + 28.2) < TOL
    assert abs(r.deltas["Duelist5"] + 24.4) < TOL
    assert abs(r.deltas["Duelist6"] - 28.2) < TOL
    assert abs(r.deltas["Duelist7"] - 24.4) < TOL
    # Exactly the property that makes the system non-zero-sum:
    assert r.deltas["Duelist3"] != r.deltas["Duelist5"]


def test_scaling_is_500_not_classic_400():
    """The classic 400 would give a different number — pin the intent down."""
    e500 = expected(1518.7, 1504.3, SCALING_1V1)
    e400 = expected(1518.7, 1504.3, 400.0)
    assert abs(e500 - 0.5166) < 0.0001
    assert abs(e400 - 0.5166) > 0.001


def test_recompute_reproduces_a_single_duel():
    """A full recompute must produce the same numbers as a single call."""
    players = recompute(
        [{"kind": "1v1", "date": "2026-07-21", "event": "duel",
          "a": "Duelist1", "b": "Duelist2", "games": 4, "score_a": 3}],
        seed={"Duelist1": 1518.7, "Duelist2": 1504.3})
    assert abs(players["Duelist1"].rating - 1552.3) < TOL   # Ranking-Blatt
    assert abs(players["Duelist2"].rating - 1470.7) < TOL     # Ranking-Blatt
    assert players["Duelist1"].peak >= players["Duelist1"].rating
    assert players["Duelist1"].title == "Adept"


def test_recompute_is_order_dependent():
    """K depends on the current rating — so order matters.

    Not a flaw but a property that has to be documented: entering games
    after the fact changes every later number.
    """
    ev1 = {"kind": "1v1", "date": "2026-01-01", "event": "a",
           "a": "X", "b": "Y", "games": 5, "score_a": 5}
    ev2 = {"kind": "1v1", "date": "2026-01-02", "event": "b",
           "a": "X", "b": "Y", "games": 5, "score_a": 0}
    forward = recompute([ev1, ev2], seed={"X": 950.0, "Y": 1250.0})
    ev1r, ev2r = dict(ev1, date="2026-01-02"), dict(ev2, date="2026-01-01")
    reverse = recompute([ev1r, ev2r], seed={"X": 950.0, "Y": 1250.0})
    assert abs(forward["X"].rating - reverse["X"].rating) > 0.1


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
