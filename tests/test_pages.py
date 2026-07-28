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


def cup(w: World, headers: dict, entrants: str = ENTRANTS) -> str:
    r = w.client.post("/manage/tournaments", headers=headers,
                      follow_redirects=False,
                      data={"name": "Summer Cup", "mode": "tournament_1v1",
                            "entrants": entrants})
    assert r.status_code == 303, r.text[:300]
    return r.headers["location"].rsplit("/", 1)[1]


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
