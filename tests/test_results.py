"""Tests for reported series and the standings they produce.

This is the path that did not exist: a finished series went nowhere, so the
shared ranking stayed the imported spreadsheet no matter who won. What matters
about it is not that a rating changes — it is *which* series are allowed to
change one.

Two conditions, both from the clearance this project was given: the lobby has
to be one the ladder set up, and every player in it has to have agreed to be
tracked. A series that fails either is kept and marked, never silently dropped
— "my game did not count" has to be answerable.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.auth import AuthError, AuthService, Role  # noqa: E402
from server.ranking import Ranking  # noqa: E402
from server.results import ResultService  # noqa: E402
from server.store import Store  # noqa: E402

STEAM = {n: f"7656119900000{i:04d}" for i, n in enumerate(
    ("Alice", "Bob", "Carol", "Dave"), start=1)}


class World:
    """A server's worth of state, in a throwaway database."""

    def __init__(self, *, consent=("Alice", "Bob")):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir / "r.sqlite")
        self.auth = AuthService()
        self.people = {}
        for i, name in enumerate(STEAM, start=1):
            acc = self.auth.login_discord(str(i), name)
            acc.role = Role.PLAYER
            acc.ufer_name = name
            self.auth.attach_steam(acc, STEAM[name])
            if name in consent:
                self.auth.set_tracking_consent(acc, True)
            self.store.save_account(acc)
            self.people[name] = acc
        # No seed file: the open column starts from the default rating.
        self.ranking = Ranking(path=self.dir / "no-seed.json")
        self.results = ResultService(self.auth, self.store, self.ranking)

    def sanction(self, lobby=111, a="Alice", b="Bob"):
        """Sanction a lobby the way the server does: with the drafted roster.

        A lobby without one is not rateable any more, and rightly so — that was
        the hole. The fixture has to do what `POST /drafts/{id}/lobby` does.
        """
        self.store.sanction_lobby(lobby, f"series-{lobby}",
                                  roster=[STEAM[a], STEAM[b]])

    def duel(self, a="Alice", b="Bob", *, games=3, score_low=2,
             lobby=111, by=None, played_at="2026-07-28T20:15:00",
             draft_id=None):
        return self.results.report(
            self.people[by or a],
            lobby_id=lobby,
            sides={STEAM[a]: 1, STEAM[b]: 2},
            games=games, score_low=score_low, played_at=played_at,
            draft_id=draft_id)

    def rating(self, name):
        self.results.refresh_ranking()
        row = next((p for p in self.ranking.players if p["name"] == name), None)
        return row["open_rating"] if row else None


# ----------------------------------------------------------------- Accepting
def test_a_sanctioned_series_between_consenting_players_is_rated():
    w = World()
    w.sanction(111)
    r = w.duel()
    assert r.rated, r.reasons
    assert w.rating("Alice") > 1000 > w.rating("Bob")


def test_an_unsanctioned_lobby_is_recorded_but_not_rated():
    """The clearance condition: only lobbies this ladder set up count."""
    w = World()
    r = w.duel(lobby=999)
    assert not r.rated
    assert any("not set up by this ladder" in x for x in r.reasons), r.reasons
    assert w.rating("Alice") is None, "an unsanctioned game moved a rating"


def test_a_player_who_never_opted_in_stops_the_whole_series():
    """Not partially rated: a series with someone who did not agree counts for
    nobody, including the player who did agree."""
    w = World(consent=("Alice",))
    w.sanction(111)
    r = w.duel()
    assert not r.rated
    assert any("agreed to be tracked" in x for x in r.reasons), r.reasons
    assert w.rating("Alice") is None


def test_the_refusal_never_names_the_other_accounts():
    """Saying *which* Steam IDs the server holds would make this a lookup
    service for who is registered."""
    w = World(consent=("Alice",))
    w.sanction(111)
    r = w.duel()
    joined = " ".join(r.reasons)
    for sid in STEAM.values():
        assert sid not in joined, joined


def test_only_someone_in_the_series_may_report_it():
    w = World()
    w.sanction(111)
    try:
        w.duel(by="Carol")
    except AuthError as e:
        assert "not in this series" in str(e), str(e)
    else:
        raise AssertionError("an outsider reported someone else's series")


def test_an_impossible_score_is_refused():
    w = World()
    w.sanction(111)
    for games, score in ((3, 4), (0, 0), (3, -1)):
        try:
            w.duel(games=games, score_low=score)
        except AuthError:
            pass
        else:
            raise AssertionError(f"{score} of {games} was accepted")


def test_reporting_the_same_series_twice_changes_nothing():
    """Both clients report — whichever is running gets it through — so the
    second arrival has to be a no-op rather than a second rating change."""
    w = World()
    w.sanction(111)
    w.duel()
    after_one = w.rating("Alice")
    w.duel(by="Bob")
    assert len(w.store.load_results()) == 1
    assert w.rating("Alice") == after_one


def test_two_series_in_one_lobby_on_one_evening_stay_two():
    """A Bo3 and then another Bo3 without leaving the lobby. Keyed by day alone
    the second would have been swallowed as a duplicate of the first."""
    w = World()
    w.sanction(111)
    w.duel(played_at="2026-07-28T20:15:00", score_low=2)
    w.duel(played_at="2026-07-28T21:40:00", score_low=0)
    assert len(w.store.load_results()) == 2,         [r.played_at for r in w.store.load_results()]
    # And both were rated: two series, two rating changes.
    assert len(w.results.events()) == 2


# ----------------------------------------------------------------- Standings
def test_withdrawing_consent_removes_the_past_too():
    """The rating is recomputed from the events every time, which is what makes
    the promise "you can withdraw" true rather than a policy."""
    w = World()
    w.sanction(111)
    w.duel()
    assert w.rating("Alice") is not None

    w.auth.set_tracking_consent(w.people["Bob"], False)
    assert w.rating("Alice") is None, \
        "a series stayed rated after one side withdrew"


def test_a_win_and_a_loss_move_in_opposite_directions():
    w = World()
    w.sanction(111)
    w.sanction(222)
    w.duel(lobby=111, score_low=3, games=3, played_at="2026-07-20T19:00:00")
    high, low = w.rating("Alice"), w.rating("Bob")
    assert high > 1000 > low, (high, low)

    w.duel(lobby=222, score_low=0, games=3, played_at="2026-07-21T19:00:00")
    assert w.rating("Alice") < high, "a 0-3 did not cost the winner anything"
    assert w.rating("Bob") > low, "a 3-0 did not gain the loser anything"


def test_the_seed_is_where_a_player_starts():
    """Someone on the spreadsheet does not reset to 1000 on their first
    reported game.

    Note what a 2-1 does to an 1800 against a 1000: it *costs* them points,
    because a clean sweep was the expected result. That is the rating system
    working, and it is why this test checks the starting point rather than the
    direction."""
    import json
    w = World()
    seed = w.dir / "seed.json"
    seed.write_text(json.dumps({"source": "test", "players": [
        {"name": "Alice", "rating": 1800}]}), encoding="utf-8")
    w.ranking = Ranking(path=seed)
    w.results = ResultService(w.auth, w.store, w.ranking)
    w.sanction(111)
    w.duel()
    assert w.rating("Alice") > 1500, "the seed was ignored and Alice began at 1000"


def test_a_player_only_known_from_reports_still_appears():
    w = World(consent=("Carol", "Dave"))
    w.sanction(111, "Carol", "Dave")
    w.duel("Carol", "Dave")
    w.results.refresh_ranking()
    names = {p["name"] for p in w.ranking.players}
    assert {"Carol", "Dave"} <= names, names


def test_under_ten_games_a_rating_is_marked_provisional():
    w = World()
    w.sanction(111)
    w.duel()
    w.results.refresh_ranking()
    row = next(p for p in w.ranking.players if p["name"] == "Alice")
    assert row["open_provisional"] is True
    assert row["open_games"] == 3


# ---------------------------------------------------- Asking a human to look
def test_an_unrated_series_is_kept_and_can_be_flagged():
    """The reason a series that cannot be rated is stored rather than dropped:
    "it did not count" is sometimes the software being wrong, and the person it
    happened to is the only one who knows."""
    w = World()
    r = w.duel(lobby=999)                 # not sanctioned, so not rated
    assert r.rated is False
    assert len(w.store.load_results()) == 1, "an unrated series was dropped"

    flagged = w.results.flag(w.people["Alice"], r.id,
                             "the host crashed and it never counted")
    assert flagged.flagged is True
    assert "crashed" in flagged.flag_note
    # And it is waiting for somebody.
    assert [x.id for x in w.results.flagged()] == [r.id]


def test_only_a_participant_may_flag_a_series():
    """A report about your own match, not a way to file complaints about other
    people."""
    w = World()
    w.sanction(111)
    r = w.duel()
    try:
        w.results.flag(w.people["Carol"], r.id, "I do not like this result")
    except AuthError as e:
        assert "not in this series" in str(e), str(e)
    else:
        raise AssertionError("an outsider flagged somebody else's series")


def test_a_flag_survives_a_reload():
    w = World()
    r = w.duel(lobby=999)
    w.results.flag(w.people["Bob"], r.id, "wrong commander loaded")
    again = [x for x in w.store.load_results() if x.id == r.id][0]
    assert again.flagged is True
    assert again.flag_note == "wrong commander loaded"


def test_flagging_does_not_make_it_count():
    """Asking for a look is not a way to get a result rated."""
    w = World()
    r = w.duel(lobby=999)
    w.results.flag(w.people["Alice"], r.id, "please check")
    assert w.rating("Alice") is None


def test_an_unrated_report_does_not_lock_out_a_rateable_one():
    """Whoever reported first used to decide.

    Only the host's log carries a lobby id — "Setting lobby" is written when
    hosting — so a guest reporting first wrote a row with no lobby, which cannot
    be rated, and the host's good report could never displace it. That is how a
    real series came back FL-231 unrated with both players watching.
    """
    w = World()
    w.sanction(111)

    first = w.duel(lobby=None, by="Bob", draft_id="abc123")
    assert not first.rated and first.reasons, first.reasons

    second = w.duel(lobby=111, by="Alice", draft_id="abc123")
    assert second.rated, second.reasons
    assert second.id == first.id, "the same series should have the same id"

    stored = {r.id: r for r in w.store.load_results()}[first.id]
    assert stored.rated, "the unrated row was kept over a rateable one"


def test_a_rated_report_is_not_replaced_by_a_second_one():
    """The original rule still holds where it was ever a rule: two rated reports
    of one series should agree, and the first is the one that counted."""
    w = World()
    w.sanction(111)
    first = w.duel(lobby=111, by="Alice", score_low=2, draft_id="abc123")
    second = w.duel(lobby=111, by="Bob", score_low=1, draft_id="abc123")
    assert first.id == second.id
    stored = {r.id: r for r in w.store.load_results()}[first.id]
    assert stored.score_low == 2, "the second arrival overwrote the first"


# ------------------------------------------------------------------ Reviewing
# A flagged series used to reach an admin as a date, a score and a sentence.
# Nothing to act on, and no way to take a rating back — so "please look at this"
# ended in somebody agreeing with you and being unable to do anything.
def reviewer(w, name="Alice"):
    """Somebody who may review. Made from an existing account, so the roster
    stays the one the ratings are computed from."""
    acc = w.people[name]
    acc.role = Role.ADMIN
    return acc


def test_annulling_takes_the_rating_back_and_says_who_and_why():
    w = World()
    w.sanction(111)
    r = w.duel(draft_id="abc")
    assert r.rated and w.rating("Alice") > 1000

    ref = reviewer(w, "Carol") if "Carol" in w.people else reviewer(w)
    row = w.results.annul(ref, r.id, "game 3 was played on the wrong map")
    assert not row.rated
    assert row.annulled_by == ref.id and row.annulled_at
    assert "wrong map" in row.annul_note

    # Retroactive for free: the standings are recomputed from what still counts,
    # which is the same mechanism that makes withdrawing consent retroactive.
    assert w.rating("Alice") is None or w.rating("Alice") == 1000


def test_an_annulment_needs_a_reason():
    """A rating that vanished for no recorded reason cannot be told apart from a
    bug, and the two players will ask."""
    w = World()
    w.sanction(111)
    r = w.duel(draft_id="abc")
    ref = reviewer(w)
    try:
        w.results.annul(ref, r.id, "   ")
    except AuthError as e:
        assert "say why" in str(e), str(e)
    else:
        raise AssertionError("a rating was taken back with no reason")


def test_a_player_cannot_annul_their_own_loss():
    w = World()
    w.sanction(111)
    r = w.duel(draft_id="abc")
    loser = w.people["Bob"]
    loser.role = Role.PLAYER
    try:
        w.results.annul(loser, r.id, "I did not like it")
    except AuthError as e:
        assert "review_results" in str(e), str(e)
    else:
        raise AssertionError("a player annulled a series they played in")


def test_an_annulment_survives_a_reload_and_can_be_undone():
    w = World()
    w.sanction(111)
    r = w.duel(draft_id="abc")
    ref = reviewer(w)
    w.results.annul(ref, r.id, "wrong commander")

    reloaded = {x.id: x for x in w.store.load_results()}[r.id]
    assert not reloaded.rated and reloaded.annul_note == "wrong commander"

    back = w.results.reinstate(ref, r.id)
    assert back.rated, back.reasons
    assert back.annulled_by is None


def test_reinstating_does_not_rate_something_that_was_never_rateable():
    """Putting an annulment back must not paper over the reason it could not be
    rated in the first place."""
    w = World()
    r = w.duel(lobby=999, draft_id="abc")        # unsanctioned lobby
    assert not r.rated
    ref = reviewer(w)
    w.results.annul(ref, r.id, "and also this")
    back = w.results.reinstate(ref, r.id)
    assert not back.rated, back.reasons
    assert any("not set up by this ladder" in x for x in back.reasons)


# -------------------------------------------------------------------- Replays
def test_a_replay_is_stored_under_a_name_the_server_chose():
    """Nothing the client sends becomes a path. A filename from a request is the
    shortest route out of a directory there is."""
    w = World()
    name = w.store.save_replay("d-abc", 2, b"fake replay bytes")
    assert name == "game02.fwr"
    assert w.store.replays_for("d-abc") == ["game02.fwr"]

    # A hostile series id cannot climb out either.
    hostile = w.store.replay_dir("../../etc/passwd")
    assert ".." not in str(hostile), hostile


def test_replays_are_deleted_after_the_keep_window():
    w = World()
    w.store.save_replay("d-abc", 1, b"x")
    assert w.store.replays_for("d-abc")
    # A week and a minute later.
    w.store.prune_replays(now=time.time() + w.store.REPLAY_KEEP_S + 60)
    assert w.store.replays_for("d-abc") == [], "a replay outlived its week"


def test_replays_inside_the_window_are_kept():
    w = World()
    w.store.save_replay("d-abc", 1, b"x")
    w.store.prune_replays(now=time.time() + w.store.REPLAY_KEEP_S - 60)
    assert w.store.replays_for("d-abc") == ["game01.fwr"]


def test_no_character_from_a_request_reaches_a_path():
    """Stronger than "it cannot escape".

    Filtering the id down to safe characters did stop traversal — there is no way
    to spell `..` out of letters, digits, hyphen and underscore. What it did not
    stop was two ids differing only in punctuation sharing a directory, and the
    argument that real ids never differ that way is reasoning from the shape ids
    happen to have today.
    """
    w = World()
    hostile = "../../etc/passwd"
    component = w.store.replay_dir(hostile).name

    # Nothing recognisable from the input, not merely nothing dangerous.
    assert ".." not in component and "/" not in component
    assert "etc" not in component and "passwd" not in component
    assert component.startswith("x")
    assert all(c in "0123456789abcdefx" for c in component), component


def test_two_ids_that_differ_only_in_punctuation_do_not_share_a_directory():
    """What the old filter allowed: `d-ab` and `d.ab` both became `dab`."""
    w = World()
    assert w.store.replay_dir("d-ab").name != w.store.replay_dir("d.ab").name


def test_the_directory_is_named_by_the_server_not_by_the_series():
    w = World()
    w.sanction(111)
    r = w.duel(draft_id="abc")
    w.store.save_replay(r.id, 1, b"bytes")

    component = w.store.replay_dir(r.id).name
    assert r.id not in component, component
    assert len(component) == 16 and all(c in "0123456789abcdef" for c in component)
    # And it is stable: a second upload goes to the same place.
    w.store.save_replay(r.id, 2, b"more")
    assert w.store.replays_for(r.id) == ["game01.fwr", "game02.fwr"]
    assert w.store.replay_dir(r.id).name == component


def test_a_name_that_is_not_there_cannot_become_a_path():
    w = World()
    w.sanction(111)
    r = w.duel(draft_id="abc")
    w.store.save_replay(r.id, 1, b"bytes")

    assert w.store.replay_path(r.id, "game01.fwr") is not None
    for bogus in ("game02.fwr", "../../ladder.sqlite", "..", "",
                  "game01.fwr ", "GAME01.FWR"):
        assert w.store.replay_path(r.id, bogus) is None, bogus


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
