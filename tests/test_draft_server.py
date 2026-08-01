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


def test_your_own_pick_comes_back_but_never_theirs_before_the_game():
    """Locking in together used to reveal both. It should not: game 1 is the
    game the blind pick is *for*, and you find out what you are against when it
    loads — not while there is still time to plan around it."""
    svc, s, a, b = started()
    play_maps(s, a, b)
    play_commander_bans(s, a, b)
    pick_a = s.draft.legal_options(Side.A)[0]
    s.apply(a, pick_a)
    assert all(g["commander_a"] is None for g in s.public_state(b)["plan"])

    pick_b = s.draft.legal_options(Side.B)[1]
    s.apply(b, pick_b)
    plan = s.public_state(b)["plan"]
    assert plan[0]["commander_b"] == pick_b, "own pick should come back"
    assert plan[0]["commander_a"] is None, "the opponent's pick was revealed"

    # Once the game has been played there is nothing left to protect.
    play_all(s, a, b)
    played(s, a, 1, "A")
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
        # The message counts the seats now, because a side can hold two.
        assert "all 2 players" in str(e), str(e)
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
def test_the_clock_does_not_run_before_an_opponent_joins():
    """The bug this exists for: `deadline()` starts the clock the first time it
    is asked, and `public_state` asked it. So watching a lobby while waiting for
    an opponent started the timer, and steps were then drawn by lot with nobody
    there to make them."""
    auth, a, b = two_players()
    clock = [1000.0]
    svc = DraftService(now=lambda: clock[0])
    s = svc.create(a, MAPS, CMDS, best_of=3, step_seconds=10)
    s.draft._now = lambda: clock[0]

    for _ in range(5):
        view = s.public_state(a)
        assert view["seconds_left"] is None, "the clock is running with one seat"
        clock[0] += 30
        assert s.tick() == [], "a step resolved itself with no opponent"
    assert s.draft.step_index == 0, "the draft advanced while waiting"
    assert not s.draft.banned_maps(), "a map was banned with nobody playing"


def test_the_first_step_gets_a_full_window_after_the_join():
    """Otherwise the clock someone started by looking at the lobby would eat
    into the first real decision."""
    auth, a, b = two_players()
    clock = [1000.0]
    svc = DraftService(now=lambda: clock[0])
    s = svc.create(a, MAPS, CMDS, best_of=3, step_seconds=10)
    s.draft._now = lambda: clock[0]
    s.public_state(a)          # would have started it before
    clock[0] += 300            # a long wait for the opponent
    svc.join(b, s.join_code)
    left = s.public_state(b)["seconds_left"]
    assert left is not None and left > 9, f"only {left}s left on the first step"



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
        assert "no map or commander pool" in str(e), str(e)
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


# ----------------------------------------------------------------- Modes
def queue_with(*players):
    auth = AuthService()
    out = []
    for i, name in enumerate(players, start=1):
        a = auth.login_discord(str(i), name)
        a.role = Role.PLAYER
        auth.attach_steam(a, f"7656119900000{i:04d}")
        auth.set_tracking_consent(a, True)
        out.append(a)
    clock = [0.0]
    drafts = DraftService(now=lambda: clock[0])
    q = QueueService(auth, drafts, now=lambda: clock[0])
    q.configure(MAPS, CMDS)
    return q, clock, out


def test_two_modes_never_pair_with_each_other():
    """One shared queue would hand a 1v1 player a 2v2 draft nobody asked for."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    q.join(b, 1500, "unranked_1v1")
    clock[0] += 5
    assert q.status(a)["proposal"] is None
    assert q.status(b)["proposal"] is None
    assert q.status(a)["mode"] == "ranked_1v1"
    assert q.status(b)["mode"] == "unranked_1v1"


def test_the_same_mode_does_pair():
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    q.join(b, 1500, "ranked_1v1")
    clock[0] += 5
    q.status(a)
    assert q.status(a)["proposal"] is not None


def test_switching_mode_leaves_the_previous_queue():
    """Standing in two queues means one offer lapses and earns a penalty for
    nothing."""
    q, clock, (a,) = queue_with("A")
    q.join(a, 1500, "ranked_1v1")
    q.join(a, 1500, "unranked_1v1")
    # `Queue.leave` marks the entry LEFT rather than deleting it, so the thing
    # to assert is that it is no longer *searchable* — checking the dict would
    # pass or fail for reasons unrelated to whether a pairing can happen.
    searching = {e.player for e in q.queues["ranked_1v1"].searching(clock[0])}
    assert a.id not in searching, "still searchable in the mode that was left"
    assert a.id in {e.player for e in q.queues["unranked_1v1"].searching(clock[0])}
    assert q.status(a)["mode"] == "unranked_1v1"


def test_a_team_mode_is_refused_with_a_reason():
    """Pairing a team mode needs whole sides matched, not two individuals — a
    queue that silently never resolves would be worse than a refusal."""
    q, clock, (a,) = queue_with("A")
    try:
        q.join(a, 1500, "ranked_2v2")
    except AuthError as e:
        assert "whole sides" in str(e), str(e)
    else:
        raise AssertionError("a team mode was queued")


def test_the_mode_list_marks_what_cannot_be_used():
    q, clock, (a,) = queue_with("A")
    modes = {m["key"]: m for m in q.modes()}
    assert modes["ranked_1v1"]["available"]
    assert not modes["ranked_2v2"]["available"]
    assert not any(k.startswith("tournament") for k in modes),         "tournament modes are entered through a bracket, not a queue"


def test_the_draft_uses_the_modes_best_of():
    """A Bo1 mode must not produce a Bo3 draft."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "unranked_1v1")
    q.join(b, 1500, "unranked_1v1")
    clock[0] += 5
    q.status(a); q.accept(a)
    st = q.accept(b)
    assert st["draft_id"]
    assert q.drafts.get(st["draft_id"]).draft.best_of == 1


# ------------------------------------------------------- Cancelling and lobby
def played(s, actor, game, winner="A"):
    """Report a game the way a current client does: with what was played.

    The map and the commanders are no longer optional. They used to be, and the
    checks read "if we were sent something, compare it" — so leaving them out
    skipped both the roster check and the plan check, and since the first report
    of a game wins, the losing client could report first with nothing attached.
    """
    want = s.draft.plan()[game - 1]
    ids = {seat.side.value: s._steam_ids.get(seat.account_id)
           for seat in s.seats.values()}
    commanders = {}
    for side in ("A", "B"):
        if ids.get(side) and want.get(f"commander_{side.lower()}"):
            commanders[ids[side]] = want[f"commander_{side.lower()}"]
    return s.note_game(actor, game, winner, map_played=want["map"],
                       commanders=commanders)


def play_all(s, a, b):
    """Run the draft to the end, whoever is on turn."""
    while s.draft.current is not None:
        step = s.draft.current
        if step.side is None:
            for side, actor in ((Side.A, a), (Side.B, b)):
                if s.draft.legal_options(side):
                    s.apply(actor, s.draft.legal_options(side)[0])
        else:
            actor = a if step.side is Side.A else b
            s.apply(actor, s.draft.legal_options(step.side)[0])


def test_either_side_can_cancel_and_both_are_told_who_did():
    """Deliberately not a delete: the other player is staring at a board, and
    "no such draft" is a worse answer than "the other side left"."""
    svc, s, a, b = started()
    s.cancel(b)
    for viewer in (a, b):
        st = s.public_state(viewer)
        assert st["cancelled"] is True
        assert st["cancelled_by"] == "B"


def test_a_cancelled_draft_accepts_no_more_moves():
    svc, s, a, b = started()
    s.cancel(a)
    try:
        s.apply(b, s.draft.legal_options(Side.A)[0])
    except AuthError as e:
        assert "left" in str(e), str(e)
    else:
        raise AssertionError("a move was accepted after the draft was cancelled")


def test_a_stranger_cannot_cancel_someone_elses_draft():
    svc, s, a, b = started()
    auth2, c, _ = two_players()
    try:
        s.cancel(c)
    except AuthError:
        pass
    else:
        raise AssertionError("an outsider cancelled a draft they are not in")
    assert not s.cancelled


def test_the_lobby_can_only_be_named_once_the_draft_is_finished():
    """Before that there is nothing to play, and the id decides which recorded
    games count — so it must not be pointed at a game that predates the plan."""
    svc, s, a, b = started()
    try:
        s.set_lobby(a, 109775241234567890)
    except AuthError as e:
        assert "not finished" in str(e), str(e)
    else:
        raise AssertionError("a lobby was accepted mid-draft")

    play_all(s, a, b)
    assert s.draft.done
    st = s.set_lobby(a, 109775241234567890)
    assert st["lobby_id"] == "109775241234567890"
    assert st["lobby_host"] == "A"
    # The other side sees it too — that is the entire point of the handoff.
    assert s.public_state(b)["lobby_id"] == "109775241234567890"


def test_the_lobby_cannot_be_repointed_at_a_different_game():
    svc, s, a, b = started()
    play_all(s, a, b)
    s.set_lobby(a, 111)
    s.set_lobby(a, 111)              # the host saying it again is harmless
    try:
        s.set_lobby(a, 222)
    except AuthError as e:
        assert "already in lobby" in str(e), str(e)
    else:
        raise AssertionError("the lobby id was overwritten")


def test_hosting_is_assigned_not_raced_for():
    """Both clients used to show "I am hosting" until somebody pressed it, which
    is two people about to open the same match — and whoever pressed second got
    an error for doing what the screen invited. Side A hosts."""
    svc, s, a, b = started()
    assert s.public_state(a)["lobby_host"] is None, "nothing to host yet"
    play_all(s, a, b)
    assert s.public_state(a)["lobby_host"] == "A"
    assert s.public_state(b)["lobby_host"] == "A"
    # And the guest is given a join target straight away.
    assert s.public_state(b)["lobby_host_steam"] == a.steam_id


def test_the_other_side_can_take_hosting_over_until_a_lobby_exists():
    """The assigned host sometimes cannot host — no port forwarding, a bad line
    — and then the series must not be stuck. Once a lobby is open it is too
    late: taking over would send the other side somewhere pointless."""
    svc, s, a, b = started()
    play_all(s, a, b)
    s.claim_host(b)
    assert s.public_state(a)["lobby_host"] == "B"
    s.set_lobby(b, 555)
    try:
        s.claim_host(a)
    except AuthError as e:
        assert "already opened a lobby" in str(e), str(e)
    else:
        raise AssertionError("hosting changed after a lobby was open")


def test_the_guest_cannot_name_the_lobby():
    svc, s, a, b = started()
    play_all(s, a, b)
    try:
        s.set_lobby(b, 999)
    except AuthError as e:
        assert "hosting this series" in str(e), str(e)
    else:
        raise AssertionError("the guest named the lobby")


def test_the_host_steam_id_is_published_so_the_other_side_can_join():
    """Steam's join URL wants the lobby owner's account. Passing zero and
    letting Steam work it out did not join."""
    svc, s, a, b = started()
    play_all(s, a, b)
    s.claim_host(a)
    assert s.public_state(b)["lobby_host_steam"] == a.steam_id


# ------------------------------------------------- Revealing one game at a time
def test_the_opponents_later_commanders_stay_hidden():
    """The blind pick decides every game up front. Revealing all of them when
    the draft ends hands over game 2 and game 3 before game 1 is played, which
    is worse than having no blind pick at all."""
    svc, s, a, b = started()
    play_all(s, a, b)
    assert s.draft.done

    view = s.public_state(a)
    assert view["revealed_through"] == 1
    for i, game in enumerate(view["plan"], start=1):
        assert game["commander_a"] is not None, f"own pick hidden in game {i}"
        assert game["commander_b"] is None, f"the opponent's game {i} was shown"

    # And mirrored for the other side.
    for game in s.public_state(b)["plan"]:
        assert game["commander_b"] is not None
        assert game["commander_a"] is None


def test_reporting_a_game_opens_that_game_and_no_further():
    svc, s, a, b = started()
    play_all(s, a, b)
    played(s, a, 1, "A")
    view = s.public_state(b)
    assert view["revealed_through"] == 2
    assert view["plan"][0]["commander_a"] is not None, "the played game stayed hidden"
    assert view["plan"][1]["commander_a"] is None, "game 2 opened before it was played"
    assert view["wins"] == {"A": 1, "B": 0}
    assert view["series_over"] is False


def test_a_series_ends_at_two_wins_not_after_three_games():
    svc, s, a, b = started()
    play_all(s, a, b)
    played(s, a, 1, "A")
    played(s, b, 2, "A")
    st = s.public_state(a)
    assert st["wins"] == {"A": 2, "B": 0}
    assert st["series_over"] is True


def test_games_cannot_be_reported_out_of_order():
    """Reporting game 3 first would open its commanders while games 1 and 2 are
    still unplayed — the very reveal this protects."""
    svc, s, a, b = started()
    play_all(s, a, b)
    try:
        played(s, a, 3, "A")
    except AuthError as e:
        assert "game 1 is the one being played" in str(e), str(e)
    else:
        raise AssertionError("a later game was reported first")


def test_the_same_game_reported_twice_counts_once():
    """Both clients report from their own log, so the second arrival is
    expected."""
    svc, s, a, b = started()
    play_all(s, a, b)
    played(s, a, 1, "A")
    played(s, b, 1, "A")
    assert s.public_state(a)["wins"] == {"A": 1, "B": 0}


def test_a_spectator_sees_only_what_has_been_played():
    svc, s, a, b = started()
    play_all(s, a, b)
    outsider = None
    plan = s.public_state(outsider)["plan"]
    assert plan[0]["commander_a"] is None, "an outsider saw game 1 before it ran"
    played(s, a, 1, "A")
    plan = s.public_state(outsider)["plan"]
    assert plan[0]["commander_a"] is not None, "a played game stayed hidden"
    assert plan[1]["commander_a"] is None


def test_the_lobby_id_stays_a_string_in_the_state():
    """A Steam lobby id does not survive a double: JavaScript and anything else
    parsing JSON numbers would round it, and a rounded id matches no game."""
    svc, s, a, b = started()
    play_all(s, a, b)
    st = s.set_lobby(a, 109775243190123456)
    assert isinstance(st["lobby_id"], str)
    assert st["lobby_id"] == "109775243190123456"


# ------------------------------------------------------------------- Voiding
def test_voiding_a_game_needs_both_sides():
    """One-sided is exactly the claim a losing player has an interest in making
    alone, so one vote changes nothing."""
    svc, s, a, b = started()
    play_all(s, a, b)
    played(s, a, 1, "A")
    assert s.public_state(a)["wins"] == {"A": 1, "B": 0}

    st = s.request_void(a, "game:1", "crashed")
    assert st["void_requests"]["A"]["scope"] == "game:1"
    assert st["voided_games"] == [], "one side voided a game on its own"
    assert st["wins"] == {"A": 1, "B": 0}

    st = s.request_void(b, "game:1", "yes, crashed")
    assert st["voided_games"] == [1]
    assert st["wins"] == {"A": 0, "B": 0}, "the voided game still counted"
    assert st["void_requests"] == {}, "the votes should be spent"


def test_a_voided_game_is_played_again_under_the_same_number():
    svc, s, a, b = started()
    play_all(s, a, b)
    played(s, a, 1, "A")
    s.request_void(a, "game:1"); s.request_void(b, "game:1")
    # Game 1 is the one being played again, not game 2.
    st = played(s, b, 1, "B")
    assert st["wins"] == {"A": 0, "B": 1}


def test_voiding_a_game_gives_the_commander_back():
    """A win spends the commander. If the win did not happen, it was not
    spent."""
    svc, s, a, b = started()
    play_all(s, a, b)
    won_with = next(c.value for c in s.draft.choices
                    if c.action is Action.PICK_COMMANDER and c.side is Side.A
                    and c.game == 1)
    played(s, a, 1, "A")
    assert won_with in s.draft.burned(Side.A)
    s.request_void(a, "game:1"); s.request_void(b, "game:1")
    assert won_with not in s.draft.burned(Side.A)


def test_voiding_the_series_stops_it_being_reported_at_all():
    svc, s, a, b = started()
    play_all(s, a, b)
    played(s, a, 1, "A")
    s.request_void(a, "series", "wrong commander loaded")
    assert s.public_state(b)["voided"] is False
    s.request_void(b, "series")
    assert s.public_state(b)["voided"] is True
    try:
        played(s, a, 2, "A")
    except AuthError as e:
        assert "voided" in str(e), str(e)
    else:
        raise AssertionError("a voided series accepted another result")


def test_the_other_side_sees_what_was_asked_for_and_why():
    """A request nobody can see is not a request."""
    svc, s, a, b = started()
    play_all(s, a, b)
    s.request_void(a, "game:1", "host crashed at 3:20")
    seen = s.public_state(b)["void_requests"]
    assert seen["A"]["scope"] == "game:1"
    assert "crashed" in seen["A"]["reason"]


def test_a_vote_can_be_taken_back_and_replaced():
    svc, s, a, b = started()
    play_all(s, a, b)
    s.request_void(a, "series")
    s.withdraw_void(a)
    assert s.public_state(a)["void_requests"] == {}

    # And asking for something else replaces the earlier ask rather than adding
    # to it: one vote, not a collection.
    s.request_void(a, "series")
    s.request_void(a, "game:1")
    assert s.public_state(a)["void_requests"]["A"]["scope"] == "game:1"
    s.request_void(b, "series")
    assert s.public_state(a)["voided"] is False,         "two different asks were treated as agreement"


def test_a_nonsense_scope_is_refused():
    svc, s, a, b = started()
    play_all(s, a, b)
    for scope in ("", "everything", "game:0", "game:99", "game:x"):
        try:
            s.request_void(a, scope)
        except AuthError:
            pass
        else:
            raise AssertionError(f"{scope!r} was accepted as a scope")


def test_a_queue_match_records_both_steam_ids():
    """The queue seats the second player without going through `join`, which is
    where a Steam ID is normally recorded. Without it the guest has no join
    target the moment side B hosts."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    q.join(b, 1500, "ranked_1v1")
    clock[0] += 5
    q.status(a); q.accept(a)
    st = q.accept(b)
    s = q.drafts.get(st["draft_id"])
    assert s._steam_ids.get(a.id) == a.steam_id
    assert s._steam_ids.get(b.id) == b.steam_id, "side B was seated without one"


def test_a_queue_match_hides_the_opponents_name_until_the_picking_is_done():
    """Knowing who you are against changes how you ban, and a queue match should
    be decided by the board. A draft somebody hosted with a code is exempt —
    they invited a specific person."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    q.join(b, 1500, "ranked_1v1")
    clock[0] += 5
    q.status(a); q.accept(a)
    st = q.accept(b)
    s = q.drafts.get(st["draft_id"])

    seen = s.public_state(a)["seats"]
    assert seen["A"] == "A", "your own name should be your own"
    assert seen["B"] == "Opponent", seen

    play_all(s, a, b)
    assert s.public_state(a)["seats"]["B"] == "B",         "the name should appear once the picking is over"


# ------------------------------------------------- Who is actually in the lobby
def test_somebody_else_in_one_of_the_seats_aborts_the_series():
    """The draft binds two accounts with proven Steam IDs. Somebody else playing
    in one of those places — a friend at the keyboard, a second account, the
    wrong lobby — cannot be rated as if the drafted pair had played."""
    svc, s, a, b = started()
    play_all(s, a, b)
    # B did not play; somebody else did.
    st = s.note_game(a, 1, "A", steam_ids=[a.steam_id, "76561199000009999"])
    assert st["aborted"] is True
    assert st["aborted_side"] == "B", "the absent side should be named"
    assert "different Steam account" in st["aborted_reason"], st["aborted_reason"]
    assert st["wins"] == {"A": 0, "B": 0}, "the game was counted anyway"


def test_an_extra_player_in_the_roster_is_not_fraud():
    """A spectator connects as a client and the log lists them like everybody
    else. Treating an unknown id as fraud made admitting a caster abort the
    series, which is two of this project's own features contradicting each
    other."""
    svc, s, a, b = started()
    play_all(s, a, b)
    want = s.draft.plan()[0]
    st = s.note_game(
        a, 1, "A", steam_ids=[a.steam_id, b.steam_id, "76561199000009999"],
        map_played=want["map"],
        commanders={a.steam_id: want["commander_a"],
                    b.steam_id: want["commander_b"]})
    assert st["aborted"] is False, st["aborted_reason"]
    assert st["wins"] == {"A": 1, "B": 0}


def test_the_side_that_reported_it_is_not_the_side_blamed():
    """The reporter is usually the one who noticed."""
    svc, s, a, b = started()
    play_all(s, a, b)
    st = s.note_game(b, 1, "B", steam_ids=[b.steam_id, "76561199000009999"])
    assert st["aborted_side"] == "A"


def test_the_expected_roster_is_accepted():
    svc, s, a, b = started()
    play_all(s, a, b)
    want = s.draft.plan()[0]
    st = s.note_game(a, 1, "A", steam_ids=[a.steam_id, b.steam_id],
                     map_played=want["map"],
                     commanders={a.steam_id: want["commander_a"],
                                 b.steam_id: want["commander_b"]})
    assert st["aborted"] is False
    assert st["wins"] == {"A": 1, "B": 0}


def test_an_aborted_series_takes_no_further_results():
    svc, s, a, b = started()
    play_all(s, a, b)
    # Somebody else in B's seat. A roster that is merely *short* is a player who
    # quit before the game was recorded, which is a replay rather than an abort.
    s.note_game(a, 1, "A",
                steam_ids=[a.steam_id, "76561199000003333"])
    try:
        s.note_game(b, 1, "B", steam_ids=[a.steam_id, b.steam_id])
    except AuthError as e:
        assert "aborted" in str(e), str(e)
    else:
        raise AssertionError("an aborted series accepted a result")


def test_both_sides_are_told_which_one_it_was():
    """The other side did nothing wrong, and a shared "aborted" would read as a
    shared fault."""
    svc, s, a, b = started()
    play_all(s, a, b)
    # Somebody else in A's seat, which is what aborting is for.
    s.note_game(b, 1, "B",
                steam_ids=[b.steam_id, "76561199000003333"])
    assert s.public_state(a)["aborted_side"] == "A"
    assert s.public_state(b)["aborted_side"] == "A"


def test_no_roster_no_abort():
    """A client that sends nothing must not be treated as sending strangers, or
    an older version would abort every series it reported."""
    svc, s, a, b = started()
    play_all(s, a, b)
    st = played(s, a, 1, "A")
    assert st["aborted"] is False
    assert st["wins"] == {"A": 1, "B": 0}


def test_accounts_without_a_linked_steam_id_are_not_punished_for_it():
    """Nothing to compare against is not evidence of anything."""
    svc, s, a, b = started()
    s._steam_ids.clear()
    play_all(s, a, b)
    st = s.note_game(a, 1, "A", steam_ids=["76561199000009999"])
    assert st["aborted"] is False


# ---------------------------------------------------------- The lobby password
def test_the_password_reaches_the_other_player():
    """Steam's join link has no field for it and the game asks on entry, so
    without this the guest was sent to a prompt for something only the host
    knew."""
    svc, s, a, b = started()
    play_all(s, a, b)
    s.set_lobby(a, 4242, password="AB3KM")
    assert s.public_state(b)["lobby_password"] == "AB3KM"
    assert s.public_state(a)["lobby_password"] == "AB3KM"


def test_the_password_is_not_handed_to_anyone_else():
    """It keeps strangers out of the lobby, which is the only thing it is for."""
    svc, s, a, b = started()
    play_all(s, a, b)
    s.set_lobby(a, 4242, password="AB3KM")
    assert s.public_state(None)["lobby_password"] is None


# ------------------------------------------------------- The end of a series
# Three holes of the same shape: a draft could be walked out of for free, it had
# no way of being *finished*, and neither of those stopped anyone queueing
# again — so the loser of game 1 could leave, queue instantly, and the other
# player was left in front of a board that would never move.
def queue_match():
    """A pairing made by the queue, drafted to the end.

    Deliberately through `QueueService`, not `DraftService.create`: what
    distinguishes a queue match from two friends with a join code is the thing
    the penalty hangs on.
    """
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500)
    q.join(b, 1500)
    clock[0] += 1
    q.status(a)
    q.accept(a)
    st = q.accept(b)
    s = q.drafts.get(st["draft_id"])
    play_all(s, a, b)
    return q, clock, s, a, b


def test_a_decided_series_can_be_closed_out():
    q, clock, s, a, b = queue_match()
    played(s, a, 1, "A")
    assert not s.can_conclude(), "closed out a series after one game of three"
    played(s, a, 2, "A")
    assert s.can_conclude(), "a 2:0 Bo3 is not offering to be closed"
    st = s.conclude(b)                        # either side may
    assert st["concluded"] and st["settled"]


def test_an_undecided_series_cannot_be_closed_out():
    """Otherwise the way out of a match you are losing is to declare it over."""
    q, clock, s, a, b = queue_match()
    played(s, a, 1, "A")
    try:
        s.conclude(a)
    except AuthError as e:
        assert "not decided" in str(e), str(e)
    else:
        raise AssertionError("a 1:0 Bo3 was closed out")


def test_a_live_series_keeps_both_players_out_of_the_queue():
    q, clock, s, a, b = queue_match()
    for who in (a, b):
        try:
            q.join(who, 1500)
        except AuthError as e:
            assert s.id in str(e), str(e)
        else:
            raise AssertionError("queued while a series was still being played")


def test_closing_the_series_frees_both_to_queue_again():
    q, clock, s, a, b = queue_match()
    played(s, a, 1, "A")
    played(s, a, 2, "A")
    s.conclude(a)
    q.join(a, 1500)
    q.join(b, 1500)               # refuses by raising, so reaching here is the
    assert q.status(a)["in_queue"]                                # whole test


def test_a_voided_series_frees_them_too():
    """A void is an agreement that it did not happen, so it cannot also be the
    thing that keeps them from playing."""
    q, clock, s, a, b = queue_match()
    s.request_void(a, "series", "crash")
    s.request_void(b, "series", "crash")
    q.join(a, 1500)
    assert q.status(a)["in_queue"]


def test_walking_out_of_a_live_queue_match_costs_a_cooldown():
    q, clock, s, a, b = queue_match()
    assert s.is_dodge(a), "leaving a live queue match counted as harmless"
    q.note_dodge(a)
    s.cancel(a)
    try:
        q.join(a, 1500)
    except AuthError as e:
        assert "left a series early" in str(e), str(e)
    else:
        raise AssertionError("a dodger queued again immediately")
    # The other player is not punished for being left.
    q.join(b, 1500)
    assert q.status(b)["in_queue"]


def test_the_second_dodge_costs_more_than_the_first():
    q, clock, s, a, b = queue_match()
    first = q.note_dodge(a) - clock[0]
    q.cooldowns.clear()
    second = q.note_dodge(a) - clock[0]
    assert second > first, f"{second}s is no worse than {first}s"


def test_leaving_a_private_draft_costs_nothing():
    """Two people who arranged a match between themselves may also call it off
    between themselves."""
    svc, s, a, b = started()
    play_all(s, a, b)
    assert not s.is_dodge(a)


def test_leaving_after_the_other_side_already_left_costs_nothing():
    q, clock, s, a, b = queue_match()
    s.cancel(b)
    assert not s.is_dodge(a), "punished for leaving a draft nobody was in"


def test_the_way_back_to_a_live_series_survives_leaving_the_queue():
    """The client leaves the queue the moment a draft starts, which used to take
    the draft id with it — and with it the way back to your own board."""
    q, clock, s, a, b = queue_match()
    q.leave(a)
    st = q.status(a)
    assert st["draft_id"] == s.id, st["draft_id"]
    assert st["blocked_by_series"] == s.id


def test_a_series_nobody_ever_finished_stops_blocking_the_queue():
    """The bug this rule could have caused, tested rather than argued about.

    Nothing prunes drafts, a client can be closed mid-series and a server can be
    redeployed — so without a cutoff an unfinished draft would keep a player out
    of the queue permanently, which is worse than the dodging it prevents.
    """
    q, clock, s, a, b = queue_match()
    assert q.open_series(a) is s
    clock[0] += q.SERIES_BLOCK_MAX_S + 1
    assert q.open_series(a) is None
    q.join(a, 1500)
    assert q.status(a)["in_queue"]


# ------------------------------------------------- Closing the client
# An entry nobody is asking about is nobody at the keyboard. Before the sweep,
# closing the client left the account in the queue for good: it kept being
# paired, and every pairing burned a whole accept window of somebody who *was*
# there.
def test_a_client_that_stops_asking_leaves_the_queue():
    """The poller is in a *different* mode on purpose.

    Two accounts in one queue are paired the instant either of them polls, which
    would measure the pairing rather than the sweep. A searcher in another mode
    ticks every queue without ever being matched against this one.
    """
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    q.join(b, 1500, "unranked_1v1")

    # A closes the client. B keeps looking, and B's poll is what sweeps.
    clock[0] += q.STALE_AFTER_S + 1
    q.status(b)
    assert a.id not in q.joined, "a closed client stayed in the queue"
    assert q.status(a)["in_queue"] is False
    # And is free to come straight back: closing a client is not an offence.
    q.join(a, 1500, "ranked_1v1")
    assert q.status(a)["in_queue"]


def test_polling_keeps_you_in_the_queue():
    """The other half, or the sweep would throw out the people who are there."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    q.join(b, 1500, "unranked_1v1")
    for _ in range(5):
        clock[0] += q.STALE_AFTER_S - 1
        q.status(a)
        q.status(b)
    assert a.id in q.joined and b.id in q.joined


def test_a_pending_proposal_is_not_swept():
    """A match offer expires on its own, with a penalty that is the queue's own
    business — a sweep must not pre-empt either."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500)
    q.join(b, 1500)
    assert q.status(a)["proposal"] is not None, "no proposal to test with"
    clock[0] += q.STALE_AFTER_S + 1
    q.status(a)
    assert b.id in q.joined, "an entry holding a live offer was swept"


def test_presence_counts_clients_inside_the_window():
    from server.presence import Presence
    clock = [1000.0]
    p = Presence(now=lambda: clock[0])
    p.seen("one")
    p.seen("two")
    assert p.online() == 2
    clock[0] += p.WINDOW_S + 1
    assert p.online() == 0, "somebody who stopped asking is still counted"
    assert p.last_seen == {}, "the dictionary grows for every account ever seen"


def test_presence_forgets_a_logout_at_once():
    """Logging out is a statement, not a timeout."""
    from server.presence import Presence
    p = Presence()
    p.seen("one")
    p.gone("one")
    assert p.online() == 0
    assert not p.is_online("one")


# ------------------------------------------------------- The handoff clock
# Between a finished draft and a running game there was no clock at all: one
# side could simply not host, and the other had nothing to do but sit there —
# held out of the queue, and charged a cooldown for leaving somebody else's
# silence.
def handing_off():
    """A queue match, drafted, with a controllable wall clock."""
    q, clock, s, a, b = queue_match()
    stamp = [1000.0]
    s._now = lambda: stamp[0]
    # The draft finished before the clock was swapped in, so re-stamp it.
    s.done_at = stamp[0]
    return s, a, b, stamp


def test_the_host_has_three_minutes_to_open_the_lobby():
    s, a, b, stamp = handing_off()
    h = s.handoff()
    assert h["phase"] == "host" and h["on"] == "A", h
    assert h["seconds_left"] == 180 and not h["expired"]

    stamp[0] += 179
    assert s.handoff()["seconds_left"] == 1
    stamp[0] += 2
    assert s.handoff()["expired"] and s.late_side() == "A"


def test_naming_the_lobby_starts_the_guests_clock():
    s, a, b, stamp = handing_off()
    stamp[0] += 60
    s.set_lobby(a, 4242, password="AB3KM")
    h = s.handoff()
    assert h["phase"] == "guest" and h["on"] == "B", h
    assert h["seconds_left"] == 180, "the guest inherited what the host had left"


def test_the_guest_reporting_in_stops_the_clock():
    s, a, b, stamp = handing_off()
    s.set_lobby(a, 4242)
    s.note_ready(b)
    h = s.handoff()
    assert h["phase"] == "playing" and h["seconds_left"] is None
    assert s.late_side() is None


def test_both_sides_are_told_the_same_number():
    """The whole reason the clock is on the server: two clients counting their
    own would disagree about when it ran out."""
    s, a, b, stamp = handing_off()
    stamp[0] += 42
    assert (s.public_state(a)["handoff"]["seconds_left"]
            == s.public_state(b)["handoff"]["seconds_left"])


def test_two_extra_minutes_can_be_asked_for_and_granted():
    s, a, b, stamp = handing_off()
    stamp[0] += 170
    s.ask_extension(a)
    assert s.public_state(b)["extension_asked_by"] == "A"
    s.grant_extension(b)
    assert s.handoff()["seconds_left"] == 180 + 120 - 170
    assert s.public_state(a)["extension_asked_by"] is None


def test_nobody_grants_themselves_more_time():
    s, a, b, stamp = handing_off()
    s.ask_extension(a)
    try:
        s.grant_extension(a)
    except AuthError as e:
        assert "other side" in str(e), str(e)
    else:
        raise AssertionError("a player extended their own deadline")


def test_whoever_was_kept_waiting_leaves_for_free():
    """Charging them for the other side's silence would be exactly backwards,
    and it is the reason the handoff needed a clock."""
    s, a, b, stamp = handing_off()
    stamp[0] += 200                       # A never opened a lobby
    assert s.late_side() == "A"
    assert s.is_dodge(b) is False, "the waiting player was charged"
    assert s.is_dodge(a) is True, "the late player got away with it"


def test_a_running_clock_still_costs_the_leaver():
    s, a, b, stamp = handing_off()
    stamp[0] += 10
    assert s.is_dodge(a) and s.is_dodge(b)


def test_leaving_the_queue_answers_with_the_new_count():
    """The client draws the mode picker from this answer. Throwing it away is
    what left "1 waiting" on screen after the last searcher left."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    assert q.status(a)["waiting"]["ranked_1v1"] == 1
    left = q.leave(a)
    assert left["waiting"].get("ranked_1v1", 0) == 0, left["waiting"]
    assert left["in_queue"] is False


def test_the_counts_are_available_without_a_whole_status():
    """An idle client asks for nothing else, so this is what keeps the picker
    honest while nobody is queueing."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500, "ranked_1v1")
    assert q.waiting()["ranked_1v1"] == 1
    # And it sweeps: asking is exactly the moment a closed client should stop
    # being counted.
    clock[0] += q.STALE_AFTER_S + 1
    assert q.waiting()["ranked_1v1"] == 0


# --------------------------------------- A game that was not the drafted game
# From a playtest: the last game of a series was played with the wrong commander
# and the series was rated anyway. The client compared what was played against
# the plan, printed a warning, and reported the result regardless — and it could
# only ever check its *own* side, because the opponent's commander is withheld
# until the game is over. So the one client able to catch it was the one that had
# got it wrong.
def played_series():
    """A drafted queue match, with the plan and both Steam IDs in hand."""
    q, clock, s, a, b = queue_match()
    plan = s.draft.plan()
    ids = {"A": s._steam_ids[a.id], "B": s._steam_ids[b.id]}
    return s, a, b, plan, ids


def test_the_drafted_game_is_counted():
    s, a, b, plan, ids = played_series()
    want = plan[0]
    st = s.note_game(a, 1, "A", map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"]})
    assert st["games_played"] == [1], st["games_played"]
    assert st["deviations"] == {}


def test_a_wrong_commander_is_not_counted_and_says_which_side():
    s, a, b, plan, ids = played_series()
    want = plan[0]
    wrong = next(c for c in s.draft.commander_pool
                 if c != want["commander_b"])
    st = s.note_game(a, 1, "A", map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: wrong})
    assert st["games_played"] == [], "a wrong-commander game was counted"
    assert "1" in st["deviations"], st["deviations"]
    why = " ".join(st["deviations"]["1"])
    assert "side B" in why and wrong in why, why
    # Put back for a replay under the same number, not thrown away: the series
    # is not over, that game just has not happened.
    assert 1 in set(st["voided_games"])
    assert st["revealed_through"] == 1


def test_the_replay_counts_and_clears_the_notice():
    s, a, b, plan, ids = played_series()
    want = plan[0]
    wrong = next(c for c in s.draft.commander_pool if c != want["commander_b"])
    s.note_game(a, 1, "A", map_played=want["map"],
                commanders={ids["A"]: want["commander_a"], ids["B"]: wrong})
    st = s.note_game(a, 1, "A", map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"]})
    assert st["games_played"] == [1], st["games_played"]
    assert st["deviations"] == {}
    assert 1 not in set(st["voided_games"])


def test_the_wrong_map_is_not_counted_either():
    s, a, b, plan, ids = played_series()
    want = plan[0]
    st = s.note_game(a, 1, "A", map_played="Somewhere Else",
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"]})
    assert st["games_played"] == []
    assert "map" in " ".join(st["deviations"]["1"])


def test_the_winner_is_taken_from_the_steam_id_not_the_side_number():
    """Forts swaps sides between games, so the log's "side 1 won" cannot be
    turned into a drafted side without knowing who side 1 was."""
    s, a, b, plan, ids = played_series()
    want = plan[0]
    # The client's fallback says A won; the Steam ID says B did. B must win.
    st = s.note_game(a, 1, "A", map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"]},
                     winner_steam=ids["B"])
    assert st["wins"] == {"A": 0, "B": 1}, st["wins"]


def test_a_stranger_in_the_commander_list_is_not_this_check_s_business():
    """Somebody who did not draft is the roster check's problem, and that one
    aborts rather than asking for a replay."""
    s, a, b, plan, ids = played_series()
    want = plan[0]
    st = s.note_game(a, 1, "A", map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"],
                                 "76561199000000999": "commander-x-alpha"})
    assert st["games_played"] == [1], st["deviations"]


def test_reporting_without_evidence_is_not_counted():
    """The reverse of what this used to assert, and the reason it changed.

    The map and the commanders were optional, and the checks read "if we were
    sent something, compare it" — so leaving them out skipped the roster check
    and the plan check together. Since the first report of a game wins, the
    losing client could report first with nothing attached and be recorded as the
    winner. Absence of evidence is not evidence of a fair game.
    """
    s, a, b, plan, ids = played_series()
    st = s.note_game(a, 1, "A")
    assert st["games_played"] == [], "an unverified report was counted"
    assert "no evidence" in " ".join(st["deviations"]["1"])


# ------------------------------------------------------- Two players a side
# `seats` was a flat map and `full()` was "two of them" — a 1v1 assumption in the
# middle of a project whose rule set has 2v2 modes.
def four_players():
    from server.auth import AuthService, Role
    auth = AuthService()
    out = []
    for i, name in enumerate(("A1", "A2", "B1", "B2"), start=1):
        a = auth.login_discord(f"t{i}", name)
        a.role = Role.PLAYER
        auth.attach_steam(a, f"7656119900000{100 + i:04d}")
        a.ufer_name = name
        auth.set_tracking_consent(a, True)
        out.append(a)
    return auth, out


def test_a_2v2_draft_waits_for_four_and_fills_a_before_b():
    auth, (a1, a2, b1, b2) = four_players()
    svc = DraftService()
    s = svc.create(a1, MAPS, CMDS, best_of=3, team_size=2)
    assert not s.full(), "a 2v2 was full with one player in it"

    svc.join(a2, s.join_code)
    assert not s.full()
    assert {x.side for x in s.seats.values()} == {Side.A}, \
        "the second player should complete side A, not open side B"

    svc.join(b1, s.join_code)
    svc.join(b2, s.join_code)
    assert s.full()
    sides = [x.side for x in s.seats.values()]
    assert sides.count(Side.A) == 2 and sides.count(Side.B) == 2


def test_a_fifth_player_is_refused_with_the_number():
    auth, (a1, a2, b1, b2) = four_players()
    svc = DraftService()
    s = svc.create(a1, MAPS, CMDS, best_of=3, team_size=2)
    for p in (a2, b1, b2):
        svc.join(p, s.join_code)
    extra = auth.login_discord("t9", "Spare")
    from server.auth import Role
    extra.role = Role.PLAYER
    auth.attach_steam(extra, "76561199000009999")
    auth.set_tracking_consent(extra, True)
    try:
        svc.join(extra, s.join_code)
    except AuthError as e:
        assert "all 4 players" in str(e), str(e)
    else:
        raise AssertionError("a fifth player took a seat in a 2v2")


def test_both_names_on_a_side_are_shown_as_one_line():
    """Everything downstream shows a side, so a 2v2 reads "A1, A2" where a duel
    reads one name."""
    auth, (a1, a2, b1, b2) = four_players()
    svc = DraftService()
    s = svc.create(a1, MAPS, CMDS, best_of=3, team_size=2)
    for p in (a2, b1, b2):
        svc.join(p, s.join_code)
    seats = s.public_state(a1)["seats"]
    assert seats["A"] == "A1, A2", seats
    assert seats["B"] == "B1, B2", seats
    assert s.public_state(a1)["team_size"] == 2


def test_a_side_of_three_is_refused_at_creation():
    auth, (a1, _, _, _) = four_players()
    svc = DraftService()
    try:
        svc.create(a1, MAPS, CMDS, best_of=3, team_size=3)
    except AuthError as e:
        assert "one or two" in str(e), str(e)
    else:
        raise AssertionError("a 3v3 draft was created")


def test_a_2v2_draft_plays_out_like_a_duel():
    """The bans, the picks and the reveal are all per side — none of them cares
    how many people a side is."""
    auth, (a1, a2, b1, b2) = four_players()
    svc = DraftService()
    s = svc.create(a1, MAPS, CMDS, best_of=3, team_size=2)
    for p in (a2, b1, b2):
        svc.join(p, s.join_code)
    # Either member of a side may act for it.
    while s.draft.current is not None:
        step = s.draft.current
        if step.side is None:
            for side, actor in ((Side.A, a2), (Side.B, b2)):
                if s.draft.legal_options(side):
                    s.apply(actor, s.draft.legal_options(side)[0])
        else:
            actor = a1 if step.side is Side.A else b1
            s.apply(actor, s.draft.legal_options(step.side)[0])
    assert s.draft.done
    assert len(s.public_state(a1)["plan"]) == 3


# --------------------------------------------------- Longer series, and Bo9
def test_a_bo9_needs_nine_maps_and_says_so():
    auth, a, b = two_players()
    svc = DraftService()
    try:
        svc.create(a, MAPS, CMDS, best_of=9)      # five maps
    except AuthError as e:
        assert "map pool" in str(e), str(e)
    else:
        raise AssertionError("a Bo9 was built out of five maps")


def test_a_bo9_works_with_an_odd_pool_of_nine():
    auth, a, b = two_players()
    svc = DraftService()
    nine = [f"Map{i}" for i in range(9)]
    s = svc.create(a, nine, CMDS, best_of=9)
    svc.join(b, s.join_code)
    play_all(s, a, b)
    assert s.draft.done
    assert len(s.public_state(a)["plan"]) == 9
    # A Bo9 is decided at five.
    assert s.series_over() is False
    for g in range(1, 6):
        played(s, a, g, "A")
    assert s.series_over()


# ------------------------------- Leaving the game first is not cheating
def test_a_missing_player_is_a_replay_not_an_abort():
    """Whoever quits Forts first hands their client a log the other player has
    already left. Aborting on that fired on whichever of the two closed the game
    first, which is the harshest verdict there is reached from a timing
    artefact."""
    s, a, b, plan, ids = played_series()
    want = plan[0]
    st = s.note_game(a, 1, "A", steam_ids=[ids["A"]],   # B already gone
                     map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"]})
    assert st["aborted"] is False, st["aborted_reason"]
    assert st["games_played"] == [], "a half-recorded game was counted"
    assert "was not in the game" in " ".join(st["deviations"]["1"])


def test_somebody_else_in_the_seat_still_aborts():
    """The case aborting is actually for: a drafted player missing while
    somebody undrafted is present is a substitution, not a quit."""
    s, a, b, plan, ids = played_series()
    want = plan[0]
    st = s.note_game(a, 1, "A",
                     steam_ids=[ids["A"], "76561199000004444"],
                     map_played=want["map"],
                     commanders={ids["A"]: want["commander_a"],
                                 ids["B"]: want["commander_b"]})
    assert st["aborted"] is True, st
    assert "different Steam account" in (st["aborted_reason"] or "")


def test_a_draft_that_never_reached_a_lobby_stops_blocking_sooner():
    """Both clients closing during the picking left a draft on the server that
    held two people out of matchmaking for three hours."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500)
    q.join(b, 1500)
    clock[0] += 1
    q.status(a)
    q.accept(a)
    st = q.accept(b)
    s = q.drafts.get(st["draft_id"])
    assert q.open_series(a) is s

    # A quarter of an hour with no lobby: not a match anybody is in the middle of.
    clock[0] += q.UNSTARTED_BLOCK_MAX_S + 1
    assert q.open_series(a) is None
    q.join(a, 1500)
    assert q.status(a)["in_queue"]


def test_a_series_with_a_lobby_still_blocks_for_the_full_time():
    """The short cutoff is for drafts that never started, not for a match being
    played slowly."""
    q, clock, (a, b) = queue_with("A", "B")
    q.join(a, 1500)
    q.join(b, 1500)
    clock[0] += 1
    q.status(a)
    q.accept(a)
    st = q.accept(b)
    s = q.drafts.get(st["draft_id"])
    play_all(s, a, b)
    s.set_lobby(a, 4242)

    clock[0] += q.UNSTARTED_BLOCK_MAX_S + 1
    assert q.open_series(a) is s, "a live series stopped blocking too early"


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
