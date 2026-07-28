"""Tests for pick & ban.

Two things have to hold or the draft is worthless: it must be **fair** (both
sides ban and pick equally often) and it must be **enforceable** (a
divergence in the played match gets noticed).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.draft import (  # noqa: E402
    Action, Draft, Side, display_name, short_name,
)

POOL5 = ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals"]
POOL6 = POOL5 + ["Moorings"]
CMDS = [f"commander-x-{n}" for n in
        ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")]


def fresh(pool=None, best_of=3, first_ban=Side.A, bans=1, seed=1) -> Draft:
    return Draft(map_pool=list(pool or POOL5), commander_pool=list(CMDS),
                 best_of=best_of, commander_bans_per_side=bans,
                 first_ban=first_ban, strike_seed=seed)


def play_maps(d: Draft) -> None:
    """Work through every map step, always taking the first legal option."""
    while d.current and d.current.action in (Action.BAN_MAP, Action.PICK_MAP):
        d.apply(d.legal_options()[0])


def play_commander_bans(d: Draft) -> None:
    while d.current and d.current.action == Action.BAN_COMMANDER:
        d.apply(d.legal_options()[0])


# ---------------------------------------------------------------- Fairness
def test_both_sides_ban_and_pick_equally_often():
    """The core: no side advantage, in any combination.

    An earlier design let one side ban twice and the other once at six maps
    and Bo3. That is a real advantage — drawing lots for it does not make it
    smaller.
    """
    for pool in (POOL5, POOL6, POOL6 + ["Balls"]):
        for bo in (1, 3, 5):
            if len(pool) < bo:
                continue
            d = fresh(pool, best_of=bo)
            assert d.is_symmetric(), (
                f"{len(pool)} maps, Bo{bo}: "
                f"bans {d.ban_counts()}, picks {d.pick_counts()}")


def test_an_even_pool_loses_one_map_neutrally():
    """To make the ban count work out, one map is struck by lot — fair to
    both, unlike an extra ban for one of them."""
    d = fresh(POOL6, best_of=3)
    assert d.neutral_strike in POOL6
    assert len(d.map_pool) == 5
    assert d.is_symmetric()


def test_the_neutral_strike_is_reproducible():
    """Same seed, same map — the draw can be checked afterwards."""
    a = fresh(POOL6, seed=42).neutral_strike
    b = fresh(POOL6, seed=42).neutral_strike
    assert a == b
    assert fresh(POOL6, seed=7).neutral_strike is not None


def test_an_odd_pool_is_left_alone():
    assert fresh(POOL5, best_of=3).neutral_strike is None


def test_whoever_bans_first_picks_second():
    d = fresh(POOL5, best_of=3, first_ban=Side.A)
    bans = [s for s in d.steps if s.action == Action.BAN_MAP]
    picks = [s for s in d.steps if s.action == Action.PICK_MAP]
    assert bans[0].side is Side.A
    assert picks[0].side is Side.B, "whoever bans first must not also pick first"


# ------------------------------------------------------------------ Flow
def test_bo3_ends_with_three_maps_one_of_them_the_decider():
    d = fresh(POOL5, best_of=3)
    play_maps(d)
    plan = d.plan()
    assert len([g for g in plan if g["map"]]) == 3
    assert plan[-1]["decider"] and plan[-1]["map_picked_by"] is None
    assert plan[0]["map_picked_by"] == "B"
    assert plan[1]["map_picked_by"] == "A"


def test_a_side_cannot_act_out_of_turn():
    d = fresh(first_ban=Side.A)
    try:
        d.apply("Spirals", Side.B)
    except ValueError as e:
        assert "not to move" in str(e)
    else:
        raise AssertionError("a move out of turn was accepted")


def test_a_banned_map_cannot_be_chosen_again():
    d = fresh()
    first = d.legal_options()[0]
    d.apply(first)
    assert first not in d.legal_options()


def test_banned_commanders_are_gone_for_both_sides():
    d = fresh()
    play_maps(d)
    d.apply(CMDS[0]); d.apply(CMDS[1])
    for side in (Side.A, Side.B):
        assert CMDS[0] not in d.available_commanders_for(side)
        assert CMDS[1] not in d.available_commanders_for(side)


def test_commander_picks_are_blind_until_both_locked():
    d = fresh()
    play_maps(d); play_commander_bans(d)
    d.apply(CMDS[2], Side.A)
    assert d.plan()[0]["commander_a"] is None
    assert d.legal_options(Side.A) == [], "A must not get to revise"
    d.apply(CMDS[3], Side.B)
    assert d.plan()[0]["commander_a"] == CMDS[2]
    assert d.plan()[0]["commander_b"] == CMDS[3]


def test_a_winning_commander_is_burned_for_that_side_only():
    """Ladder rule: a win uses the map up, a loss does not."""
    d = fresh()
    play_maps(d); play_commander_bans(d)
    d.apply(CMDS[2], Side.A); d.apply(CMDS[3], Side.B)
    d.note_result(1, Side.A)
    assert CMDS[2] not in d.available_commanders_for(Side.A)
    assert CMDS[2] in d.available_commanders_for(Side.B)
    assert CMDS[3] in d.available_commanders_for(Side.B)


# ---------------------------------------------------------------- Names
def test_display_names_fall_back_readably():
    assert display_name("commander-zz-eigen-bau") == "Eigen Bau"
    assert short_name("commander-da-builder") == "builder"


# ---------------------------------------------------------------- Kontrolle
def test_verify_catches_a_different_map():
    d = fresh(best_of=1)
    play_maps(d); play_commander_bans(d)
    d.apply(CMDS[2], Side.A); d.apply(CMDS[3], Side.B)
    played_map = d.plan()[0]["map"]
    other = next(m for m in POOL5 if m != played_map)
    problems = d.verify([{"map": other,
                          "commanders": {"side1": CMDS[2], "side2": CMDS[3]}}])
    assert any(other in p and played_map in p for p in problems), problems


def test_verify_catches_a_different_commander():
    """The actual point: the log turns the draft into a rule."""
    d = fresh(best_of=1)
    play_maps(d); play_commander_bans(d)
    d.apply(CMDS[2], Side.A); d.apply(CMDS[3], Side.B)
    problems = d.verify([{"map": d.plan()[0]["map"],
                          "commanders": {"side1": CMDS[4], "side2": CMDS[3]}}])
    assert len(problems) == 1
    assert display_name(CMDS[4]) in problems[0]


def test_verify_is_quiet_when_everything_matches():
    d = fresh(best_of=1)
    play_maps(d); play_commander_bans(d)
    d.apply(CMDS[2], Side.A); d.apply(CMDS[3], Side.B)
    assert d.verify([{"map": d.plan()[0]["map"],
                      "commanders": {"side1": CMDS[2], "side2": CMDS[3]}}]) == []


def test_pool_too_small_is_refused_early():
    try:
        Draft(map_pool=["A", "B"], commander_pool=CMDS, best_of=5)
    except ValueError as e:
        assert "map pool" in str(e)
    else:
        raise AssertionError("an undersized pool was accepted")


def test_even_best_of_is_refused():
    try:
        Draft(map_pool=POOL5, commander_pool=CMDS, best_of=2)
    except ValueError:
        pass
    else:
        raise AssertionError("Bo2 was accepted")


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
