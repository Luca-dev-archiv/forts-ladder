"""Tests for the networked draft — mostly about what a player cannot see.

The rules themselves are covered by tests/test_draft.py. What is new once two
people are involved is confidentiality: in the local hot-seat draft "blind
commander pick" is a UI convention, because one person is looking at both
sides anyway. Over a network it has to be a property of the server, or it is
a promise the client makes and a debugger breaks.

So the load-bearing test here is that a pending pick never appears in the
opponent's state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.draft import Action, Side  # noqa: E402
from server.auth import AuthError, AuthService, Role  # noqa: E402
from server.draft import DraftService  # noqa: E402
from server.queue import QueueService  # noqa: E402

MAPS = ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals"]
CMDS = [f"commander-x-{n}" for n in
        ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")]


def two_players():
    auth = AuthService()
    out = []
    for i, name in enumerate(("A", "B"), start=1):
        a = auth.login_discord(str(i), name)
        a.role = Role.PLAYER
        auth.attach_steam(a, f"7656119900000{i:04d}")
        a.ufer_name = name
        auth.set_tracking_consent(a, True)
        out.append(a)
    return auth, out[0], out[1]


def started(step_seconds=None):
    """A draft with both seats filled. No clock unless a test wants one."""
    auth, a, b = two_players()
    svc = DraftService()
    s = svc.create(a, MAPS, CMDS, best_of=3, step_seconds=step_seconds)
    svc.join(b, s.join_code)
    return svc, s, a, b


def play_maps(s, a, b):
    """Work through the map steps, whoever is on turn."""
    while s.draft.current and s.draft.current.action in (
            Action.BAN_MAP, Action.PICK_MAP):
        side = s.draft.current.side
        actor = a if side is Side.A else b
        s.apply(actor, s.draft.legal_options(side)[0])


def play_commander_bans(s, a, b):
    while s.draft.current and s.draft.current.action is Action.BAN_COMMANDER:
        side = s.draft.current.side
        actor = a if side is Side.A else b
        s.apply(actor, s.draft.legal_options(side)[0])


# ------------------------------------------------------------ Confidentiality
#: Fields that legitimately list the whole commander pool, so every id appears
#: in them by definition. Excluded when searching for a leak — otherwise the
#: test trips over the pool listing and says nothing about confidentiality.
POOL_FIELDS = {"commander_names", "commander_pool", "options",
               "banned_commanders", "map_pool"}


def dynamic_state(view: dict) -> dict:
    return {k: v for k, v in view.items() if k not in POOL_FIELDS}


def test_a_pending_blind_pick_is_never_in_the_opponents_state():
    """The one that matters. If this fails, "blind" is decoration."""
    svc, s, a, b = started()
    play_maps(s, a, b)
    play_commander_bans(s, a, b)
    assert s.draft.current.action is Action.PICK_COMMANDER

    chosen = s.draft.legal_options(Side.A)[0]
    s.apply(a, chosen)

    view = s.public_state(b)
    assert chosen not in repr(dynamic_state(view)), \
        f"A's pick leaked into B's state: {dynamic_state(view)}"
    assert view["your_pending_pick"] is None
    assert all(g["commander_a"] is None for g in view["plan"])
    # B must still learn that A has committed — that is not exploitable, and
    # without it the UI cannot say what it is waiting for.
    assert view["locked_in"] == ["A"]
    # And B's own options must not have shrunk to reveal what A took.
    assert chosen in view["options"], \
        "removing A's pick from B's options would disclose it by elimination"


def test_your_own_pick_comes_back_to_you():
    """So the UI can show what you chose while you wait."""
    svc, s, a, b = started()
    play_maps(s, a, b)
    play_commander_bans(s, a, b)
    chosen = s.draft.legal_options(Side.A)[0]
    s.apply(a, chosen)
    assert s.public_state(a)["your_pending_pick"] == chosen


def test_both_picks_appear_only_after_both_locked_in():
    svc, s, a, b = started()
    play_maps(s, a, b)
    play_commander_bans(s, a, b)
    pick_a = s.draft.legal_options(Side.A)[0]
    s.apply(a, pick_a)
    assert all(g["commander_a"] is None for g in s.public_state(b)["plan"])

    pick_b = s.draft.legal_options(Side.B)[1]
    s.apply(b, pick_b)
    plan = s.public_state(b)["plan"]
    assert plan[0]["commander_a"] == pick_a
    assert plan[0]["commander_b"] == pick_b


def test_a_spectator_gets_no_side_and_no_options():
    svc, s, a, b = started()
    auth2 = AuthService()
    outsider = auth2.login_discord("9", "Nosy")
    view = s.public_state(outsider)
    assert view["your_side"] is None
    assert view["options"] == []
    assert view["your_pending_pick"] is None


# -------------------------------------------------------------------- Turns
def test_moving_out_of_turn_is_refused():
    svc, s, a, b = started()
    on_turn = s.draft.current.side
    wrong = b if on_turn is Side.A else a
    try:
        s.apply(wrong, s.draft.legal_options(on_turn)[0])
    except AuthError as e:
        assert "on turn" in str(e), str(e)
    else:
        raise AssertionError("a move out of turn was accepted")


def test_a_stranger_cannot_move_at_all():
    svc, s, a, b = started()
    auth2 = AuthService()
    outsider = auth2.login_discord("9", "Nosy")
    try:
        s.apply(outsider, MAPS[0])
    except AuthError as e:
        assert "not in this draft" in str(e), str(e)
    else:
        raise AssertionError("a stranger moved in someone else's draft")


def test_nothing_can_be_played_before_the_second_player_joins():
    auth, a, b = two_players()
    svc = DraftService()
    s = svc.create(a, MAPS, CMDS, step_seconds=None)
    try:
        s.apply(a, MAPS[0])
    except AuthError as e:
        assert "second player" in str(e), str(e)
    else:
        raise AssertionError("a one-sided draft accepted a move")


def test_locking_in_twice_for_the_same_game_is_refused():
    svc, s, a, b = started()
    play_maps(s, a, b)
    play_commander_bans(s, a, b)
    first = s.draft.legal_options(Side.A)[0]
    s.apply(a, first)
    # Passed literally: once locked in, `legal_options` for that side is empty
    # by design, so the value has to come from outside the engine.
    try:
        s.apply(a, first)
    except AuthError as e:
        assert "already locked in" in str(e), str(e)
    else:
        raise AssertionError("a side locked in twice")


# --------------------------------------------------------------------- Seats
def test_a_third_player_cannot_take_a_seat():
    svc, s, a, b = started()
    auth2 = AuthService()
    third = auth2.login_discord("9", "Third")
    third.role = Role.PLAYER
    try:
        svc.join(third, s.join_code)
    except AuthError as e:
        assert "two players" in str(e), str(e)
    else:
        raise AssertionError("a third player joined")


def test_rejoining_returns_your_existing_seat():
    """Reconnecting must not be treated as a new player."""
    svc, s, a, b = started()
    again = svc.join(b, s.join_code)
    assert again.id == s.id
    assert len(again.seats) == 2
    assert again.seat_of(b).side is Side.B


def test_an_unknown_join_code_is_refused():
    svc, s, a, b = started()
    try:
        svc.join(a, "ZZZZZZ")
    except AuthError as e:
        assert "no draft with that code" in str(e), str(e)
    else:
        raise AssertionError("an unknown code was accepted")


# --------------------------------------------------------------------- Clock
def test_the_deadline_is_evaluated_on_the_server():
    """A client that stops polling must not be able to freeze the draft."""
    svc, s, a, b = started(step_seconds=10)
    clock = [1000.0]
    s.draft._now = lambda: clock[0]
    s.draft.seconds_left()
    before = s.draft.step_index
    clock[0] += 60
    s.tick()
    assert s.draft.step_index > before, "an expired step was not resolved"


def test_stale_sessions_are_pruned():
    svc, s, a, b = started()
    assert svc.prune(max_age_s=0) == [s.id]
    assert svc.sessions == {}


# --------------------------------------------------------------------- Queue
def test_queueing_requires_consent():
    auth, a, b = two_players()
    q = QueueService(auth, DraftService())
    q.configure(MAPS, CMDS)
    auth.set_tracking_consent(a, False)
    try:
        q.join(a, 1500)
    except AuthError as e:
        assert "not agreed" in str(e), str(e)
    else:
        raise AssertionError("a non-consenting player joined the queue")


def test_queueing_without_configured_pools_is_refused():
    """Better an explicit refusal than a draft with an empty map pool."""
    auth, a, b = two_players()
    q = QueueService(auth, DraftService())
    try:
        q.join(a, 1500)
    except AuthError as e:
        assert "pools" in str(e), str(e)
    else:
        raise AssertionError("queued with no pools configured")


def test_two_accepting_players_get_one_shared_draft():
    auth, a, b = two_players()
    clock = [0.0]
    drafts = DraftService(now=lambda: clock[0])
    q = QueueService(auth, drafts, now=lambda: clock[0])
    q.configure(MAPS, CMDS)
    q.join(a, 1500)
    q.join(b, 1500)
    clock[0] += 1
    q.status(a)                      # ticking produces the proposal
    q.accept(a)
    st = q.accept(b)
    assert st["draft_id"], "no draft was created for an accepted proposal"
    assert q.status(a)["draft_id"] == q.status(b)["draft_id"]

    s = drafts.get(st["draft_id"])
    assert s.full()
    assert {x.side for x in s.seats.values()} == {Side.A, Side.B}


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
