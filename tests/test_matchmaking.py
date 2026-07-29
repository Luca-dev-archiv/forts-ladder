"""Tests for the queue and pairing.

Every time value is a parameter rather than a real clock, or these cases
would be either slow or irreproducible. What is checked here is exactly what
goes wrong in a small scene: nobody finds a game, or the same two players
keep finding each other.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.matchmaking import (  # noqa: E402
    ACCEPT_TIMEOUT_S, EntryState, PENALTY_BASE_S, PENALTY_FORGET_S,
    PENALTY_STEP_S, PairCap,
    Queue, allowed_gap,
)


def test_the_search_window_opens_with_waiting_time():
    assert allowed_gap(0) == 100
    assert allowed_gap(45) == 200
    assert allowed_gap(120) == 350
    assert allowed_gap(3600) == 10_000, "after a long wait, anything must pair"


def test_close_ratings_pair_immediately():
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1560, 0)
    made = q.tick(0)
    assert len(made) == 1 and set(made[0].players) == {"A", "B"}


def test_distant_ratings_only_pair_after_waiting():
    q = Queue()
    q.join("A", 1200, 0); q.join("B", 1700, 0)      # 500 points apart
    assert q.tick(0) == [], "paired too early"
    assert q.tick(60) == [], "still too early"
    assert len(q.tick(200)) == 1, "after three and a half minutes it must pair"


def test_both_must_accept_before_a_match_exists():
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0)
    assert q.accept("A", 1) is None, "one yes on its own is not enough"
    p = q.accept("B", 2)
    assert p is not None and set(p.players) == {"A", "B"}
    assert q.entries["A"].state is EntryState.PLAYING


def test_declining_costs_time_and_the_other_side_keeps_waiting():
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0)
    q.decline("A", 5)
    assert q.entries["A"].penalty_until == 5 + PENALTY_BASE_S
    assert q.entries["B"].penalty_until == 0, "the uninvolved player was penalised"
    assert q.entries["B"].joined_at == 0, "their wait time was reset"


def test_repeated_declining_gets_more_expensive():
    """Two minutes, then five, then eight. The weight belongs on the pattern,
    not on the one accident."""
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0); q.decline("A", 0)
    assert q.entries["A"].penalty_until == PENALTY_BASE_S
    q.entries["A"].penalty_until = 0            # lift the block for this test
    q.tick(1); q.decline("A", 1)
    assert q.entries["A"].penalty_until == 1 + PENALTY_BASE_S + PENALTY_STEP_S


def test_the_record_is_forgotten_after_a_clean_day():
    """Without a horizon the counter is a permanent mark for one bad evening
    months ago."""
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0); q.decline("A", 0)

    later = PENALTY_FORGET_S + 1
    q.entries["A"].penalty_until = 0
    q.join("A", 1500, later); q.join("B", 1520, later)
    q.tick(later); q.decline("A", later)
    assert q.entries["A"].penalty_until == later + PENALTY_BASE_S, \
        "a day-old offence still counted"


def test_the_record_survives_leaving_and_rejoining():
    """An Entry is created fresh on every join, so a counter living there could
    be reset by leaving the queue and coming back."""
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0); q.decline("A", 0)
    q.leave("A")
    q.join("A", 1500, 1)
    q.entries["A"].penalty_until = 0
    q.tick(1); q.decline("A", 1)
    assert q.entries["A"].penalty_until == 1 + PENALTY_BASE_S + PENALTY_STEP_S


def test_declining_and_sleeping_share_one_ledger():
    """To the player who was waiting they are the same event: the match did not
    happen. The difference matters only to the person who did it."""
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0)
    q.decline("A", 0)                              # first offence
    q.entries["A"].penalty_until = 0
    q.tick(1)
    q.accept("B", 1)
    q.tick(1 + ACCEPT_TIMEOUT_S + 1)               # A slept: second offence
    assert q.entries["A"].penalty_until == \
        1 + ACCEPT_TIMEOUT_S + 1 + PENALTY_BASE_S + PENALTY_STEP_S


def test_not_reacting_costs_the_same_as_declining():
    """Ten minutes for a missed offer was punishment rather than deterrence: in
    a scene this size it ends an evening, and it lands hardest on somebody whose
    game crashed while the offer was on screen."""
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0)
    q.accept("A", 1)
    q.tick(ACCEPT_TIMEOUT_S + 1)                # B was asleep
    assert q.entries["B"].penalty_until == ACCEPT_TIMEOUT_S + 1 + PENALTY_BASE_S
    assert q.entries["A"].penalty_until == 0, \
        "whoever accepted must not pay for the other one"
    assert q.entries["A"].state is EntryState.SEARCHING


def test_a_blocked_player_is_not_paired():
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1520, 0)
    q.tick(0); q.decline("A", 0)
    assert q.tick(10) == [], "a penalised player was paired again immediately"
    assert len(q.tick(PENALTY_BASE_S + 1)) == 1


def test_the_weekly_pair_cap_prevents_useless_matches():
    """Two players who have used up their quota against each other do not
    get paired — otherwise you queue ten minutes for a game that does not
    count."""
    cap = PairCap(limit=12)
    cap.note("A", "B", 12)
    q = Queue(pair_cap=cap)
    q.join("A", 1500, 0); q.join("B", 1510, 0)
    assert q.tick(0) == []
    # With a third player it works immediately.
    q.join("C", 1505, 0)
    assert len(q.tick(1)) == 1


def test_the_longest_waiting_player_is_served_first():
    q = Queue()
    q.join("Alt", 1500, 0)
    q.join("Neu1", 1505, 100)
    q.join("Neu2", 1495, 100)
    made = q.tick(110)
    assert "Alt" in made[0].players, "the longest waiter came away empty"


def test_leaving_removes_a_player_from_the_pool():
    q = Queue()
    q.join("A", 1500, 0); q.join("B", 1510, 0)
    q.leave("A")
    assert q.tick(0) == []
    assert all(s["player"] != "A" for s in q.status(0))


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
