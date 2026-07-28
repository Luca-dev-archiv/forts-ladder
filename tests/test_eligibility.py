"""Tests for what counts towards the ladder.

These exist because two things were promised publicly when the project was
cleared, and a promise that only lives in a Discord message is not a
guarantee. Each test below is one sentence from that exchange:

  * "a tracker for all lobbies except ranked games"
  * "your data is only collected if you want it to be"
  * "as long as it tracks people who want to"

So this file is the answer to "how do you actually enforce that?" — if one of
these fails, the project is doing something it said it would not.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.eligibility import Eligibility, consent_filter  # noqa: E402
from ladder.identity import Registry  # noqa: E402
from ladder.open_ladder import recompute  # noqa: E402
from ladder.report import Series  # noqa: E402

A = "76561190000000001"
B = "76561190000000002"
C = "76561190000000003"


def match(lobby=None, ids=(A, B), map_name="Abyss", winner=1):
    return {
        "map": map_name,
        "lobby_id": lobby,
        "played_at": "2026-07-28T20:00:00",
        "players": [{"steam_id": s, "name": f"P{i}", "side": i + 1}
                    for i, s in enumerate(ids)],
        "outcome": {"status": "decided", "winner_side": winner},
        "commanders": {},
    }


def ready(lobby=111, ids=(A, B)) -> Eligibility:
    """An eligibility that says yes — so a failing test below means the gate
    closed, not that the fixture was incomplete."""
    e = Eligibility()
    e.authoritative = True
    e.sanction(lobby)
    for s in ids:
        e.opt_in(s)
    return e


# --------------------------------------------------------- Arming a lobby
def test_arming_turns_the_next_lobby_id_into_a_sanctioned_one():
    """The lobby id does not exist until Steam creates it, so intent is
    recorded first and matched against the id when the log reports it."""
    e = Eligibility()
    e.opt_in(A)
    e.opt_in(B)
    e.authoritative = True
    assert not e.check_series([match(lobby=555)])
    e.arm(series_id="s1", now=1000.0)
    assert e.observe_lobby(555, now=1001.0)
    assert e.check_series([match(lobby=555)])


def test_an_arm_covers_exactly_one_lobby():
    """Otherwise one declaration would quietly collect a whole evening."""
    e = Eligibility()
    e.arm(now=1000.0)
    assert e.observe_lobby(555, now=1001.0)
    assert not e.observe_lobby(556, now=1002.0)
    assert e.is_sanctioned(555) and not e.is_sanctioned(556)


def test_an_expired_arm_sanctions_nothing():
    """The whole point of the deadline: arming and then playing something
    else an hour later must not count."""
    e = Eligibility()
    e.arm(ttl_s=60, now=1000.0)
    assert not e.observe_lobby(555, now=1000.0 + 61)
    assert not e.is_sanctioned(555)
    assert e.armed is None, "an expired arm should be dropped, not kept"


def test_observing_a_lobby_without_arming_does_nothing():
    e = Eligibility()
    assert not e.observe_lobby(555)
    assert e.sanctioned == {}


def test_an_arm_survives_a_client_restart():
    """There is a gap between setting the lobby up and hosting it."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "consent.json"
        e = Eligibility()
        e.arm(series_id="s7", ttl_s=900, now=1000.0)
        e.save(path)
        again = Eligibility.load(path)
        assert again.armed is not None
        assert again.armed.series_id == "s7"
        assert again.observe_lobby(555, now=1100.0)


# ------------------------------------------------------------- Server sync
def test_sync_lets_the_guest_agree_with_the_host():
    """The guest's machine never saw the lobby being armed, so without this
    their client would refuse a match that legitimately counts."""
    guest = Eligibility()
    assert not guest.check_series([match(lobby=555)])
    guest.sync_from_server({"steam_ids": [A, B], "sanctioned_lobbies": [555]})
    assert guest.check_series([match(lobby=555)])
    assert guest.authoritative


def test_a_server_statement_is_not_downgraded_by_a_local_one():
    e = Eligibility()
    e.sync_from_server({"steam_ids": [A], "sanctioned_lobbies": [555]})
    e.sanction(555, source="client")
    assert e.sanctioned[555] == "server", "provenance was overwritten"


def test_sync_replaces_the_roster_so_a_withdrawal_propagates():
    """A stale local entry must not keep someone in after they left."""
    e = Eligibility()
    e.opt_in(A)
    e.opt_in(B)
    e.sync_from_server({"steam_ids": [A], "sanctioned_lobbies": []})
    assert e.is_registered(A)
    assert not e.is_registered(B), "withdrawal did not propagate"


# ------------------------------------------------------------- The allowlist
def test_a_lobby_the_ladder_did_not_create_does_not_count():
    """The load-bearing one.

    Ranked is excluded because nothing is included by default — not because
    ranked is detected. A detection gap would silently *count* ranked games;
    this way an unknown lobby is silently ignored instead.
    """
    e = ready()
    v = e.check_series([match(lobby=999)])
    assert not v
    assert any("not played in a lobby the ladder set up" in r
               for r in v.reasons), v.reasons
    # Same refusal for a single game, phrased for one match.
    single = e.check_match(match(lobby=999))
    assert not single
    assert any("not set up by the ladder" in r for r in single.reasons), single


def test_a_match_without_a_lobby_id_does_not_count():
    """Skirmish against the built-in AI logs no lobby line at all."""
    e = ready()
    v = e.check_series([match(lobby=None)])
    assert not v
    assert any("no lobby id" in r for r in v.reasons), v.reasons


def test_a_sanctioned_lobby_with_everyone_opted_in_counts():
    assert ready().check_series([match(lobby=111)])


def test_every_game_of_a_series_has_to_be_sanctioned():
    """Half a Bo3 is not a result.

    A host crash moves play into a NEW lobby, and that continuation has to be
    sanctioned too — otherwise a series would silently report as a partial.
    """
    e = ready()
    v = e.check_series([match(lobby=111), match(lobby=222)])
    assert not v
    assert any("222" in r for r in v.reasons), v.reasons
    e.sanction(222)
    assert e.check_series([match(lobby=111), match(lobby=222)])


# ----------------------------------------------------------------- Consent
def test_an_opponent_who_never_opted_in_blocks_the_result():
    e = ready(ids=(A,))
    v = e.check_series([match(lobby=111)])
    assert not v
    assert any(B in r for r in v.reasons), v.reasons


def test_an_unknown_roster_says_so_instead_of_assuming_refusal():
    """Offline the client only knows about itself. "Cannot confirm" and
    "declined" are different facts and the reason has to distinguish them, or
    people are told they refused something they never saw."""
    e = Eligibility()
    e.sanction(111)
    e.opt_in(A)
    reasons = e.check_series([match(lobby=111)]).reasons
    assert any("consent unknown" in r for r in reasons), reasons

    e.authoritative = True
    reasons = e.check_series([match(lobby=111)]).reasons
    assert any("not opted in" in r for r in reasons), reasons


def test_withdrawal_works_and_is_not_a_one_way_door():
    e = ready()
    assert e.check_series([match(lobby=111)])
    assert e.withdraw(B)
    assert not e.check_series([match(lobby=111)])
    assert not e.withdraw(B), "withdrawing twice should report nothing to do"


def test_opting_in_twice_is_not_an_error_but_reports_no_change():
    e = Eligibility()
    assert e.opt_in(A)
    assert not e.opt_in(A)


# ------------------------------------------------- The gate at the way out
def test_no_report_line_is_produced_for_a_series_that_does_not_count():
    """The point of the whole thing: a non-consenting opponent's name must
    not reach a message, so there is no line to send in the first place."""
    s = Series(matches=[match(lobby=111)])
    reg = Registry()
    line, warnings = s.report(reg, elig=ready(ids=(A,)))
    assert line == ""
    assert warnings
    assert not any(B in w and "opted in" not in w and "unknown" not in w
                   for w in warnings if w == line)


def test_reading_back_your_own_logs_stays_ungated():
    """Recording and inspecting is not publishing. Passing no eligibility
    keeps `list`/`show` working — otherwise the tool would be useless for the
    archived logs it was built against."""
    s = Series(matches=[match(lobby=999)])
    line, _ = s.report(Registry())
    assert line, "ungated report should still produce a line"


def test_a_withdrawn_player_disappears_from_the_open_ladder():
    """Retroactive by construction: the rating is recomputed from events, so
    a filter that stops passing them removes the past too."""
    events = [{"kind": "1v1", "date": "2026-08-01", "event": "open",
               "a": "Alice", "b": "Bob", "games": 3, "score_a": 3}]
    reg = Registry()
    reg.add_link("Alice", A, "test")
    reg.add_link("Bob", B, "test")

    e = Eligibility()
    e.authoritative = True
    e.opt_in(A)
    e.opt_in(B)
    both = recompute(events, allow=consent_filter(e, reg))
    assert "Alice" in both.players and "Bob" in both.players

    e.withdraw(B)
    after = recompute(events, allow=consent_filter(e, reg))
    assert after.players == {}, "the event should vanish for both sides"
    assert any("no consent" in n for n in after.notes), after.notes


def test_an_unlinked_name_is_not_rated():
    """A name with no Steam ID cannot have consented, so counting it would be
    guessing about consent."""
    reg = Registry()
    e = Eligibility()
    e.authoritative = True
    allow = consent_filter(e, reg)
    assert not allow("Nobody")


def test_one_opted_in_alt_account_is_enough():
    """A player is a person, not an account. Alt accounts are normal."""
    reg = Registry()
    reg.add_link("Alice", A, "test")
    reg.add_link("Alice", C, "test")
    e = Eligibility()
    e.opt_in(C)
    assert consent_filter(e, reg)("Alice")


# -------------------------------------------------------------- Persistence
def test_consent_survives_a_restart_but_a_broken_file_grants_nothing():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "consent.json"
        e = ready()
        e.save(path)
        again = Eligibility.load(path)
        assert again.is_registered(A) and again.is_registered(B)
        assert set(again.sanctioned) == {111}
        assert again.authoritative

        path.write_text("{not json", encoding="utf-8")
        broken = Eligibility.load(path)
        assert broken.consent == {}, "a broken file must not widen the gate"
        assert not broken.check_series([match(lobby=111)])


def test_load_resolves_the_path_at_call_time():
    """Same trap as in identity.py: a default frozen at import made a test
    write the real file."""
    with tempfile.TemporaryDirectory() as d:
        import ladder.eligibility as mod
        original = mod.CONSENT_FILE
        try:
            mod.CONSENT_FILE = Path(d) / "consent.json"
            e = Eligibility()
            e.opt_in(A)
            e.save()
            assert mod.CONSENT_FILE.exists(), "save ignored the redirect"
            assert Eligibility.load().is_registered(A)
        finally:
            mod.CONSENT_FILE = original


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
