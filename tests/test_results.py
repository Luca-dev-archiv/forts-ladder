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

    def duel(self, a="Alice", b="Bob", *, games=3, score_low=2,
             lobby=111, by=None, played_at="2026-07-28T20:15:00"):
        return self.results.report(
            self.people[by or a],
            lobby_id=lobby,
            sides={STEAM[a]: 1, STEAM[b]: 2},
            games=games, score_low=score_low, played_at=played_at)

    def rating(self, name):
        self.results.refresh_ranking()
        row = next((p for p in self.ranking.players if p["name"] == name), None)
        return row["open_rating"] if row else None


# ----------------------------------------------------------------- Accepting
def test_a_sanctioned_series_between_consenting_players_is_rated():
    w = World()
    w.store.sanction_lobby(111)
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
    w.store.sanction_lobby(111)
    r = w.duel()
    assert not r.rated
    assert any("agreed to be tracked" in x for x in r.reasons), r.reasons
    assert w.rating("Alice") is None


def test_the_refusal_never_names_the_other_accounts():
    """Saying *which* Steam IDs the server holds would make this a lookup
    service for who is registered."""
    w = World(consent=("Alice",))
    w.store.sanction_lobby(111)
    r = w.duel()
    joined = " ".join(r.reasons)
    for sid in STEAM.values():
        assert sid not in joined, joined


def test_only_someone_in_the_series_may_report_it():
    w = World()
    w.store.sanction_lobby(111)
    try:
        w.duel(by="Carol")
    except AuthError as e:
        assert "not in this series" in str(e), str(e)
    else:
        raise AssertionError("an outsider reported someone else's series")


def test_an_impossible_score_is_refused():
    w = World()
    w.store.sanction_lobby(111)
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
    w.store.sanction_lobby(111)
    w.duel()
    after_one = w.rating("Alice")
    w.duel(by="Bob")
    assert len(w.store.load_results()) == 1
    assert w.rating("Alice") == after_one


def test_two_series_in_one_lobby_on_one_evening_stay_two():
    """A Bo3 and then another Bo3 without leaving the lobby. Keyed by day alone
    the second would have been swallowed as a duplicate of the first."""
    w = World()
    w.store.sanction_lobby(111)
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
    w.store.sanction_lobby(111)
    w.duel()
    assert w.rating("Alice") is not None

    w.auth.set_tracking_consent(w.people["Bob"], False)
    assert w.rating("Alice") is None, \
        "a series stayed rated after one side withdrew"


def test_a_win_and_a_loss_move_in_opposite_directions():
    w = World()
    w.store.sanction_lobby(111)
    w.store.sanction_lobby(222)
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
    w.store.sanction_lobby(111)
    w.duel()
    assert w.rating("Alice") > 1500, "the seed was ignored and Alice began at 1000"


def test_a_player_only_known_from_reports_still_appears():
    w = World(consent=("Carol", "Dave"))
    w.store.sanction_lobby(111)
    w.duel("Carol", "Dave")
    w.results.refresh_ranking()
    names = {p["name"] for p in w.ranking.players}
    assert {"Carol", "Dave"} <= names, names


def test_under_ten_games_a_rating_is_marked_provisional():
    w = World()
    w.store.sanction_lobby(111)
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
    w.store.sanction_lobby(111)
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
