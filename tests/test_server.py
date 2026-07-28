"""Tests for login, permissions and the running-matches list.

The focus is on what is meant to fail: claiming someone else's name,
creating tournaments without admin rights, pushing into a full lobby. A
permission system proves itself in its refusals, not in its approvals.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.auth import (  # noqa: E402
    AuthError, AuthService, OAUTH_STATE_TTL_S, Role,
)
from server.live import LiveService, RequestState, STALE_AFTER_S  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def service_with(*people) -> tuple[AuthService, dict]:
    auth = AuthService()
    accounts = {}
    for discord_id, name, role in people:
        a = auth.login_discord(discord_id, name)
        a.role = role
        auth.attach_steam(a, f"7656119900000{int(discord_id):04d}")
        a.ufer_name = name
        # Through the real API, not by assignment: these accounts stand in for
        # people who signed up to play, and consent is what makes them
        # trackable at all. The refusal path has its own tests below.
        auth.set_tracking_consent(a, True)
        accounts[name] = a
    return auth, accounts


# ------------------------------------------------------------- Permissions
def test_only_admins_may_create_tournaments():
    auth, acc = service_with(("1", "Player", Role.PLAYER),
                             ("2", "Boss", Role.ADMIN))
    assert not acc["Player"].may("create_tournament")
    assert acc["Boss"].may("create_tournament")
    try:
        acc["Player"].require("create_tournament")
    except AuthError as e:
        assert "Admin" in str(e), str(e)
    else:
        raise AssertionError("a player was allowed to create a tournament")


def test_everyone_may_report_their_own_match():
    auth, acc = service_with(("1", "Player", Role.PLAYER))
    assert acc["Player"].may("report_own_match")
    assert not acc["Player"].may("report_any_match")


def test_unknown_action_is_a_programming_error_not_a_silent_no():
    auth, acc = service_with(("1", "X", Role.OWNER))
    try:
        acc["X"].may("no-such-key")
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown action was checked silently")


def test_you_cannot_grant_a_role_you_do_not_outrank():
    auth, acc = service_with(("1", "Boss", Role.ADMIN), ("2", "Rookie", Role.PLAYER))
    try:
        auth.grant_role(acc["Boss"], acc["Rookie"], Role.ADMIN)
    except AuthError:
        pass
    else:
        raise AssertionError("an admin appointed a second admin")


# ------------------------------------------------------------------ Consent
def test_signing_in_is_not_consent_to_being_tracked():
    """The condition this project was cleared under. An account made to watch
    a stream is not a request to be rated."""
    auth = AuthService()
    a = auth.login_discord("1", "X")
    auth.attach_steam(a, "76561190000000001")
    assert not a.tracking_consent
    assert not a.trackable
    try:
        auth.require_trackable(a)
    except AuthError as e:
        assert "not agreed" in str(e), str(e)
    else:
        raise AssertionError("an account without consent was trackable")


def test_consent_needs_a_proven_steam_id():
    """Otherwise someone could consent on another person's behalf."""
    auth = AuthService()
    a = auth.login_discord("1", "X")
    try:
        auth.set_tracking_consent(a, True)
    except AuthError as e:
        assert "Steam" in str(e), str(e)
    else:
        raise AssertionError("consent was accepted without a Steam ID")


def test_consent_can_be_withdrawn_again():
    auth, acc = service_with(("1", "X", Role.PLAYER))
    a = acc["X"]
    assert a.trackable and a.consent_since
    auth.set_tracking_consent(a, False)
    assert not a.trackable
    assert a.consent_since is None


def test_the_roster_holds_only_consenting_verified_accounts():
    """What the clients sync. Ids only — checking eligibility does not
    require knowing who else plays here."""
    auth, acc = service_with(("1", "In", Role.PLAYER), ("2", "Out", Role.PLAYER))
    auth.set_tracking_consent(acc["Out"], False)
    roster = auth.trackable_ids()
    assert acc["In"].steam_id in roster
    assert acc["Out"].steam_id not in roster
    assert all(isinstance(x, str) for x in roster)


def test_publishing_a_live_match_needs_consent():
    """A live entry names who is playing, so it is publication."""
    auth, acc = service_with(("1", "Host", Role.PLAYER))
    auth.set_tracking_consent(acc["Host"], False)
    live = LiveService()
    try:
        live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1", ["Host"],
                     slots_used=1, slots_total=9)
    except AuthError as e:
        assert "not agreed" in str(e), str(e)
    else:
        raise AssertionError("a non-consenting host published a live match")


# ----------------------------------------------------------------- Identity
def test_a_matching_discord_name_proves_the_ufer_claim():
    """The core: the ranking lists Discord names, so the login itself
    substantiates the claim."""
    auth = AuthService()
    a = auth.login_discord("42", "SecondSeed")
    auth.claim_ufer_name(a, "SecondSeed")
    assert a.ufer_name == "SecondSeed"


def test_a_different_name_needs_an_admin():
    auth = AuthService()
    a = auth.login_discord("42", "someone-else")
    try:
        auth.claim_ufer_name(a, "TopSeed")
    except AuthError as e:
        assert "admin" in str(e).lower()
    else:
        raise AssertionError("foreign ladder name accepted without a check")
    auth.claim_ufer_name(a, "TopSeed", by_admin=True)
    assert a.ufer_name == "TopSeed"


def test_a_name_cannot_be_claimed_twice():
    auth = AuthService()
    a = auth.login_discord("1", "SameName")
    auth.claim_ufer_name(a, "SameName")
    b = auth.login_discord("2", "SameName")          # same display name
    try:
        auth.claim_ufer_name(b, "SameName")
    except AuthError as e:
        assert "another account" in str(e)
    else:
        raise AssertionError("two accounts share one ranking name")


def test_a_steam_id_belongs_to_exactly_one_account():
    """Two accounts with the same SteamID would score the same matches
    twice."""
    auth = AuthService()
    a = auth.login_discord("1", "A")
    b = auth.login_discord("2", "B")
    auth.attach_steam(a, "76561190000000001")
    try:
        auth.attach_steam(b, "76561190000000001")
    except AuthError as e:
        assert "another account" in str(e)
    else:
        raise AssertionError("the same SteamID was attached twice")


def test_a_malformed_steam_id_is_refused():
    auth = AuthService()
    a = auth.login_discord("1", "A")
    for bad in ("123", "abcdefghijklmnopq", ""):
        try:
            auth.attach_steam(a, bad)
        except AuthError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a SteamID")


def test_logging_in_again_keeps_the_account_and_updates_the_name():
    auth = AuthService()
    a = auth.login_discord("7", "OldName")
    a.ufer_name = "OldName"
    b = auth.login_discord("7", "NewName")
    assert a.id == b.id, "the second login created a new account"
    assert b.discord_name == "NewName"
    assert b.ufer_name == "OldName", "the ranking name was overwritten"


def test_verified_means_both_halves_are_proven():
    auth = AuthService()
    a = auth.login_discord("1", "X")
    assert not a.verified
    auth.attach_steam(a, "76561190000000001")
    assert a.verified


# --------------------------------------------------------------- Sessions
def test_a_session_expires():
    clock = Clock()
    auth = AuthService(now=clock)
    a = auth.login_discord("1", "X")
    s = auth.start_session(a)
    assert auth.account_for(s.token) is a
    clock.t += 8 * 24 * 3600
    assert auth.account_for(s.token) is None


def test_an_oauth_state_is_single_use_and_expires():
    clock = Clock()
    auth = AuthService(now=clock)
    p = auth.begin_login("discord", "/ranking")
    assert auth.consume_state(p.state).return_to == "/ranking"
    try:
        auth.consume_state(p.state)          # second attempt
    except AuthError:
        pass
    else:
        raise AssertionError("a login attempt could be reused")

    p2 = auth.begin_login("discord")
    clock.t += OAUTH_STATE_TTL_S + 1
    try:
        auth.consume_state(p2.state)
    except AuthError as e:
        assert "expired" in str(e)
    else:
        raise AssertionError("an expired login attempt was accepted")


def test_no_token_means_not_logged_in_rather_than_an_error():
    auth = AuthService()
    assert auth.account_for(None) is None
    assert auth.account_for("made-up") is None


# ------------------------------------------------------- Running matches
def live_setup():
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Opponent", Role.PLAYER),
                             ("3", "Caster", Role.CASTER),
                             ("4", "Referee", Role.ADMIN))
    live = LiveService()
    m = live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1",
                     ["Host", "Opponent"], slots_used=2, slots_total=9,
                     lobby_id=109775240000000001)
    return auth, acc, live, m


def test_the_listing_hides_the_lobby_id():
    """The lobby ID is what lets someone join — it is the access token."""
    _, _, live, m = live_setup()
    entry = live.listing()[0]
    assert "lobby_id" not in entry
    assert entry["free_slots"] == 7


def test_an_approved_observer_gets_the_join_link():
    _, acc, live, m = live_setup()
    r = live.request_observer(acc["Caster"], m.id)
    assert r.state is RequestState.PENDING
    live.answer(acc["Host"], r.id, approve=True)
    info = live.join_info(acc["Caster"], r.id)
    assert info["join_url"].endswith(str(m.lobby_id))
    assert m.slots_used == 3


def test_a_declined_observer_gets_nothing():
    _, acc, live, m = live_setup()
    r = live.request_observer(acc["Caster"], m.id)
    live.answer(acc["Host"], r.id, approve=False, reason="gleich Turnierspiel")
    try:
        live.join_info(acc["Caster"], r.id)
    except AuthError:
        pass
    else:
        raise AssertionError("a declined spectator got the lobby ID")


def test_the_host_can_switch_requests_off():
    _, acc, live, m = live_setup()
    live.set_accepting(acc["Host"], m.id, False)
    r = live.request_observer(acc["Caster"], m.id)
    assert r.state is RequestState.DECLINED
    assert "switched requests off" in r.reason


def test_admins_get_in_even_when_requests_are_off():
    """Rule set: "UFER admins are always allowed to observe"."""
    _, acc, live, m = live_setup()
    live.set_accepting(acc["Host"], m.id, False)
    r = live.request_observer(acc["Referee"], m.id)
    assert r.state is RequestState.APPROVED
    assert live.join_info(acc["Referee"], r.id)["lobby_id"] == m.lobby_id


def test_even_an_admin_does_not_fit_into_a_full_lobby():
    """The nine is not a permission question but an array in the binary."""
    _, acc, live, m = live_setup()
    m.slots_used = 9
    live.set_accepting(acc["Host"], m.id, False)
    r = live.request_observer(acc["Referee"], m.id)
    assert r.state is RequestState.DECLINED
    assert "nine clients" in r.reason


def test_a_full_lobby_says_so_instead_of_blaming_the_person():
    _, acc, live, m = live_setup()
    m.slots_used = m.slots_total
    r = live.request_observer(acc["Caster"], m.id)
    assert r.state is RequestState.DECLINED
    assert "full" in r.reason


def test_only_the_host_answers_requests_for_his_match():
    _, acc, live, m = live_setup()
    r = live.request_observer(acc["Caster"], m.id)
    try:
        live.answer(acc["Opponent"], r.id, approve=True)
    except AuthError as e:
        assert "only the host" in str(e)
    else:
        raise AssertionError("a fellow player decided the request")


def test_a_request_cannot_be_answered_twice():
    _, acc, live, m = live_setup()
    r = live.request_observer(acc["Caster"], m.id)
    live.answer(acc["Host"], r.id, approve=True)
    try:
        live.answer(acc["Host"], r.id, approve=False)
    except AuthError as e:
        assert "already" in str(e)
    else:
        raise AssertionError("an answer was overwritten")


def test_duplicate_requests_are_refused():
    _, acc, live, m = live_setup()
    live.request_observer(acc["Caster"], m.id)
    try:
        live.request_observer(acc["Caster"], m.id)
    except AuthError as e:
        assert "already pending" in str(e)
    else:
        raise AssertionError("two open requests for the same match")


def test_a_silent_client_drops_out_of_the_listing():
    """After a crash no ghost entry may be left behind."""
    clock = Clock()
    auth, acc = service_with(("1", "Host", Role.PLAYER))
    live = LiveService(now=clock)
    m = live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1", ["Host"], 1, 9)
    assert len(live.listing()) == 1
    clock.t += STALE_AFTER_S + 1
    assert live.listing() == []
    assert m.id not in live.matches


def test_a_pending_request_expires_with_its_match():
    clock = Clock()
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService(now=clock)
    m = live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1", ["Host"], 1, 9)
    r = live.request_observer(acc["Caster"], m.id)
    clock.t += STALE_AFTER_S + 1
    live.prune()
    assert r.state is RequestState.EXPIRED


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
