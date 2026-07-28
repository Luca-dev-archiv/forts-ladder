"""Tests for the machine-checkable ladder rules.

Source of the rules checked here: the ranking's published rule set, Discord,
2025-09-08 (duels) and 2025-12-15 (brawls). These tests pin down the
reading — if the rule set changes, they have to change with it, not the
other way round.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.rules import (  # noqa: E402
    check_series, duel_pairing_allowed, month_key, quota_ok, report_line,
)


def test_grand_master_only_plays_grand_master():
    """This is not a distance rule but a wall.

    A Master challenging a Grand Master would be in the clear under the
    distance rule ("one tier up") — the Grandmaster rule forbids it anyway.
    This is exactly where you otherwise build yourself a bug.
    """
    ok, why = duel_pairing_allowed(1950.0, 1750.0)     # GM vs Master
    assert not ok, why
    ok, _ = duel_pairing_allowed(1950.0, 2050.0)       # GM vs GM
    assert ok
    ok, _ = duel_pairing_allowed(1750.0, 1550.0)       # Master vs Adept
    assert ok
    ok, _ = duel_pairing_allowed(1750.0, 1250.0)       # Master vs Intermediate
    assert not ok


def test_quota_is_tied_to_calendar_month():
    """Duel on 31 August -> the next one is allowed on 1 September."""
    assert month_key("2025-08-31") == "2025-08"
    assert month_key("2025-09-01") == "2025-09"
    played = [{"kind": "duel", "date": "2025-08-31", "players": ["Bravo", "X"]}]
    ok, _ = quota_ok("Bravo", "duel", "2025-09-01", played)
    assert ok
    ok, _ = quota_ok("Bravo", "duel", "2025-08-15", played)
    assert not ok
    # Duel and brawl have separate quotas.
    ok, _ = quota_ok("Bravo", "brawl", "2025-08-15", played)
    assert ok


def test_hillfort_is_permanently_banned_in_duels():
    games = [{"map": "Hillfort", "commanders": {}, "outcome": {}}]
    chk = check_series(games, pool=["Hillfort", "Abyss"])
    assert not chk.ok
    assert any(v.rule == "map ban" for v in chk.violations)


def test_map_may_not_be_played_three_times():
    games = [{"map": "Abyss", "commanders": {}, "outcome": {}} for _ in range(3)]
    chk = check_series(games, pool=["Abyss"])
    assert not chk.ok
    assert any(v.rule == "map repeat" for v in chk.violations)
    # Twice is allowed.
    assert check_series(games[:2], pool=["Abyss"]).ok


def test_commander_burns_only_after_a_win():
    """A loss leaves it reusable — only a win uses the map up."""
    lost_then_reused = [
        {"map": "Abyss", "commanders": {"side1": "commander-da-builder"},
         "outcome": {"winner_side": 2}},
        {"map": "Vanilla", "commanders": {"side1": "commander-da-builder"},
         "outcome": {"winner_side": 2}},
    ]
    assert check_series(lost_then_reused, pool=["Abyss", "Vanilla"]).ok

    won_then_reused = [
        {"map": "Abyss", "commanders": {"side1": "commander-da-builder"},
         "outcome": {"winner_side": 1}},
        {"map": "Vanilla", "commanders": {"side1": "commander-da-builder"},
         "outcome": {"winner_side": 2}},
    ]
    chk = check_series(won_then_reused, pool=["Abyss", "Vanilla"])
    assert not chk.ok
    assert any(v.rule == "commander reuse" for v in chk.violations)


def test_brawl_uses_the_fpl_pool():
    """A ranked duel-pool map is illegal in a brawl, and vice versa."""
    chk = check_series([{"map": "Abyss", "commanders": {}, "outcome": {}}],
                       brawl=True)
    assert not chk.ok
    chk = check_series([{"map": "Caverns MS", "commanders": {}, "outcome": {}}],
                       brawl=True)
    assert chk.ok


def test_series_longer_than_bo9_is_rejected():
    games = [{"map": f"M{i}", "commanders": {}, "outcome": {}} for i in range(10)]
    chk = check_series(games)
    assert not chk.ok
    assert any(v.rule == "series length" for v in chk.violations)


def test_report_line_matches_the_prescribed_format():
    assert (report_line("duel", ["Alpha"], ["Bravo"], (2, 3))
            == "UFER Duel: Alpha vs Bravo 2-3")
    assert (report_line("brawl", ["Alpha", "Charlie"], ["Bravo", "Delta"], (3, 4))
            == "UFER Brawl: Alpha, Charlie vs Bravo, Delta 3-4")
    # The rule set makes the score optional.
    assert report_line("duel", ["A"], ["B"]) == "UFER Duel: A vs B"


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
