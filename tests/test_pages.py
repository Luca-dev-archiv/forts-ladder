"""Tests for the admin and tournament-host pages.

These are the only pages that change anything, so what matters is not that
they render but who they refuse. An admin page that shows a role picker to
someone who cannot use it, or a bracket that lets a spectator report a
result, is worse than no page at all.

Driven through the real ASGI app rather than by calling the functions: the
permission check, the form parsing and the redirect after a POST are all part
of what is being tested, and none of them exist below the route.

Needs `httpx` (see requirements-dev.txt); skipped, not failed, without it, so
the suite still runs on a machine that only has the server's dependencies.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as _e:  # httpx missing
    print(f"skipped: {_e}")
    sys.exit(0)

# Own database and no ranking seed, so a run cannot touch a real one. Set
# before the module is imported: the app opens both at import time.
os.environ["LADDER_DB"] = os.path.join(tempfile.mkdtemp(), "pages.sqlite")
os.environ["LADDER_SEED"] = str(Path(tempfile.gettempdir()) / "no-such-seed.json")

import server.app as app_mod  # noqa: E402

app_mod = importlib.reload(app_mod)
from server.auth import Grant, Role  # noqa: E402

ENTRANTS = "Alice, 1400\nBob,1200\n\nCarol\nDave, 1100\nO'Neil, Jr, 1500\n"


#: Every account in the file needs its own Steam ID — two accounts sharing one
#: is refused, and rightly so. Counted across worlds, not per world.
_ids = iter(range(1, 10_000))


class World:
    """One app, one client, and accounts made the way logging in makes them."""

    def __init__(self) -> None:
        self.client = TestClient(app_mod.app)

    def person(self, name: str, role: Role = Role.PLAYER, *grants: Grant):
        n = next(_ids)
        acc = app_mod.auth.login_discord(f"discord-{name}-{n}", name)
        acc.role = role
        acc.grants = set(grants)
        # A structurally valid id that is nobody's: the tests check that other
        # people's ids stay off the pages.
        app_mod.auth.attach_steam(acc, f"7656119900000{n:04d}")
        app_mod.auth.set_tracking_consent(acc, True)
        app_mod.store.save_account(acc)
        session = app_mod.auth.start_session(acc)
        return acc, {"Authorization": f"Bearer {session.token}"}


def cup(w: World, headers: dict, entrants: str = ENTRANTS,
        start: bool = True) -> str:
    """Create a tournament and, by default, start it.

    Creating now lands in the planner — a host adds people and looks at the
    bracket before anything is fixed — so a test that wants a runnable bracket
    has to say so.
    """
    r = w.client.post("/manage/tournaments", headers=headers,
                      follow_redirects=False,
                      data={"name": "Summer Cup", "mode": "tournament_1v1",
                            "entrants": entrants})
    assert r.status_code == 303, r.text[:300]
    tid = r.headers["location"].rsplit("/", 1)[1]
    assert "/manage/plan/" in r.headers["location"], r.headers["location"]
    if start:
        s = w.client.post(f"/manage/plan/{tid}", headers=headers,
                          follow_redirects=False, data={"do": "start"})
        assert s.status_code == 303, s.text[:300]
    return tid


# ------------------------------------------------------------ Who sees what
def test_the_nav_offers_only_what_the_account_may_reach():
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    _, plain = w.person("Plain")

    assert "/admin" in w.client.get("/", headers=owner).text
    assert "/manage/tournaments" in w.client.get("/", headers=host).text
    body = w.client.get("/", headers=plain).text
    assert "/admin" not in body and "/manage/tournaments" not in body


def test_the_pages_refuse_the_accounts_that_may_not_use_them():
    w = World()
    _, plain = w.person("Plain")
    assert w.client.get("/admin", headers=plain).status_code == 403
    assert w.client.get("/manage/tournaments", headers=plain).status_code == 403


def test_a_visitor_who_is_not_signed_in_is_sent_to_sign_in():
    """Not a 401: these are pages, and a 401 body is a dead end in a browser."""
    w = World()
    for url in ("/admin", "/manage/tournaments", "/manage/tournaments/x"):
        r = w.client.get(url, follow_redirects=False)
        assert r.status_code == 303, f"{url} -> {r.status_code}"
        assert "/auth/discord/start" in r.headers["location"]


def test_only_an_owner_sees_the_role_picker():
    """An admin may hand out grants but not roles, so showing them a picker
    that refuses on submit would be a lie in the interface."""
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    _, admin = w.person("Admin", Role.ADMIN)
    assert "select name=role" in w.client.get("/admin", headers=owner).text
    assert "select name=role" not in w.client.get("/admin", headers=admin).text


# -------------------------------------------------------------- Admin saves
def test_grants_and_roles_are_written_to_the_database():
    """A promotion that only lives in memory is undone by the next restart."""
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    target, _ = w.person("Target")

    r = w.client.post("/admin/save", headers=owner, follow_redirects=False,
                      data={"account": target.id, "role": "caster",
                            "grant": ["referee", "tournament_host"]})
    assert r.status_code == 303
    stored = app_mod.store.load_accounts()[target.id]
    assert stored.role is Role.CASTER
    assert {g.value for g in stored.grants} == {"referee", "tournament_host"}

    # Unchecking has to remove them again — a checkbox that only ever adds is
    # a one-way door.
    w.client.post("/admin/save", headers=owner,
                  data={"account": target.id, "role": "caster",
                        "grant": ["referee"]})
    stored = app_mod.store.load_accounts()[target.id]
    assert {g.value for g in stored.grants} == {"referee"}


def test_an_admin_cannot_promote_and_is_told_why():
    w = World()
    _, admin = w.person("Admin", Role.ADMIN)
    target, _ = w.person("Target")
    r = w.client.post("/admin/save", headers=admin,
                      data={"account": target.id, "role": "owner"})
    assert r.status_code == 200, "a refusal should come back as the page"
    # The code names the refusal, and the missing role comes from our own
    # permission table rather than from the exception's words.
    assert "FL-600" in r.text and "needs Owner" in r.text
    assert target.role is Role.PLAYER, "the refusal must not have applied"


def test_you_cannot_change_your_own_row():
    """The one mistake on this page that cannot be undone from this page."""
    w = World()
    owner, headers = w.person("Owner", Role.OWNER)
    r = w.client.post("/admin/save", headers=headers,
                      data={"account": owner.id, "role": "guest"})
    assert "FL-604" in r.text and "your own row" in r.text
    assert owner.role is Role.OWNER


def test_a_bad_form_changes_nothing():
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    target, _ = w.person("Target", Role.PLAYER, Grant.CASTER)
    r = w.client.post("/admin/save", headers=owner,
                      data={"account": target.id, "grant": ["wizard"]})
    assert "Unknown role or grant" in r.text
    assert {g.value for g in target.grants} == {"caster"}
    assert w.client.post("/admin/save", headers=owner,
                         data={"account": "nobody"}).status_code == 404


def test_the_admin_page_shows_whether_the_server_is_set_up():
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    assert "not set" in w.client.get("/admin", headers=owner).text
    app_mod.queue.configure(["Abyss", "Pillars"], ["Bolt", "Dagger", "Rex"])
    body = w.client.get("/admin", headers=owner).text
    assert "2 maps, 3 commanders" in body


# -------------------------------------------------------------- Tournaments
def test_entrants_are_parsed_from_pasted_lines():
    """Hosts have the names in a message already; they paste them."""
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    t = app_mod.store.load_tournament(cup(w, host))
    assert [p.name for p in t.participants] == \
        ["Alice", "Bob", "Carol", "Dave", "O'Neil, Jr"]
    # A comma inside a name is not a rating, and a missing rating is 1000.
    assert t.participants[4].rating == 1500.0
    assert t.participants[2].rating == 1000.0


def test_a_refused_tournament_keeps_what_was_typed():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    r = w.client.post("/manage/tournaments", headers=host,
                      data={"name": "", "entrants": "a\nb"})
    assert "Give the tournament a name" in r.text
    r = w.client.post("/manage/tournaments", headers=host,
                      data={"name": "Solo", "entrants": "just me"})
    assert "at least two entrants" in r.text
    assert "just me" in r.text, "the list was thrown away"


def test_a_referee_gets_the_list_without_the_create_form():
    """A referee corrects results in brackets someone else built, so they need
    to find one — but building is not theirs."""
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    _, ref = w.person("Ref", Role.PLAYER, Grant.REFEREE)
    tid = cup(w, host)

    body = w.client.get("/manage/tournaments", headers=ref).text
    assert "Summer Cup" in body
    assert "New tournament" not in body, "a referee was offered the create form"
    assert w.client.post("/manage/tournaments", headers=ref,
                         data={"name": "Mine", "entrants": "a\nb"}
                         ).status_code == 403

    # And they can report, which is the whole point of the grant.
    m = app_mod.store.load_tournament(tid).playable()[0]
    r = w.client.post(f"/manage/tournaments/{tid}/report", headers=ref,
                      follow_redirects=False,
                      data={"match": m.id, "winner": m.a.name, "score": "3:0"})
    assert r.status_code == 303, r.text[:200]


def test_the_bracket_is_readable_by_a_player_but_reportable_by_a_host():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    _, plain = w.person("Plain")
    tid = cup(w, host)

    body = w.client.get(f"/manage/tournaments/{tid}", headers=host).text
    assert "Summer Cup" in body and "Bo5" in body and "Report" in body

    body = w.client.get(f"/manage/tournaments/{tid}", headers=plain).text
    assert "Summer Cup" in body, "an entrant may look at their own bracket"
    assert "Report" not in body
    assert "All tournaments" not in body, "offered a page they cannot open"


def test_a_result_advances_the_bracket_and_survives_a_reload():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, host)
    m = app_mod.store.load_tournament(tid).playable()[0]

    r = w.client.post(f"/manage/tournaments/{tid}/report", headers=host,
                      follow_redirects=False,
                      data={"match": m.id, "winner": m.a.name, "score": "3:1"})
    assert r.status_code == 303
    after = app_mod.store.load_tournament(tid)
    assert after.match(m.id).winner.name == m.a.name
    assert after.match(m.id).score == (3, 1)


def test_an_impossible_result_is_explained_and_not_applied():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    _, plain = w.person("Plain")
    tid = cup(w, host)
    m = app_mod.store.load_tournament(tid).playable()[0]
    url = f"/manage/tournaments/{tid}/report"

    for data, expected in (
            ({"match": m.id, "winner": "Nobody"}, "FL-504"),
            ({"match": m.id, "winner": m.b.name, "score": "banana"},
             "is not a score"),
            # 2:1 does not decide a Bo5.
            ({"match": m.id, "winner": m.b.name, "score": "2-1"},
             "FL-505")):
        r = w.client.post(url, headers=host, data=data)
        assert r.status_code == 200 and expected in r.text, r.text[-300:]

    assert w.client.post(url, headers=plain,
                         data={"match": m.id, "winner": m.b.name}
                         ).status_code == 403
    assert app_mod.store.load_tournament(tid).match(m.id).winner is None


def test_a_hostile_tournament_name_cannot_inject_markup():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    r = w.client.post("/manage/tournaments", headers=host,
                      follow_redirects=False,
                      data={"name": "<img src=x onerror=alert(1)>",
                            "entrants": "a\nb"})
    tid = r.headers["location"].rsplit("/", 1)[1]
    body = w.client.get(f"/manage/tournaments/{tid}", headers=host).text
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body


def test_no_other_persons_steam_id_reaches_a_players_page():
    """The admin page is the only place ids appear, and it is admin-only."""
    w = World()
    other, _ = w.person("Other", Role.OWNER)
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    me, plain = w.person("Plain")
    tid = cup(w, host)
    for url in ("/", f"/manage/tournaments/{tid}"):
        body = w.client.get(url, headers=plain).text
        assert other.steam_id not in body, f"leaked into {url}"
    # Your own id is yours to see.
    assert me.steam_id in w.client.get("/", headers=plain).text


# ------------------------------------------------ Held names and bracket setup
def test_a_held_ladder_name_is_shown_and_can_be_confirmed():
    """The route somebody listed under a different name needs. Refusing the
    claim used to throw it away, so there was nothing for an admin to see."""
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    player, ph = w.person("Player")

    r = w.client.post("/me/ufer_name", headers=ph, json={"name": "TopSeed"})
    assert r.status_code == 200 and r.json()["applied"] is False
    assert r.json()["pending"] == "TopSeed"

    body = w.client.get("/admin", headers=owner).text
    assert "TopSeed" in body and "Confirm" in body

    r = w.client.post("/admin/name", headers=owner, follow_redirects=False,
                      data={"account": player.id, "decision": "confirm"})
    assert r.status_code == 303
    assert app_mod.store.load_accounts()[player.id].ufer_name == "TopSeed"


def test_rejecting_a_held_name_leaves_the_account_without_one():
    w = World()
    _, owner = w.person("Owner", Role.OWNER)
    player, ph = w.person("Player")
    w.client.post("/me/ufer_name", headers=ph, json={"name": "Someone"})
    w.client.post("/admin/name", headers=owner,
                  data={"account": player.id, "decision": "reject"})
    stored = app_mod.store.load_accounts()[player.id]
    assert stored.ufer_name is None and stored.ufer_claim is None


def test_a_matching_name_needs_no_admin():
    w = World()
    acc, h = w.person("Dranistian")
    r = w.client.post("/me/ufer_name", headers=h, json={"name": "Dranistian"})
    assert r.json()["applied"] is True
    assert app_mod.store.load_accounts()[acc.id].ufer_name == "Dranistian"


def test_the_admin_page_shows_a_steam_name_rather_than_the_id():
    w = World()
    acc, h = w.person("Owner", Role.OWNER)
    w.client.put("/me/steam_name", headers=h, json={"name": "local_player"})
    body = w.client.get("/admin", headers=h).text
    assert "local_player" in body
    assert app_mod.store.load_accounts()[acc.id].steam_name == "local_player"


def test_a_bracket_can_be_seeded_by_the_listed_order():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    r = w.client.post("/manage/tournaments", headers=host,
                      follow_redirects=False,
                      data={"name": "Listed", "mode": "tournament_1v1",
                            "seeding": "listed", "best_of": "3",
                            "entrants": "Dave, 900\nAlice, 2000"})
    tid = r.headers["location"].rsplit("/", 1)[1]
    t = app_mod.store.load_tournament(tid)
    assert t.seeding == "listed" and t.best_of == 3
    assert t.match("R1M1").a.name == "Dave", "the ratings decided anyway"
    # And it survives being rebuilt from storage.
    assert app_mod.store.load_tournament(tid).series_length() == 3


def test_an_entrant_can_be_renamed_from_the_page():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, host)
    r = w.client.post(f"/manage/tournaments/{tid}/rename", headers=host,
                      follow_redirects=False, data={"seat": "0", "name": "Alicia"})
    assert r.status_code == 303, r.text[:200]
    names = [p.name for p in app_mod.store.load_tournament(tid).participants]
    assert "Alicia" in names and "Alice" not in names


def test_renaming_is_refused_once_a_result_is_in():
    w = World()
    _, host = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, host)
    m = app_mod.store.load_tournament(tid).playable()[0]
    w.client.post(f"/manage/tournaments/{tid}/report", headers=host,
                  data={"match": m.id, "winner": m.a.name, "score": "3:0"})
    r = w.client.post(f"/manage/tournaments/{tid}/rename", headers=host,
                      data={"seat": "0", "name": "Nope"})
    assert r.status_code == 200 and "result has been reported" in r.text
    names = [p.name for p in app_mod.store.load_tournament(tid).participants]
    assert "Nope" not in names


# ------------------------------------------------------------------- Planner
# A tournament used to be a form: a name, a mode, and a textarea of entrants,
# after which nothing could be changed. That is data entry, not planning.
def plan(w: World, headers: dict, tid: str, **data):
    r = w.client.post(f"/manage/plan/{tid}", headers=headers,
                      follow_redirects=False, data=data)
    assert r.status_code in (200, 303), r.text[:300]
    return r


def entrant_names(w: World, headers: dict, tid: str) -> list[str]:
    t = app_mod.store.load_tournament(tid)
    return [x.name for x in t.participants]


def test_a_new_tournament_lands_in_the_planner_not_in_a_bracket():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, start=False)
    body = w.client.get(f"/manage/plan/{tid}", headers=h).text
    assert "Add an entrant" in body
    assert "Start the tournament" in body
    # And the bracket page hands a host straight back to it, so there is one
    # place a half-built tournament lives.
    r = w.client.get(f"/manage/tournaments/{tid}", headers=h,
                     follow_redirects=False)
    assert r.status_code == 303 and f"/manage/plan/{tid}" in r.headers["location"]


def test_an_entrant_can_be_added_dropped_and_moved():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, entrants="Alice, 1400\nBob, 1200\n", start=False)

    plan(w, h, tid, do="add", name="Carol", rating="1300")
    assert entrant_names(w, h, tid) == ["Alice", "Bob", "Carol"]

    plan(w, h, tid, do="up", seat="2")
    assert entrant_names(w, h, tid) == ["Alice", "Carol", "Bob"]

    plan(w, h, tid, do="remove", seat="0")
    assert entrant_names(w, h, tid) == ["Carol", "Bob"]

    plan(w, h, tid, do="edit", seat="1", name="Bobby", rating="1250")
    t = app_mod.store.load_tournament(tid)
    assert [x.name for x in t.participants] == ["Carol", "Bobby"]
    assert t.participants[1].rating == 1250


def test_a_pasted_list_is_added_one_per_line():
    """A sign-up list arrives as a block, and retyping it is how names get
    misspelled."""
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, entrants="Alice, 1400\n", start=False)
    plan(w, h, tid, do="add", name="Bob, 1200\nCarol\nDave, 1100")
    assert entrant_names(w, h, tid) == ["Alice", "Bob", "Carol", "Dave"]


def test_the_same_person_is_not_added_twice():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, entrants="Alice, 1400\n", start=False)
    plan(w, h, tid, do="add", name="Alice")
    assert entrant_names(w, h, tid) == ["Alice"]


def test_the_format_can_be_changed_while_planning():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, start=False)
    plan(w, h, tid, do="format", name="Winter Cup",
         mode="tournament_1v1", best_of="5", seeding="listed")
    t = app_mod.store.load_tournament(tid)
    assert t.name == "Winter Cup"
    assert t.series_length() == 5
    assert t.seeding == "listed"


def test_nothing_can_be_reported_before_the_tournament_starts():
    """A bracket built from entrants that can still change would move a stored
    result onto a different player."""
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, start=False)
    r = w.client.post(f"/manage/tournaments/{tid}/report", headers=h,
                      follow_redirects=False,
                      data={"match": "1", "winner": "0", "score": "2:0"})
    assert r.status_code == 400, r.status_code
    assert "not started" in r.text


def test_a_tournament_cannot_be_started_with_one_entrant():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, entrants="Alice, 1400\n", start=False)
    r = plan(w, h, tid, do="start")
    assert r.status_code == 200 and "two entrants" in r.text
    assert app_mod.store.is_planning(tid)


def test_starting_closes_the_planner():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h)                                    # starts it
    assert not app_mod.store.is_planning(tid)
    r = w.client.post(f"/manage/plan/{tid}", headers=h, data={"do": "add",
                                                             "name": "Late"})
    assert r.status_code == 400, r.status_code
    assert "already started" in r.text


def test_the_listing_says_which_tournaments_are_still_being_planned():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, start=False)
    body = w.client.get("/manage/tournaments", headers=h).text
    assert "being planned" in body
    assert f"/manage/plan/{tid}" in body


def test_a_player_cannot_plan_a_tournament():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    _, ph = w.person("Player")
    tid = cup(w, h, start=False)
    assert w.client.get(f"/manage/plan/{tid}", headers=ph).status_code == 403
    assert w.client.post(f"/manage/plan/{tid}", headers=ph,
                         data={"do": "add", "name": "X"}).status_code == 403


# ------------------------------------------------------- Watching your own match
# A spectator sees both forts. In your own series that is the one thing the blind
# pick exists to hide, so it would also be a way round every other rule about
# who may watch what.
def published(w: World, headers: dict, lobby: int = 4242) -> str:
    r = w.client.post("/live", headers=headers, json={
        "mode_key": "unranked_1v1", "mode_label": "Unranked 1v1",
        "players": ["one", "two"], "slots_used": 2, "slots_total": 9,
        "lobby_id": lobby})
    assert r.status_code == 200, r.text[:200]
    return r.json()["match_id"]


def test_the_host_cannot_watch_their_own_match():
    w = World()
    _, host = w.person("Host")
    mid = published(w, host)
    r = w.client.post(f"/live/{mid}/observe", headers=host)
    assert r.status_code == 403, r.status_code
    assert "playing in this match" in r.text


def test_the_listing_marks_your_own_match_for_you_and_not_for_others():
    w = World()
    _, host = w.person("Host")
    _, other = w.person("Someone")
    mid = published(w, host, lobby=5150)

    def row(headers):
        rows = w.client.get("/live", headers=headers).json()["matches"]
        return next(x for x in rows if x["id"] == mid)

    assert row(host)["yours"] is True
    assert row(other)["yours"] is False
    # Still public, and still without the lobby id for anyone not admitted.
    anon = w.client.get("/live").json()["matches"]
    assert next(x for x in anon if x["id"] == mid)["yours"] is False
    assert "lobby_id" not in next(x for x in anon if x["id"] == mid)


# ------------------------------------------------------------ Redirect targets
# `return_to` decides where somebody lands after proving who they are, which is
# exactly when a redirect is worth stealing: the link is ours, the login is
# real, and only the destination is not.
OFF_SITE = [
    "https://example.invalid/",
    "http://example.invalid/",
    "//example.invalid/",
    "/\\example.invalid/",
    "javascript:alert(1)",
    "",
    "manage/tournaments",          # no leading slash: not a path on this site
]


def test_only_a_path_on_this_site_survives_the_check():
    for bad in OFF_SITE:
        assert app_mod.safe_return_to(bad) == "/", bad
    for good in ("/", "/admin", "/manage/tournaments",
                 "/manage/plan/abc?x=1#y"):
        assert app_mod.safe_return_to(good) == good, good


def test_a_control_character_cannot_ride_along():
    """How header splitting is attempted."""
    for bad in ("/admin\r\nX-Evil: 1", "/admin\n", "/admin\x00", "/admin\t"):
        assert app_mod.safe_return_to(bad) == "/", repr(bad)


def test_the_login_refuses_to_send_anyone_off_site():
    # A client id, or the route refuses before it ever stores anything.
    os.environ["DISCORD_CLIENT_ID"] = "test-client-id"
    w = World()
    r = w.client.get("/auth/discord/start",
                     params={"return_to": "https://example.invalid/",
                             "json": 1})
    assert r.status_code == 200, r.text[:200]
    state = r.json()["state"]
    # What was stored is what the callback will honour, and it must be ours.
    assert app_mod.auth.pending[state].return_to == "/"


def test_a_tournament_id_cannot_shape_the_location():
    """The id is escaped whole, so it can never contribute a slash or a query."""
    assert app_mod.path_for("manage", "plan", "a/b") == "/manage/plan/a%2Fb"
    assert app_mod.path_for("manage", "plan", "..") == "/manage/plan/.."
    assert app_mod.path_for("manage", "plan", "x?y=1") == "/manage/plan/x%3Fy%3D1"


def test_an_unknown_tournament_does_not_echo_the_id_back():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    r = w.client.get("/manage/tournaments/nope-does-not-exist", headers=h)
    assert r.status_code == 404, r.status_code
    assert "nope-does-not-exist" not in r.text


# ------------------------------------------------- What a refusal may say
# CodeQL flagged the pages that show an exception's text. The `except` clauses
# were narrow enough, but an *unhandled* path sat right next to them: a rating
# of "nan" parses, cannot be stored, and turned a typo into a crashed request.
#: Anything here is a sentence Python wrote about the inside of the program,
#: rather than one written for the person reading it.
PYTHON_TALK = ("could not convert", "invalid literal", "math domain",
               "unsupported operand", "Traceback", "sqlite3", "NOT NULL",
               "object has no attribute", "not subscriptable",
               "IntegrityError", "constraint failed")


def test_an_impossible_rating_does_not_crash_the_route():
    """`float("nan")` and `float("1e999")` both parse. SQLite has no NaN, stores
    it as NULL, and the NOT NULL constraint turned a typo into a 500."""
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    for entrants in ("Alice, nan\nBob, 1200\n",
                     "Alice, 1e999\nBob, 1200\n",
                     "Alice, -nan\nBob, inf\n"):
        r = w.client.post("/manage/tournaments", headers=h,
                          follow_redirects=False,
                          data={"name": "Cup", "mode": "tournament_1v1",
                                "entrants": entrants})
        assert r.status_code in (200, 303), f"{entrants!r} -> {r.status_code}"


def test_a_nonsense_rating_in_the_planner_does_not_crash_either():
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, start=False)
    for rating in ("nan", "1e999", "-inf", "abc", ""):
        r = w.client.post(f"/manage/plan/{tid}", headers=h,
                          follow_redirects=False,
                          data={"do": "add", "name": f"P{rating or 'x'}",
                                "rating": rating})
        assert r.status_code in (200, 303), f"{rating!r} -> {r.status_code}"


def test_no_refusal_repeats_what_python_said():
    """Every message on these pages is one we wrote. A builtin's text names
    types and values from inside the program, which the reader can do nothing
    with."""
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, entrants="Alice, 1400\nBob, 1200\nCarol\nDave, 1\n")

    bodies = []
    for data in ({"match": "R1M1", "winner": "0", "score": "abc"},
                 {"match": "R1M1", "winner": "99", "score": "2:0"},
                 {"match": "nope", "winner": "0", "score": "2:0"},
                 {"match": "R1M1", "winner": "0", "score": "1:2:3"}):
        bodies.append(w.client.post(f"/manage/tournaments/{tid}/report",
                                    headers=h, follow_redirects=False,
                                    data=data).text)
    for data in ({"seat": "-1", "name": "X"}, {"seat": "99", "name": "X"},
                 {"seat": "0", "name": ""}):
        bodies.append(w.client.post(f"/manage/tournaments/{tid}/rename",
                                    headers=h, follow_redirects=False,
                                    data=data).text)
    for body in bodies:
        for phrase in PYTHON_TALK:
            assert phrase not in body, f"{phrase!r} reached a page"


def test_a_missing_seat_is_not_reported_as_a_missing_tournament():
    """Both lookups used to share one handler, so after the message became a
    fixed sentence a bad seat sent people looking for the wrong thing."""
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h)
    r = w.client.post(f"/manage/tournaments/{tid}/rename", headers=h,
                      follow_redirects=False, data={"seat": "99", "name": "X"})
    assert r.status_code == 200, r.status_code
    assert "FL-510" in r.text and "#99" in r.text
    assert "no such tournament" not in r.text

    r = w.client.post(f"/manage/tournaments/{tid}/report", headers=h,
                      follow_redirects=False,
                      data={"match": "R9M9", "winner": "0", "score": "2:0"})
    assert "FL-511" in r.text and "R9M9" in r.text
    assert "no such tournament" not in r.text


# ------------------------------------- Only authored text may reach a page
def test_only_a_rule_refusal_keeps_its_words():
    """`except ValueError` cannot tell the two apart, because both *are*
    ValueErrors. `json.JSONDecodeError` is one, and so is math domain error."""
    import json as _json

    from ladder.errors import RuleError
    from server.auth import AuthError

    # A code keeps its sentence; the sentence is ours, not the exception's.
    assert "FL-500" in app_mod.refusal(
        RuleError("FL-500", "a tournament needs at least two entrants"))
    assert "FL-601" in app_mod.refusal(
        AuthError("x already belongs to another account", "FL-601"))

    # Everything a library raises, including the ValueError subclasses.
    try:
        _json.loads("not json")
    except ValueError as e:
        leaked = app_mod.refusal(e)
    assert "Expecting value" not in leaked, leaked
    assert "line 1" not in leaked, leaked

    import math
    try:
        math.log2(0)
    except ValueError as e:
        assert "math domain" not in app_mod.refusal(e)

    for e in (ValueError("could not convert string to float: 'x'"),
              KeyError("some internal key"),
              RuntimeError("dict changed size during iteration")):
        out = app_mod.refusal(e)
        assert "convert" not in out and "internal" not in out and "dict" not in out


def test_a_damaged_row_does_not_take_the_page_down():
    """Every one of those columns was written by this program, so a value that
    will not parse means the row is damaged. One bad row must not make a whole
    tournament unreachable, and the parser's text must not land where a refusal
    would have been shown."""
    w = World()
    _, h = w.person("Host", Role.PLAYER, Grant.TOURNAMENT_HOST)
    tid = cup(w, h, entrants="Alice, 1400\nBob, 1200\n")

    app_mod.store.db.execute(
        "UPDATE tournament_participants SET members = ? WHERE tournament_id = ?",
        ("not json at all", tid))
    app_mod.store.db.commit()

    r = w.client.get(f"/manage/tournaments/{tid}", headers=h)
    assert r.status_code == 200, r.status_code
    for phrase in ("Expecting value", "line 1 column", "JSONDecode"):
        assert phrase not in r.text, phrase
    # And the entrant is still named, from the column that is intact.
    assert "Alice" in r.text


def test_nothing_but_the_code_crosses_the_boundary():
    """The load-bearing property. `refusal()` reads one attribute, checks it
    against a closed table, and returns a string written in this repository — so
    no message an exception carries can reach a page, whoever raised it."""
    from ladder.errors import RuleError
    from server.auth import AuthError

    # A refusal whose *message* is hostile and whose code is real: the message
    # must not appear, the code's own sentence must.
    e = RuleError("FL-500", "SECRET /var/lib/forts-ladder/ladder.sqlite")
    out = app_mod.refusal(e)
    assert "SECRET" not in out and "sqlite" not in out, out
    assert "FL-500" in out and "two entrants" in out

    # Same for AuthError, which the admin pages catch.
    out = app_mod.refusal(AuthError("SECRET internal detail", "FL-601"))
    assert "SECRET" not in out and "FL-601" in out

    # A code nobody wrote down is not printed either — it falls back.
    out = app_mod.refusal(RuleError("FL-<script>", "SECRET"))
    assert "script" not in out and "SECRET" not in out
    assert app_mod.UNKNOWN_REFUSAL in out


def test_every_code_a_rule_can_raise_has_a_sentence():
    """A code with no entry renders as the fallback, which would hide a real
    refusal behind "that did not work"."""
    import re

    raised = set(re.findall(r'RuleError\(\s*"(FL-\d+)"',
                            Path("ladder/tournament.py").read_text(encoding="utf-8")))
    raised |= set(re.findall(r'AuthError\([^)]*?"(FL-\d+)"',
                             Path("server/auth.py").read_text(encoding="utf-8"),
                             re.DOTALL))
    assert raised, "no codes found — the search is wrong, not the code"
    missing = sorted(c for c in raised if c not in app_mod.REFUSAL_TEXT)
    assert not missing, f"codes with no sentence: {missing}"


def test_the_codes_in_the_docs_match_the_codes_in_the_code():
    """A code that means two different things in two places is worse than none."""
    import re

    doc = Path("docs/error-codes.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(FL-\d+)`", doc))
    server_codes = {c for c in app_mod.REFUSAL_TEXT}
    undocumented = sorted(server_codes - documented)
    assert not undocumented, f"codes missing from the docs: {undocumented}"


# ------------------------------------------------- Replays for a review
# An upload endpoint is free storage and a path-traversal hole until it is not,
# so the interesting tests here are the refusals.
def reported_series(w: World, headers: dict, steam_id: str,
                    other: str = "76561199000000777") -> str:
    # A fresh draft id per call. It is the identity of a series now, so a shared
    # one would make every test in this file report the *same* series — which is
    # exactly what it is for, and useless here.
    r = w.client.post("/results", headers=headers, json={
        "sides": {steam_id: 1, other: 2},
        "games": 2, "score_low": 2,
        "played_at": "2026-07-30T21:00:00",
        "lobby_id": "4242", "draft_id": f"probe-{next(_ids)}"})
    assert r.status_code == 200, r.text[:200]
    return r.json()["id"]


def test_only_a_player_in_the_series_may_upload_a_replay():
    w = World()
    player, ph = w.person("Player")
    _, oh = w.person("Outsider")
    rid = reported_series(w, ph, player.steam_id)

    files = {"file": ("whatever.fwr", b"replay bytes",
                      "application/octet-stream")}
    ok = w.client.post(f"/results/{rid}/replay?index=1", headers=ph, files=files)
    assert ok.status_code == 200, ok.text[:200]

    files = {"file": ("whatever.fwr", b"replay bytes",
                      "application/octet-stream")}
    no = w.client.post(f"/results/{rid}/replay?index=1", headers=oh, files=files)
    assert no.status_code == 403, no.status_code


def test_the_stored_name_is_the_servers_not_the_uploaders():
    w = World()
    player, ph = w.person("Player")
    rid = reported_series(w, ph, player.steam_id)
    files = {"file": ("../../etc/passwd", b"x", "application/octet-stream")}
    r = w.client.post(f"/results/{rid}/replay?index=3", headers=ph, files=files)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["stored"] == "game03.fwr", r.json()


def test_an_oversized_replay_is_refused():
    w = World()
    player, ph = w.person("Player")
    rid = reported_series(w, ph, player.steam_id)
    big = b"x" * (app_mod.store.REPLAY_MAX_BYTES + 10)
    files = {"file": ("big.fwr", big, "application/octet-stream")}
    r = w.client.post(f"/results/{rid}/replay?index=1", headers=ph, files=files)
    assert r.status_code == 413, r.status_code


def test_only_a_reviewer_may_read_a_replay_back():
    """The players uploaded it; watching somebody's game afterwards is a
    reviewer's job, and the page it hangs off says so too."""
    w = World()
    player, ph = w.person("Player")
    _, admin = w.person("Ref", Role.ADMIN)
    rid = reported_series(w, ph, player.steam_id)
    files = {"file": ("g.fwr", b"bytes", "application/octet-stream")}
    w.client.post(f"/results/{rid}/replay?index=1", headers=ph, files=files)

    assert w.client.get(f"/manage/review/{rid}/replay/game01.fwr",
                        headers=admin).status_code == 200
    assert w.client.get(f"/manage/review/{rid}/replay/game01.fwr",
                        headers=ph).status_code == 403
    # And a name that is not there is not opened, whatever it points at.
    assert w.client.get(f"/manage/review/{rid}/replay/..%2F..%2Fladder.sqlite",
                        headers=admin).status_code == 404


def test_the_review_page_shows_what_a_reviewer_needs():
    w = World()
    player, ph = w.person("Player")
    _, admin = w.person("Ref", Role.ADMIN)
    rid = reported_series(w, ph, player.steam_id)
    body = w.client.get(f"/manage/review/{rid}", headers=admin).text
    assert "Series review" in body
    assert "Annul this series" in body
    # The player's ladder name, never their Steam ID: this page is for settling
    # a dispute, not for looking people up.
    assert player.steam_id not in body


def test_annulling_from_the_page_takes_the_rating_back():
    w = World()
    player, ph = w.person("Player")
    _, admin = w.person("Ref", Role.ADMIN)
    rid = reported_series(w, ph, player.steam_id)

    r = w.client.post(f"/manage/review/{rid}", headers=admin,
                      follow_redirects=False,
                      data={"do": "annul", "note": "wrong map in game 2"})
    assert r.status_code == 303, r.text[:200]
    row = app_mod.results.one(rid)
    assert not row.rated and "wrong map" in row.annul_note

    # And a player cannot.
    r = w.client.post(f"/manage/review/{rid}", headers=ph,
                      data={"do": "annul", "note": "no"})
    assert r.status_code == 403, r.status_code


# ------------------------------------------- Which lobbies were the ladder's
# The client keeps its own list as each draft hands off a lobby, but that list is
# per machine: a reinstall or a second computer loses it, and then a real ladder
# series looks like a casual game and cannot be sent to a referee.
def test_your_own_sanctioned_lobbies_come_back():
    w = World()
    player, ph = w.person("Player")
    app_mod.store.sanction_lobby(7777, "series-1", created_by=player.id)

    got = w.client.get("/lobbies/mine", headers=ph).json()["lobbies"]
    assert "7777" in got, got
    # Strings, because a Steam lobby id needs 64 bits and a JSON number is a
    # double — a rounded id matches no game.
    assert all(isinstance(x, str) for x in got)


def test_a_lobby_you_played_in_counts_even_if_you_did_not_host():
    """Only the host's client registers the lobby, so a guest would otherwise
    never learn that their own series was a ladder one."""
    w = World()
    host, hh = w.person("Host")
    guest, gh = w.person("Guest")
    app_mod.store.sanction_lobby(8888, "series-2", created_by=host.id)
    w.client.post("/results", headers=hh, json={
        "sides": {host.steam_id: 1, guest.steam_id: 2},
        "games": 2, "score_low": 2, "played_at": "2026-07-30T22:00:00",
        "lobby_id": "8888", "draft_id": f"lobbytest-{next(_ids)}"})

    assert "8888" in w.client.get("/lobbies/mine", headers=gh).json()["lobbies"]


def test_nobody_gets_a_directory_of_everybody_elses_lobbies():
    """The whole list would say who played where, which is not what a client
    needs to label its own history."""
    w = World()
    _, mine = w.person("Mine")
    other, _ = w.person("Other")
    app_mod.store.sanction_lobby(9999, "series-3", created_by=other.id)

    got = w.client.get("/lobbies/mine", headers=mine).json()["lobbies"]
    assert "9999" not in got, got


def test_an_unsanctioned_lobby_is_never_claimed_as_the_ladders():
    w = World()
    player, ph = w.person("Player")
    w.client.post("/results", headers=ph, json={
        "sides": {player.steam_id: 1, "76561199000000888": 2},
        "games": 2, "score_low": 2, "played_at": "2026-07-30T23:00:00",
        "lobby_id": "1234", "draft_id": f"lobbytest-{next(_ids)}"})
    got = w.client.get("/lobbies/mine", headers=ph).json()["lobbies"]
    assert "1234" not in got, got


# ----------------------------------------------------- Audit regressions
# Every one of these was verified against the live code before it was fixed.
def test_a_sanctioned_lobby_is_not_a_licence_to_invent_results():
    """The worst of them: `report` checked that the reporter's own Steam ID was
    in what they sent and that the lobby was sanctioned by *somebody*. Nothing
    tied the lobby to the opponent, so the normal way of getting a lobby
    sanctioned — host a real draft — handed out a token for inventing 3:0 wins
    against anybody with tracking on."""
    w = World()
    attacker, ah = w.person("Attacker")
    victim, _ = w.person("Victim")
    app_mod.store.sanction_lobby(51515, "mine", created_by=attacker.id,
                                 roster=[attacker.steam_id, "76561199000007777"])

    r = w.client.post("/results", headers=ah, json={
        "sides": {attacker.steam_id: 1, victim.steam_id: 2},
        "games": 3, "score_low": 3, "played_at": "2026-07-31T10:00:00",
        "lobby_id": "51515", "draft_id": f"forge-{next(_ids)}"})
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["rated"] is False, body
    assert any("not the players who drafted" in x for x in body["reasons"]), body


def test_a_lobby_with_no_recorded_roster_is_not_rated():
    """Belt and braces for lobbies sanctioned before the roster was kept."""
    w = World()
    player, ph = w.person("Player")
    app_mod.store.sanction_lobby(52525, "old", created_by=player.id)
    r = w.client.post("/results", headers=ph, json={
        "sides": {player.steam_id: 1, "76561199000006666": 2},
        "games": 2, "score_low": 2, "played_at": "2026-07-31T11:00:00",
        "lobby_id": "52525", "draft_id": f"forge-{next(_ids)}"})
    assert r.json()["rated"] is False
    assert any("no drafted roster" in x for x in r.json()["reasons"])


def test_somebody_who_did_not_draft_cannot_report_the_series_at_all():
    w = World()
    a, _ = w.person("Drafter")
    b, bh = w.person("Stranger")
    app_mod.store.sanction_lobby(53535, "theirs", created_by=a.id,
                                 roster=[a.steam_id, "76561199000005555"])
    r = w.client.post("/results", headers=bh, json={
        "sides": {b.steam_id: 1, a.steam_id: 2},
        "games": 2, "score_low": 2, "played_at": "2026-07-31T12:00:00",
        "lobby_id": "53535", "draft_id": f"forge-{next(_ids)}"})
    assert r.status_code == 403, r.text[:200]


def test_the_heartbeat_needs_the_host():
    """It took no credentials at all — the one state-changing route here that
    asked for nothing — and match ids are listed anonymously, so a passer-by
    could set a match to full and shut every spectator request out of it."""
    w = World()
    host, hh = w.person("Host")
    _, other = w.person("Passer-by")
    mid = published(w, hh, lobby=61616)

    assert w.client.post(f"/live/{mid}/heartbeat?slots_used=9").status_code == 401
    assert w.client.post(f"/live/{mid}/heartbeat?slots_used=9",
                         headers=other).status_code == 403
    row = next(x for x in w.client.get("/live").json()["matches"]
               if x["id"] == mid)
    assert row["free_slots"] > 0, "an outsider filled the match up"
    assert w.client.post(f"/live/{mid}/heartbeat?slots_used=3",
                         headers=hh).status_code == 200


def test_only_the_host_ends_their_own_live_match():
    """`require` established that somebody was logged in, not that it was
    theirs — while the two routes beside it check `host_account_id` properly."""
    w = World()
    host, hh = w.person("Host")
    _, other = w.person("Outsider")
    mid = published(w, hh, lobby=62626)

    assert w.client.delete(f"/live/{mid}", headers=other).status_code == 403
    assert any(x["id"] == mid for x in w.client.get("/live").json()["matches"])
    assert w.client.delete(f"/live/{mid}", headers=hh).status_code == 200


def test_the_steam_callback_is_bound_to_the_login_that_started_it():
    """The state was drawn and thrown away, and the callback never consumed one.
    A state-changing GET with no session binding, and `attach_steam` overwrites
    an existing link without asking."""
    w = World()
    r = w.client.get("/auth/steam/start", params={"json": 1})
    assert r.status_code == 200, r.text[:200]
    url = r.json()["url"]
    assert "state%3D" in url or "state=" in url, url

    # A callback with no state, or one nobody issued, is refused before anything
    # is attached.
    _, ph = w.person("Victim")
    for params in ({}, {"state": "not-a-state-anybody-issued"}):
        bad = w.client.get("/auth/steam/callback", params=params, headers=ph)
        assert bad.status_code in (400, 403), (params, bad.status_code)


def test_login_attempts_do_not_pile_up_for_ever():
    """`auth.pending` grew through an endpoint that needs no account and was
    never emptied."""
    w = World()
    _, ph = w.person("Player")
    for _ in range(25):
        w.client.get("/auth/steam/start", params={"json": 1})
    assert len(app_mod.auth.pending) >= 25
    for entry in app_mod.auth.pending.values():
        entry.created_at -= app_mod.auth.PENDING_TTL_S + 60
    w.client.get("/queue", headers=ph)      # the traffic does the sweeping
    assert app_mod.auth.pending == {}, app_mod.auth.pending


# --------------------------------------- Both clients get the same answer
# Two clients showed different labels for one match: the host has a lobby id in
# its log and the guest does not, and a guest's record of a game only exists if
# the game was accepted — which that one had not been. Both were reasoning
# locally from different evidence, so the answer moved to the server.
def test_both_players_are_told_the_same_thing_about_a_series():
    w = World()
    host, hh = w.person("Host")
    guest, gh = w.person("Guest")
    app_mod.store.sanction_lobby(70707, "s-1", created_by=host.id,
                                 roster=[host.steam_id, guest.steam_id])
    r = w.client.post("/results", headers=hh, json={
        "sides": {host.steam_id: 1, guest.steam_id: 2},
        "games": 2, "score_low": 2, "played_at": "2026-08-01T20:00:00",
        "lobby_id": "70707", "draft_id": f"same-{next(_ids)}"})
    assert r.status_code == 200, r.text[:200]

    def only_ours(headers):
        rows = w.client.get("/series/mine", headers=headers).json()["series"]
        return [x for x in rows if x["lobby_id"] == "70707"]

    a, b = only_ours(hh), only_ours(gh)
    assert len(a) == 1 and len(b) == 1, (a, b)
    # The label, the reasons and the roster: identical, because there is one of
    # each rather than one per client.
    assert a[0]["state"] == b[0]["state"], (a[0], b[0])
    assert a[0]["reasons"] == b[0]["reasons"]
    assert a[0]["roster"] == b[0]["roster"] == sorted(
        [host.steam_id, guest.steam_id])


def test_a_series_the_ladder_never_ran_is_not_listed():
    """Which is what makes 'casual game' the honest label for the rest."""
    w = World()
    player, ph = w.person("Player")
    rows = w.client.get("/series/mine", headers=ph).json()["series"]
    assert rows == [], rows


def test_an_unrated_series_says_unrated_rather_than_vanishing():
    """It used to read as a casual game on one side, which is the opposite of
    what it is: the ladder ran it and refused to rate it."""
    w = World()
    host, hh = w.person("Host")
    guest, _ = w.person("Guest")
    app_mod.store.sanction_lobby(70808, "s-2", created_by=host.id)
    w.client.post("/results", headers=hh, json={
        "sides": {host.steam_id: 1, guest.steam_id: 2},
        "games": 2, "score_low": 2, "played_at": "2026-08-01T21:00:00",
        "lobby_id": "70808", "draft_id": f"same-{next(_ids)}"})

    row = next(x for x in w.client.get("/series/mine", headers=hh).json()["series"]
               if x["lobby_id"] == "70808")
    assert row["state"] == "unrated", row
    assert row["reasons"], "unrated with no reason is indistinguishable from a bug"


def test_nobody_learns_about_a_series_they_were_not_in():
    w = World()
    host, hh = w.person("Host")
    guest, _ = w.person("Guest")
    _, other = w.person("Somebody else")
    app_mod.store.sanction_lobby(70909, "s-3", created_by=host.id,
                                 roster=[host.steam_id, guest.steam_id])
    w.client.post("/results", headers=hh, json={
        "sides": {host.steam_id: 1, guest.steam_id: 2},
        "games": 2, "score_low": 2, "played_at": "2026-08-01T22:00:00",
        "lobby_id": "70909", "draft_id": f"same-{next(_ids)}"})

    rows = w.client.get("/series/mine", headers=other).json()["series"]
    assert all(x["lobby_id"] != "70909" for x in rows), rows


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
