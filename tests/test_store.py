"""Tests for persistence and grants.

The core: after a restart everything has to be back — accounts, roles,
grants, running tournaments in the state they were in. A ranking that does
not survive a server restart is not one.

Every test gets its own database file in a temp folder.
"""

import sqlite3
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.modes import TOURNAMENT_1V1  # noqa: E402
from ladder.tournament import Participant, Tournament  # noqa: E402
from server.auth import AuthError, AuthService, Grant, Role  # noqa: E402
from server.store import Store  # noqa: E402


def fresh_store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "ladder.sqlite")


def people(n: int) -> list[Participant]:
    return [Participant(f"P{i}", 2100 - i * 25) for i in range(1, n + 1)]


# ----------------------------------------------------------------- Grants
def test_a_grant_unlocks_without_promoting():
    """A tournament host may create tournaments — but not touch accounts."""
    auth = AuthService()
    boss = auth.login_discord("1", "Boss"); boss.role = Role.ADMIN
    organiser = auth.login_discord("2", "Organiser")
    assert not organiser.may("create_tournament")
    auth.grant_permission(boss, organiser, Grant.TOURNAMENT_HOST)
    assert organiser.may("create_tournament")
    assert not organiser.may("link_other_account"), "the grant promoted them"
    assert organiser.role is Role.PLAYER


def test_the_refusal_names_both_ways_in():
    """Otherwise nobody learns a grant exists alongside the rank."""
    auth = AuthService()
    a = auth.login_discord("1", "Rookie")
    try:
        a.require("create_tournament")
    except AuthError as e:
        assert "Admin" in str(e) and "Tournament Host" in str(e), str(e)
    else:
        raise AssertionError("the refusal never came")


def test_a_referee_may_correct_results_a_caster_may_not():
    auth = AuthService()
    boss = auth.login_discord("0", "Boss"); boss.role = Role.ADMIN
    referee = auth.login_discord("1", "Referee")
    caster = auth.login_discord("2", "Caster")
    auth.grant_permission(boss, referee, Grant.REFEREE)
    auth.grant_permission(boss, caster, Grant.CASTER)
    assert referee.may("report_any_match")
    assert not caster.may("report_any_match")
    # Both may watch even when the host has closed requests.
    assert caster.may("override_observer_lock")


def test_granting_needs_admin():
    auth = AuthService()
    a = auth.login_discord("1", "Player")
    b = auth.login_discord("2", "Other")
    try:
        auth.grant_permission(a, b, Grant.TOURNAMENT_HOST)
    except AuthError:
        pass
    else:
        raise AssertionError("a player handed out a grant")


def test_a_grant_can_be_taken_away_again():
    auth = AuthService()
    boss = auth.login_discord("1", "Boss"); boss.role = Role.ADMIN
    x = auth.login_discord("2", "X")
    auth.grant_permission(boss, x, Grant.TOURNAMENT_HOST)
    auth.revoke_permission(boss, x, Grant.TOURNAMENT_HOST)
    assert not x.may("create_tournament")


# ------------------------------------------------------------ Persistence
def test_accounts_and_grants_survive_a_restart():
    store = fresh_store()
    auth = AuthService()
    boss = auth.login_discord("1", "Boss"); boss.role = Role.OWNER
    organiser = auth.login_discord("2", "Organiser")
    auth.attach_steam(organiser, "76561190000000001")
    organiser.ufer_name = "SecondSeed"
    auth.grant_permission(boss, organiser, Grant.TOURNAMENT_HOST)
    auth.grant_permission(boss, organiser, Grant.CASTER)
    for a in auth.accounts.values():
        store.save_account(a)

    reloaded = store.restore_auth(AuthService())
    l2 = next(a for a in reloaded.accounts.values() if a.discord_name == "Organiser")
    assert l2.steam_id == "76561190000000001"
    assert l2.ufer_name == "SecondSeed"
    assert l2.grants == {Grant.TOURNAMENT_HOST, Grant.CASTER}
    assert l2.may("create_tournament")
    assert next(a for a in reloaded.accounts.values()
                if a.discord_name == "Boss").role is Role.OWNER


def test_a_revoked_grant_does_not_come_back_after_a_restart():
    """Saving reconciles rather than only adding."""
    store = fresh_store()
    auth = AuthService()
    boss = auth.login_discord("1", "Boss"); boss.role = Role.ADMIN
    x = auth.login_discord("2", "X")
    auth.grant_permission(boss, x, Grant.TOURNAMENT_HOST)
    store.save_account(x)
    auth.revoke_permission(boss, x, Grant.TOURNAMENT_HOST)
    store.save_account(x)
    assert store.load_accounts()[x.id].grants == set()


def test_sessions_survive_but_expired_ones_are_swept_away():
    store = fresh_store()
    auth = AuthService()
    a = auth.login_discord("1", "X")
    store.save_account(a)
    good = auth.start_session(a)
    store.save_session(good)

    from server.auth import Session
    old = Session("stale", a.id, 0.0, 1.0)        # long expired
    store.save_session(old)

    loaded = store.load_sessions()
    assert good.token in loaded
    assert "stale" not in loaded, "an expired session was loaded"


def test_two_accounts_cannot_share_a_steam_id_in_the_database():
    """The database holds the rule even when the code sidesteps it."""
    store = fresh_store()
    auth = AuthService()
    a = auth.login_discord("1", "A")
    b = auth.login_discord("2", "B")
    a.steam_id = b.steam_id = "76561190000000001"      # bypassing the check
    store.save_account(a)
    import sqlite3
    try:
        store.save_account(b)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("the database allowed a duplicate SteamID")


# ------------------------------------------------------------- Tournaments
def test_a_bracket_is_built_deterministically():
    """The precondition for load = rebuild + replay results working."""
    a = Tournament("Cup", people(6))
    b = Tournament("Cup", people(6))
    assert [m.label() for r in a.rounds for m in r] == \
           [m.label() for r in b.rounds for m in r]


def test_a_tournament_survives_a_restart_with_its_progress():
    store = fresh_store()
    t = Tournament("Sommercup", people(6), mode=TOURNAMENT_1V1)
    store.create_tournament("t1", t)

    first = t.playable()[0]
    t.report(first.id, first.b.name, (3, 2))      # the underdog wins
    store.record_result("t1", first.id, first.b.name, (3, 2), ["replay:x.fwr"])

    reloaded = store.load_tournament("t1")
    assert reloaded.name == "Sommercup"
    assert reloaded.match(first.id).winner.name == first.b.name
    assert reloaded.match(first.id).score == (3, 2)
    assert reloaded.match(first.id).match_keys == ["replay:x.fwr"]
    # The underdog is in the next round.
    assert [m.label() for m in reloaded.playable()] == \
           [m.label() for m in t.playable()]


def test_a_finished_tournament_reloads_with_its_champion():
    store = fresh_store()
    t = Tournament("Cup", people(8))
    store.create_tournament("t2", t)
    while not t.finished:
        m = t.playable()[0]
        t.report(m.id, m.a.name, (3, 0))
        store.record_result("t2", m.id, m.a.name, (3, 0))
    store.mark_finished("t2")

    reloaded = store.load_tournament("t2")
    assert reloaded.finished
    assert reloaded.champion.name == t.champion.name == "P1"


def test_results_are_replayed_in_the_order_they_were_reported():
    """A different order = a winner in a round that does not exist yet."""
    store = fresh_store()
    t = Tournament("Cup", people(4))
    store.create_tournament("t3", t)
    order = []
    while not t.finished:
        m = t.playable()[0]
        t.report(m.id, m.a.name, (3, 0))
        store.record_result("t3", m.id, m.a.name, (3, 0))
        order.append(m.id)
    rows = [r["match_id"] for r in store.db.execute(
        "SELECT match_id FROM tournament_results WHERE tournament_id='t3' "
        "ORDER BY seq")]
    assert rows == order
    store.load_tournament("t3")          # must not raise


def test_listing_shows_participant_counts():
    store = fresh_store()
    store.create_tournament("a", Tournament("Klein", people(4)))
    store.create_tournament("b", Tournament("Gross", people(12)))
    listing = {r["name"]: r["participants"] for r in store.list_tournaments()}
    assert listing == {"Klein": 4, "Gross": 12}


def test_sanctioned_lobbies_persist_and_ignore_duplicates():
    """The server list is what lets a guest's client agree that a match
    counts, so it has to survive a restart."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.sqlite"
        store = Store(path)
        store.sanction_lobby(109775243033698881, series_id="s1")
        store.sanction_lobby(109775243033698881)      # re-host of the same lobby
        assert store.sanctioned_lobbies() == [109775243033698881]
        store.close()

        again = Store(path)
        assert again.sanctioned_lobbies() == [109775243033698881]
        again.close()


def test_consent_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.sqlite"
        store = Store(path)
        auth = AuthService()
        a = auth.login_discord("1", "X")
        auth.attach_steam(a, "76561190000000001")
        auth.set_tracking_consent(a, True)
        store.save_account(a)
        store.close()

        again = Store(path)
        loaded = again.load_accounts()[a.id]
        assert loaded.tracking_consent and loaded.consent_since
        assert loaded.trackable
        # Windows keeps the file locked while the handle is open, and the
        # temp directory cannot be removed then.
        again.close()


def test_a_database_from_before_the_consent_column_still_opens():
    """`CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so
    without a migration every write would fail with "no such column"."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.sqlite"
        db = sqlite3.connect(path)
        db.executescript(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, discord_id TEXT "
            "UNIQUE, discord_name TEXT, steam_id TEXT UNIQUE, ufer_name TEXT "
            "UNIQUE, role INTEGER NOT NULL DEFAULT 1, created_at REAL NOT "
            "NULL);")
        db.execute("INSERT INTO accounts VALUES "
                   "('old', NULL, 'Legacy', NULL, NULL, 1, 0)")
        db.commit()
        db.close()

        store = Store(path)
        accounts = store.load_accounts()
        # The important half: an account that predates the column must not
        # come back as having agreed to something it never saw.
        assert accounts["old"].tracking_consent is False
        assert not accounts["old"].trackable
        store.close()


def test_a_draft_survives_a_restart_exactly():
    """Stored as setup plus moves, replayed through the engine — so a restored
    draft can only be a state the engine would produce itself.

    The neutrally struck map is the part that breaks silently: `Draft` removes
    it from its own pool, so saving that pool and rebuilding from it would
    strike a second map and change the board under the players.
    """
    from ladder.draft import Side
    from server.draft import DraftService

    maps = ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals", "Moorings"]
    cmds = [f"commander-x-{n}" for n in "abcdef"]
    auth = AuthService()
    seats = []
    for i, n in enumerate(("A", "B"), start=1):
        acc = auth.login_discord(str(i), n)
        acc.role = Role.PLAYER
        auth.attach_steam(acc, f"7656119900000{i:04d}")
        auth.set_tracking_consent(acc, True)
        seats.append(acc)
    a, b = seats

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.sqlite"
        store = Store(path)
        svc = DraftService()
        s = svc.create(a, maps, cmds, best_of=3, step_seconds=None)
        svc.join(b, s.join_code)
        for _ in range(3):
            step = s.draft.current
            actor = a if step.side is Side.A else b
            s.apply(actor, s.draft.legal_options(step.side)[0])
            store.save_draft(s)
        before = (s.draft.step_index, s.draft.banned_maps(),
                  dict(s.draft.picked_maps()), s.draft.neutral_strike)
        store.close()

        again = Store(path)
        svc2 = DraftService()
        assert svc2.restore(again.load_drafts()) == 1
        s2 = svc2.get(s.id)
        after = (s2.draft.step_index, s2.draft.banned_maps(),
                 dict(s2.draft.picked_maps()), s2.draft.neutral_strike)
        assert after == before, f"{before} became {after}"
        assert s2.join_code == s.join_code
        assert {x.side for x in s2.seats.values()} == {Side.A, Side.B}
        again.close()


def test_the_lobby_and_a_cancellation_survive_a_restart():
    """Both are decisions, not derived state. A lobby id that came back empty
    would leave a finished series with no way to tell which games were it, and
    a cancelled draft coming back alive would hand somebody a dead board.

    Also covers the migration: the columns are added to an existing table, and
    `CREATE TABLE IF NOT EXISTS` does not add a column.
    """
    from ladder.draft import Side
    from server.draft import DraftService

    maps = ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals", "Moorings"]
    cmds = [f"commander-x-{n}" for n in "abcdef"]
    auth = AuthService()
    people = []
    for i, n in enumerate(("A", "B", "C", "D"), start=1):
        acc = auth.login_discord(str(i), n)
        acc.role = Role.PLAYER
        auth.attach_steam(acc, f"7656119900000{i:04d}")
        auth.set_tracking_consent(acc, True)
        people.append(acc)
    a, b, c, d_ = people

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.sqlite"
        store = Store(path)
        svc = DraftService()

        finished = svc.create(a, maps, cmds, best_of=1, step_seconds=None)
        svc.join(b, finished.join_code)
        while finished.draft.current is not None:
            step = finished.draft.current
            if step.side is None:
                for side, actor in ((Side.A, a), (Side.B, b)):
                    if finished.draft.legal_options(side):
                        finished.apply(actor, finished.draft.legal_options(side)[0])
            else:
                actor = a if step.side is Side.A else b
                finished.apply(actor, finished.draft.legal_options(step.side)[0])
        finished.set_lobby(a, 109775243190123456)
        store.save_draft(finished)

        walked = svc.create(c, maps, cmds, best_of=1, step_seconds=None)
        svc.join(d_, walked.join_code)
        walked.cancel(d_)
        store.save_draft(walked)
        store.close()

        again = Store(path)
        svc2 = DraftService()
        svc2.restore(again.load_drafts())
        assert svc2.get(finished.id).lobby_id == 109775243190123456
        assert svc2.get(finished.id).lobby_host == "A"
        assert svc2.get(walked.id).cancelled_by == "D" or                svc2.get(walked.id).cancelled_by == "B",                svc2.get(walked.id).cancelled_by
        assert svc2.get(walked.id).cancelled
        again.close()


def test_a_void_and_the_played_games_survive_a_restart():
    """Both are agreements between two people. A void that came back to life,
    or a played game that had to be replayed, would be worse than not having
    the feature."""
    from ladder.draft import Side
    from server.draft import DraftService

    maps = ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals", "Moorings"]
    cmds = [f"commander-x-{n}" for n in "abcdef"]
    auth = AuthService()
    people = []
    for i, n in enumerate(("A", "B"), start=1):
        acc = auth.login_discord(str(i), n)
        acc.role = Role.PLAYER
        auth.attach_steam(acc, f"7656119900000{i:04d}")
        auth.set_tracking_consent(acc, True)
        people.append(acc)
    a, b = people

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v.sqlite"
        store = Store(path)
        svc = DraftService()
        s = svc.create(a, maps, cmds, best_of=3, step_seconds=None)
        svc.join(b, s.join_code)
        while s.draft.current is not None:
            step = s.draft.current
            if step.side is None:
                for side, actor in ((Side.A, a), (Side.B, b)):
                    if s.draft.legal_options(side):
                        s.apply(actor, s.draft.legal_options(side)[0])
            else:
                actor = a if step.side is Side.A else b
                s.apply(actor, s.draft.legal_options(step.side)[0])

        s.note_game(a, 1, "A")
        s.note_game(a, 2, "B")
        s.request_void(a, "game:2", "crash")
        s.request_void(b, "game:2")
        before = (s.wins(), sorted(s.voided_games), s.draft.revealed_through())
        store.save_draft(s)
        store.close()

        again = Store(path)
        svc2 = DraftService()
        svc2.restore(again.load_drafts())
        s2 = svc2.get(s.id)
        after = (s2.wins(), sorted(s2.voided_games), s2.draft.revealed_through())
        assert after == before, f"{before} became {after}"
        assert s2.wins() == {"A": 1, "B": 0}
        again.close()


def test_a_stale_draft_is_not_restored():
    """A board nobody finished yesterday is not something to resume."""
    from server.draft import DraftService

    auth = AuthService()
    acc = auth.login_discord("1", "A")
    acc.role = Role.PLAYER
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "x.sqlite")
        svc = DraftService()
        s = svc.create(acc, ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals"],
                       [f"commander-x-{n}" for n in "abcdef"], step_seconds=None)
        s.created_at = time.time() - 48 * 3600
        store.save_draft(s)
        assert store.load_drafts(max_age_s=24 * 3600) == []
        store.close()


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
