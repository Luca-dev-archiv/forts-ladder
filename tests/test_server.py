"""Tests for login, permissions and the running-matches list.

The focus is on what is meant to fail: claiming someone else's name,
creating tournaments without admin rights, pushing into a full lobby. A
permission system proves itself in its refusals, not in its approvals.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.auth import (  # noqa: E402
    AuthError, AuthService, Grant, OAUTH_STATE_TTL_S, Role,
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


# ------------------------------------------------------------- Bootstrapping
def test_without_a_configured_owner_nobody_can_become_one():
    """The hole this closes: OWNER can only be granted by an OWNER and every
    account starts as PLAYER, so a fresh server had no first admin at all and
    could never be set up."""
    auth = AuthService()
    a = auth.login_discord("1", "First")
    b = auth.login_discord("2", "Second")
    assert a.role is Role.PLAYER
    assert auth.apply_owner_bootstrap() is None
    try:
        auth.grant_role(a, b, Role.OWNER)
    except AuthError:
        pass
    else:
        raise AssertionError("a player handed out OWNER")


def test_the_configured_operator_becomes_owner():
    """Named in the environment rather than "first login wins": on a public
    endpoint that would hand the server to whichever stranger arrives first."""
    import server.auth as mod
    # The module constant is patched directly rather than reloading the module:
    # a reload builds new class objects, so `Role` imported at the top of this
    # file would stop matching and every later test would compare against stale
    # classes. That cost one failure before it was noticed.
    saved = mod.OWNER_DISCORD_ID
    mod.OWNER_DISCORD_ID = "12345"
    try:
        auth = AuthService()
        me = auth.login_discord("12345", "Operator")
        assert me.role is Role.OWNER
        # And nobody else.
        assert auth.login_discord("999", "Stranger").role is Role.PLAYER

        # An account that already existed is promoted too, so the operator does
        # not have to sign in again after configuring the id.
        later = AuthService()
        existing = later.login_discord("12345", "Operator")
        existing.role = Role.PLAYER
        assert later.apply_owner_bootstrap().role is Role.OWNER
    finally:
        mod.OWNER_DISCORD_ID = saved


# ------------------------------------------------------------- Verification
def test_a_steam_identity_from_another_host_is_rejected_before_any_call():
    """The parameters arrive in the user's own query string, so they can be
    edited. Trusting `claimed_id` would let anyone claim any account."""
    from server.auth import verify_steam_openid
    for forged in ("https://evil.example/openid/id/76561190000000001",
                   "https://steamcommunity.com/openid/id/12",
                   "76561190000000001", ""):
        try:
            verify_steam_openid({"openid.claimed_id": forged})
        except AuthError as e:
            assert "not a Steam identity" in str(e), str(e)
        else:
            raise AssertionError(f"{forged!r} was accepted as an identity")


def test_discord_exchange_refuses_without_a_secret():
    """Without the secret the code cannot be verified, and a login that is not
    verified is worse than none because it looks like proof."""
    import os
    from server.auth import exchange_discord_code
    saved = {k: os.environ.pop(k, None)
             for k in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET")}
    try:
        exchange_discord_code("some-code", "https://example.com/cb")
    except AuthError as e:
        assert "DISCORD_CLIENT_SECRET" in str(e), str(e)
    else:
        raise AssertionError("the exchange proceeded without a secret")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ----------------------------------------------------------------- Identity
def test_a_matching_discord_name_proves_the_ufer_claim():
    """The core: the ranking lists Discord names, so the login itself
    substantiates the claim."""
    auth = AuthService()
    a = auth.login_discord("42", "SecondSeed")
    auth.claim_ufer_name(a, "SecondSeed")
    assert a.ufer_name == "SecondSeed"


def test_a_different_name_is_held_for_an_admin_not_applied():
    """A name that does not match the Discord login must not take effect on its
    own — but it must not be thrown away either. Refusing outright left anyone
    listed on the spreadsheet under another name with no route in at all, so
    the claim is held and an admin decides."""
    auth = AuthService()
    a = auth.login_discord("42", "someone-else")

    assert auth.claim_ufer_name(a, "TopSeed") is False
    assert a.ufer_name is None, "a foreign name was applied without a check"
    assert a.ufer_claim == "TopSeed", "the claim was thrown away"
    assert a in auth.pending_claims()

    admin = auth.login_discord("1", "Boss")
    admin.role = Role.ADMIN
    assert auth.confirm_ufer_name(admin, a) == "TopSeed"
    assert a.ufer_name == "TopSeed"
    assert a.ufer_claim is None
    assert auth.pending_claims() == []


def test_only_an_admin_can_confirm_a_held_name():
    auth = AuthService()
    a = auth.login_discord("42", "someone-else")
    auth.claim_ufer_name(a, "TopSeed")
    nobody = auth.login_discord("7", "Nobody")
    try:
        auth.confirm_ufer_name(nobody, a)
    except AuthError as e:
        assert "link_other_account" in str(e), str(e)
    else:
        raise AssertionError("a player confirmed their own identity claim")
    assert a.ufer_name is None


def test_a_matching_name_needs_nobody():
    """The spreadsheet lists Discord names, so a matching login *is* the
    proof — asking an admin to confirm it would be theatre."""
    auth = AuthService()
    a = auth.login_discord("9", "Dranistian")
    assert auth.claim_ufer_name(a, "Dranistian") is True
    assert a.ufer_name == "Dranistian"
    assert a.ufer_claim is None


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
    # Unranked, because that is what a spectated match is: a rated series is
    # closed to watchers except for admins, which has its own tests below.
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1",
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
    # Lobby *and* owner. Steam needs the owner's account in the third field;
    # a link that stopped at the lobby id did not join.
    assert info["join_url"] == (f"steam://joinlobby/410900/{m.lobby_id}/"
                                f"{acc['Host'].steam_id}"), info["join_url"]
    assert info["lobby_id"] == str(m.lobby_id)
    assert m.slots_used == 3


def test_a_declined_observer_gets_nothing():
    _, acc, live, m = live_setup()
    r = live.request_observer(acc["Caster"], m.id)
    live.answer(acc["Host"], r.id, approve=False, reason="tournament game starting")
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
    # A string, because a Steam lobby id does not survive being parsed as a
    # JSON number.
    assert live.join_info(acc["Referee"], r.id)["lobby_id"] == str(m.lobby_id)


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
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["Host"], 1, 9)
    assert len(live.listing()) == 1
    clock.t += STALE_AFTER_S + 1
    assert live.listing() == []
    assert m.id not in live.matches


def test_a_pending_request_expires_with_its_match():
    clock = Clock()
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService(now=clock)
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["Host"], 1, 9)
    r = live.request_observer(acc["Caster"], m.id)
    clock.t += STALE_AFTER_S + 1
    live.prune()
    assert r.state is RequestState.EXPIRED


# ------------------------------------------- What the person who asked can see
def test_a_requester_can_find_out_what_happened():
    """Before this route existed the only list was the host's inbox, so someone
    pressed "ask to watch" and never learned the answer."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1",
                     ["Host", "Guest"], 2, 9, lobby_id=109775240000000001)

    r = live.request_observer(acc["Caster"], m.id)
    mine = live.requests_for(acc["Caster"])
    assert len(mine) == 1 and mine[0]["state"] == "pending"
    assert "lobby_id" not in mine[0], "the lobby id leaked before admission"

    live.answer(acc["Host"], r.id, approve=True)
    mine = live.requests_for(acc["Caster"])
    assert mine[0]["state"] == "approved"
    assert mine[0]["lobby_id"] == "109775240000000001"
    assert mine[0]["join_url"].startswith("steam://joinlobby/410900/")


def test_a_declined_requester_is_told_why():
    """"No room" is arithmetic — nine clients including spectators — and reads
    completely differently from "the host said no"."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1",
                     ["Host", "Guest"], 9, 9, lobby_id=1)
    live.request_observer(acc["Caster"], m.id)
    mine = live.requests_for(acc["Caster"])
    assert mine[0]["state"] == "declined"
    assert "nine clients" in mine[0]["reason"], mine[0]["reason"]
    assert "lobby_id" not in mine[0]


def test_only_your_own_requests_come_back():
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "One", Role.CASTER),
                             ("3", "Two", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=1)
    live.request_observer(acc["One"], m.id)
    live.request_observer(acc["Two"], m.id)
    assert len(live.requests_for(acc["One"])) == 1
    assert len(live.requests_for(acc["Two"])) == 1


def test_the_join_url_names_the_lobby_owner():
    """Steam needs the owner's account in the third field; leaving it out and
    letting Steam work it out does not join."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=42)
    r = live.request_observer(acc["Caster"], m.id)
    live.answer(acc["Host"], r.id, approve=True)
    info = live.join_info(acc["Caster"], r.id)
    assert info["join_url"] == f"steam://joinlobby/410900/42/{acc['Host'].steam_id}"


def test_a_ranked_match_is_not_open_to_spectators():
    """A watcher in a rated series is one more person who knows the board."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1", ["a", "b"], 2, 9,
                     lobby_id=1)
    r = live.request_observer(acc["Caster"], m.id)
    assert r.state is RequestState.DECLINED
    assert "ranked" in r.reason, r.reason


def test_an_unranked_match_is():
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=1)
    assert live.request_observer(acc["Caster"], m.id).state is RequestState.PENDING


def test_an_admin_is_not_blocked_from_a_ranked_match():
    """Arbitrating needs seeing, which is why the grant exists. They still ask
    the host — being allowed to watch is not the same as walking in."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Ref", Role.ADMIN))
    live = LiveService()
    m = live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1", ["a", "b"], 2, 9,
                     lobby_id=1)
    r = live.request_observer(acc["Ref"], m.id)
    assert r.state is RequestState.PENDING, r.reason

    # And when the host has closed requests they are admitted anyway, which is
    # the actual override. A fresh account, because one pending request per
    # person is all the service allows — rightly.
    auth2, acc2 = service_with(("9", "Ref2", Role.ADMIN))
    live.set_accepting(acc["Host"], m.id, False)
    r2 = live.request_observer(acc2["Ref2"], m.id)
    assert r2.state is RequestState.APPROVED


def test_a_plain_player_may_not_ask_to_watch_at_all():
    """Watching is a role. A player with no reason to be in somebody else's
    lobby is exactly who should not be there, and "ask everyone and see who says
    yes" is how information leaks in a small scene."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Nosy", Role.PLAYER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=1)
    r = live.request_observer(acc["Nosy"], m.id)
    assert r.state is RequestState.DECLINED
    assert "caster or referee" in r.reason, r.reason


def test_the_caster_grant_is_enough_without_a_promotion():
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Cam", Role.PLAYER))
    acc["Cam"].grants.add(Grant.CASTER)
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=1)
    assert live.request_observer(acc["Cam"], m.id).state is RequestState.PENDING


def test_a_caster_may_not_watch_a_rated_series_but_a_referee_may():
    """Somebody has to be answerable for being in a lobby whose result changes
    ratings."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Cam", Role.CASTER),
                             ("3", "Ref", Role.PLAYER))
    acc["Ref"].grants.add(Grant.REFEREE)
    live = LiveService()
    m = live.publish(acc["Host"], "ranked_1v1", "Ranked 1v1", ["a", "b"], 2, 9,
                     lobby_id=1)
    assert live.request_observer(acc["Cam"], m.id).state is RequestState.DECLINED
    assert live.request_observer(acc["Ref"], m.id).state is RequestState.PENDING


def test_a_host_can_close_a_match_to_spectators_entirely():
    """Different from "not right now": a closed match declines a caster too."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Cam", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=1)
    live.set_spectators_allowed(acc["Host"], m.id, False)
    r = live.request_observer(acc["Cam"], m.id)
    assert r.state is RequestState.DECLINED
    assert "closed to spectators" in r.reason, r.reason
    assert live.listing()[0]["allow_spectators"] is False


def test_only_the_host_closes_their_own_match():
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Cam", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["a", "b"],
                     2, 9, lobby_id=1)
    try:
        live.set_spectators_allowed(acc["Cam"], m.id, False)
    except AuthError:
        pass
    else:
        raise AssertionError("somebody else closed the host's match")


def test_the_terms_say_what_a_spectator_is_agreeing_to():
    """A spectator sees both forts. In a rated series that is everything one side
    is paying to keep hidden, so the delay is the condition."""
    terms = LiveService.OBSERVER_TERMS.lower()
    assert "delay" in terms
    assert "do not pass" in terms


# ------------------------------------------- One entry, and the way into it
def test_publishing_the_same_lobby_twice_does_not_list_it_twice():
    """The client publishes from its draft refresh, which ticks about once a
    second, and its own guard is only set once the request comes back. Two of
    them went out while the first was in flight and the same match appeared
    twice in everybody's live list — and a client restarted mid-series did it
    again, which no guard living in a client can prevent."""
    _, acc, live, m = live_setup()
    again = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1",
                         ["Host", "Opponent"], slots_used=2, slots_total=9,
                         lobby_id=m.lobby_id)
    assert again.id == m.id, "the same lobby was published as a second match"
    assert len(live.listing()) == 1


def test_republishing_does_not_forget_the_spectators_already_in():
    """slots_used carries the admitted ones, and the client only ever knows
    about the players — so taking its number would open the seats again."""
    _, acc, live, m = live_setup()
    r = live.request_observer(acc["Caster"], m.id)
    live.answer(acc["Host"], r.id, approve=True)
    used = m.slots_used

    live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1",
                 ["Host", "Opponent"], slots_used=2, slots_total=9,
                 lobby_id=m.lobby_id)
    assert m.slots_used == used, "a re-publish freed an occupied seat"
    assert m.observers == [acc["Caster"].ufer_name]


def test_a_different_lobby_is_a_different_match():
    """The dedup is per lobby, not per host: playing a second series after the
    first is not a duplicate of it."""
    _, acc, live, m = live_setup()
    other = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1",
                         ["Host", "Opponent"], slots_used=2, slots_total=9,
                         lobby_id=109775240000000002)
    assert other.id != m.id
    assert len(live.listing()) == 2


def test_an_admitted_spectator_is_given_the_lobby_password():
    """Every ladder lobby has one — the client writes it into multiplayer.lua —
    so being admitted with only a join link got somebody as far as the game's
    password prompt and no further."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["Host"],
                     slots_used=1, slots_total=6,
                     lobby_id=109775240000000003, lobby_password="K7QMB")
    r = live.request_observer(acc["Caster"], m.id)
    live.answer(acc["Host"], r.id, approve=True)

    assert live.join_info(acc["Caster"], r.id)["lobby_password"] == "K7QMB"
    mine = live.requests_for(acc["Caster"])[0]
    assert mine["lobby_password"] == "K7QMB"


def test_the_password_is_not_in_the_public_listing():
    """It is the second half of what lets somebody in, so it follows the same
    rule the lobby id does."""
    auth, acc = service_with(("1", "Host", Role.PLAYER),
                             ("2", "Caster", Role.CASTER))
    live = LiveService()
    m = live.publish(acc["Host"], "unranked_1v1", "Unranked 1v1", ["Host"],
                     slots_used=1, slots_total=6,
                     lobby_id=109775240000000003, lobby_password="K7QMB")
    entry = live.listing()[0]
    assert "lobby_password" not in entry
    assert "K7QMB" not in str(entry)

    # And not before the host has said yes, either.
    r = live.request_observer(acc["Caster"], m.id)
    waiting = live.requests_for(acc["Caster"])[0]
    assert "lobby_password" not in waiting, waiting
    try:
        live.join_info(acc["Caster"], r.id)
    except AuthError:
        pass
    else:
        raise AssertionError("a pending request was handed the way in")


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
