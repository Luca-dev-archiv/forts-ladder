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
    assert "requires Owner" in r.text
    assert target.role is Role.PLAYER, "the refusal must not have applied"


def test_you_cannot_change_your_own_row():
    """The one mistake on this page that cannot be undone from this page."""
    w = World()
    owner, headers = w.person("Owner", Role.OWNER)
    r = w.client.post("/admin/save", headers=headers,
                      data={"account": owner.id, "role": "guest"})
    assert "cannot change your own account" in r.text
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
            ({"match": m.id, "winner": "Nobody"}, "does not play in"),
            ({"match": m.id, "winner": m.b.name, "score": "banana"},
             "is not a score"),
            # 2:1 does not decide a Bo5.
            ({"match": m.id, "winner": m.b.name, "score": "2-1"},
             "does not decide a Bo5")):
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
    assert "no entrant 99" in r.text
    assert "no such tournament" not in r.text

    r = w.client.post(f"/manage/tournaments/{tid}/report", headers=h,
                      follow_redirects=False,
                      data={"match": "R9M9", "winner": "0", "score": "2:0"})
    assert "no such match" in r.text
    assert "no such tournament" not in r.text


# ------------------------------------- Only authored text may reach a page
def test_only_a_rule_refusal_keeps_its_words():
    """`except ValueError` cannot tell the two apart, because both *are*
    ValueErrors. `json.JSONDecodeError` is one, and so is math domain error."""
    import json as _json

    from ladder.errors import RuleError
    from server.auth import AuthError

    keep = [RuleError("a tournament needs at least two entrants"),
            AuthError("you do not have that permission")]
    for e in keep:
        assert app_mod.reason(e) == str(e), e

    # Everything a library raises, including the ValueError subclasses.
    try:
        _json.loads("not json")
    except ValueError as e:
        leaked = app_mod.reason(e)
    assert "Expecting value" not in leaked, leaked
    assert "line 1" not in leaked, leaked

    import math
    try:
        math.log2(0)
    except ValueError as e:
        assert "math domain" not in app_mod.reason(e)

    for e in (ValueError("could not convert string to float: 'x'"),
              KeyError("some internal key"),
              RuntimeError("dict changed size during iteration")):
        out = app_mod.reason(e)
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
