"""HTTP interface — a thin layer over `auth` and `live`.

The rules live in the domain modules, not here. This file only translates
between HTTP and calls, which is what makes the rules testable without
starting a server (see tests/test_server.py — 26 tests, no network).

Logging in needs a **Discord application** of the operator's own
(discord.com/developers). `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` come
from the environment — a secret in the repository is public after the first
push — and the redirect URI registered there has to match
`{LADDER_BASE_URL}/auth/discord/callback` exactly.

Steam needs no registration and no API key: the login is OpenID 2.0, and the
response is verified by asking Steam to confirm its own signature. Both
verifications refuse rather than assume; an unverified login is worse than
none, because it looks like proof.

State lives in SQLite (`LADDER_DB`, default `data/ladder.sqlite`) and is
loaded at startup. Running matches stay deliberately volatile: after a
server restart the games carry on, and the clients re-announce themselves
within seconds.

    uvicorn server.app:app --reload
"""

from __future__ import annotations

import os
import secrets

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ladder.modes import BY_KEY
from ladder.tournament import Participant, Tournament

from . import page
from .brackets import viewer_data
from .auth import (
    AuthError, AuthService, Grant, Role, discord_authorize_url,
    exchange_discord_code, steam_openid_url, verify_steam_openid,
)
from .draft import DraftService
from .live import LiveService
from .queue import QueueService
from .ranking import Ranking
from .results import ResultService
from .store import Store

#: The interactive docs are useful while developing and an unnecessary
#: invitation once the API is on the internet: every route is guarded, but
#: publishing the full list — including the admin ones — hands anyone the map
#: for free. Off unless asked for: LADDER_DOCS=1.
_DOCS = os.environ.get("LADDER_DOCS") == "1"

app = FastAPI(title="Forts Ladder", version="0.1.0",
              docs_url="/docs" if _DOCS else None,
              redoc_url="/redoc" if _DOCS else None,
              openapi_url="/openapi.json" if _DOCS else None)

# Load persisted state at startup, or a restart logs everyone out and loses
# every running tournament.
store = Store(os.environ.get("LADDER_DB", "data/ladder.sqlite"))
auth = store.restore_auth(AuthService())
# Promote the configured operator before anything else can need the permission.
if (_owner := auth.apply_owner_bootstrap()) is not None:
    store.save_account(_owner)
live = LiveService()
drafts = DraftService()
# Restored at startup: a draft that only survived while the process did would
# not survive the thing persistence is for — a redeploy mid-tournament.
drafts.restore(store.load_drafts())
queue = QueueService(auth, drafts)
ranking = Ranking()
# Reported series, and the standings they produce. Before this existed the
# shared ranking was the imported spreadsheet and nothing else — winning a
# match on this ladder changed no number anyone else could see.
results = ResultService(auth, store, ranking)

# The bracket viewer (MIT, see server/static/brackets-viewer.LICENSE) is served
# from here rather than from a CDN: a ladder reachable through a tunnel should
# not stop drawing brackets because jsdelivr is unreachable, and the whole point
# of vendoring is that the version cannot change under us.
_STATIC = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

BASE_URL = os.environ.get("LADDER_BASE_URL", "http://localhost:8000")
SESSION_COOKIE = "ladder_session"


def current(token: str | None):
    return auth.account_for(token)


def session_token(cookie: str | None, authorization: str | None) -> str | None:
    """Accept the session from a cookie or a bearer header.

    The browser gets a cookie; the desktop client cannot, because the login
    happens in the browser and the cookie stays there. It holds a paired token
    instead and sends it as a header.
    """
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return cookie


def require(token: str | None):
    acc = current(token)
    if acc is None:
        raise HTTPException(401, "not logged in")
    return acc


def guard(fn, *a, **kw):
    """Pass domain refusals through as 403, not as a server error."""
    try:
        return fn(*a, **kw)
    except AuthError as e:
        raise HTTPException(403, str(e)) from e


# -------------------------------------------------------------------- Login
@app.get("/auth/discord/start")
def discord_start(return_to: str = "/", json: int = 0):
    """Begin a Discord login.

    Redirects, because the only thing that opens this is a browser and a person
    looking at a JSON blob has to copy a URL out of it by hand. `?json=1` keeps
    the machine-readable form for anything scripted.
    """
    p = guard(auth.begin_login, "discord", return_to)
    url = discord_authorize_url(p.state, f"{BASE_URL}/auth/discord/callback")
    if json:
        return {"url": url, "state": p.state}
    return RedirectResponse(url, status_code=303)


@app.get("/auth/discord/callback")
def discord_callback(code: str, state: str, response: Response):
    # The state is consumed first and exactly once. It is what ties this
    # callback to a login *we* started, so a replayed or forged callback dies
    # here before any credential is spent on it.
    pending = guard(auth.consume_state, state)
    profile = guard(exchange_discord_code, code,
                    f"{BASE_URL}/auth/discord/callback")

    acc = auth.login_discord(profile["id"], profile["name"])
    session = auth.start_session(acc)
    store.save_account(acc)
    store.save_session(session)

    response = RedirectResponse(pending.return_to or "/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, session.token,
        httponly=True,                       # not readable from JavaScript
        secure=BASE_URL.startswith("https"),
        samesite="lax",                      # survives the return redirect
        max_age=int(session.expires_at - session.created_at),
    )
    return response


@app.get("/auth/steam/start")
def steam_start(return_to: str = "/", json: int = 0, ticket: str | None = None):
    """Send the browser to Steam.

    `ticket` is how the desktop client links Steam: it holds a bearer token that
    no browser has, so it asks for a single-use ticket and passes it here. The
    ticket rides inside `openid.return_to`, which Steam signs.
    """
    guard(auth.begin_login, "steam", return_to)
    callback = f"{BASE_URL}/auth/steam/callback"
    if ticket:
        callback += f"?ticket={ticket}"
    url = steam_openid_url(callback, BASE_URL)
    if json:
        return {"url": url}
    return RedirectResponse(url, status_code=303)


@app.post("/auth/steam/ticket")
def steam_ticket(ladder_session: str | None = Cookie(None),
                 authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    ticket = auth.begin_steam_link(acc)
    return {"ticket": ticket,
            "url": f"{BASE_URL}/auth/steam/start?ticket={ticket}"}


@app.get("/auth/steam/callback")
def steam_callback(request: Request, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Attach a verified Steam ID to the account that is already signed in.

    Steam proves *which Steam account*, Discord proves *which person on the
    ranking*. Requiring the session here is what binds the two together —
    without it, a verified Steam ID would arrive with nobody to attach it to.
    """
    params = dict(request.query_params)
    # Verify first, always. Whether the account comes from a cookie or a ticket
    # is irrelevant if Steam did not actually sign this response.
    steam_id = guard(verify_steam_openid, params)
    ticket = params.get("ticket")
    acc = (guard(auth.claim_steam_ticket, ticket) if ticket
           else require(session_token(ladder_session, authorization)))
    guard(auth.attach_steam, acc, steam_id)
    store.save_account(acc)
    return RedirectResponse("/", status_code=303)


class NativeLogin(BaseModel):
    code: str
    #: Must be one of the URIs registered on the Discord application. Discord
    #: checks it when issuing the code and again on exchange, and the two have
    #: to agree, so the client sends the one it used.
    redirect_uri: str = "http://localhost"


@app.get("/auth/discord/config")
def discord_config():
    """What the desktop client needs to talk to the local Discord app.

    Only the client id, which is public by design — it appears in every OAuth
    URL. The secret stays here: an .exe that anyone can download is not a place
    to keep one.
    """
    client_id = os.environ.get("DISCORD_CLIENT_ID")
    return {"client_id": client_id,
            "native_login": bool(client_id),
            "redirect_uri": os.environ.get("DISCORD_NATIVE_REDIRECT",
                                           "http://localhost")}


@app.post("/auth/discord/native")
def discord_native(body: NativeLogin):
    """Log in with a code obtained from the local Discord client.

    Same verification as the browser callback — the code is exchanged with the
    secret and the profile is read back. What is missing is the `state`, and
    that is sound here: state exists to tie a *browser redirect* to a login we
    started, and there is no redirect in this flow. The code came from Discord
    over a local pipe to this machine, and it is still worthless without the
    secret held on this server.
    """
    profile = guard(exchange_discord_code, body.code, body.redirect_uri)
    acc = auth.login_discord(profile["id"], profile["name"])
    session = auth.start_session(acc)
    store.save_account(acc)
    store.save_session(session)
    return {"token": session.token,
            "expires_at": session.expires_at,
            "discord": acc.discord_name,
            "ufer_name": acc.ufer_name,
            "steam_id": acc.steam_id,
            "tracking_consent": acc.tracking_consent}


@app.post("/auth/pair")
def auth_pair(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """In the browser, after logging in: produce a code for the client.

    Shown on screen and typed into the client once. The alternative would be
    embedding a browser in the app, which means asking people to type their
    Discord password into our window — worse in every way.
    """
    acc = require(session_token(ladder_session, authorization))
    code = auth.begin_pairing(acc)
    return {"code": code, "expires_in_s": 300,
            "hint": "Enter this in the client under Live → Connect."}


class PairClaim(BaseModel):
    code: str


@app.post("/auth/pair/claim")
def auth_pair_claim(body: PairClaim):
    """In the client: trade the code for a session token."""
    session = guard(auth.claim_pairing, body.code)
    store.save_session(session)
    acc = auth.accounts.get(session.account_id)
    return {"token": session.token,
            "expires_at": session.expires_at,
            "discord": acc.discord_name if acc else None,
            "ufer_name": acc.ufer_name if acc else None,
            "steam_id": acc.steam_id if acc else None,
            "tracking_consent": acc.tracking_consent if acc else False}


@app.post("/auth/logout")
def logout(response: Response, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    if session_token(ladder_session, authorization):
        auth.logout(session_token(ladder_session, authorization))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/me")
def me(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = current(session_token(ladder_session, authorization))
    if acc is None:
        # Not logged in is a valid state, not an error: the recorder runs
        # without an account.
        return {"logged_in": False}
    return {"logged_in": True, "discord": acc.discord_name,
            "ufer_name": acc.ufer_name, "steam_id": acc.steam_id,
            # What is waiting on a human, so the client can say so rather than
            # showing "not set" as if nothing had been tried.
            "ufer_claim": acc.ufer_claim,
            "steam_name": acc.steam_name,
            "role": acc.role.label, "verified": acc.verified,
            # Own grants only: the client offers the tournament pages to a host
            # who is not an admin, and the rank alone does not say so.
            "grants": sorted(g.value for g in acc.grants),
            "tracking_consent": acc.tracking_consent,
            "consent_since": acc.consent_since}


class NameBody(BaseModel):
    name: str


@app.post("/me/ufer_name")
def claim_name(body: NameBody, ladder_session: str | None = Cookie(None),
               authorization: str | None = Header(None)):
    """Claim a ladder name.

    A name matching the Discord login is proof in itself, because the
    spreadsheet lists Discord names. Anything else is held for an admin instead
    of refused — being listed under a different name is common, and refusing
    outright left those people with no route at all.
    """
    acc = require(session_token(ladder_session, authorization))
    applied = guard(auth.claim_ufer_name, acc, body.name)
    store.save_account(acc)
    return {"applied": applied, "ufer_name": acc.ufer_name,
            "pending": acc.ufer_claim}


@app.put("/me/steam_name")
def set_steam_name(body: NameBody, ladder_session: str | None = Cookie(None),
                   authorization: str | None = Header(None)):
    """The Steam display name this account plays under.

    Sent by the account's own client, which reads it out of the game log. It is
    shown instead of a 17-digit id and decides nothing — the id remains the
    identity, because a display name can be changed to anyone else's.
    """
    acc = require(session_token(ladder_session, authorization))
    auth.set_steam_name(acc, body.name)
    store.save_account(acc)
    return {"steam_name": acc.steam_name}


# ------------------------------------------------------------------- Consent
@app.post("/me/consent")
def consent_grant(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Agree to have your results tracked.

    Separate from signing in on purpose: this project was cleared on the
    condition that it only tracks people who want it to, so the agreement is
    its own deliberate act rather than a side effect of logging in.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(auth.set_tracking_consent, acc, True)
    store.save_account(acc)
    return {"tracking_consent": True, "since": acc.consent_since}


@app.delete("/me/consent")
def consent_withdraw(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Withdraw again. Past results stop counting on the next recompute."""
    acc = require(session_token(ladder_session, authorization))
    guard(auth.set_tracking_consent, acc, False)
    store.save_account(acc)
    return {"tracking_consent": False}


class EligibilityQuery(BaseModel):
    #: Steam IDs and lobby ids the caller already saw in its own game log.
    steam_ids: list[str] = []
    lobby_ids: list[int] = []


@app.post("/eligibility/check")
def eligibility_check(body: EligibilityQuery):
    """Answer whether specific ids count. Never enumerate.

    There is deliberately no "give me everyone who opted in" endpoint: that
    list is the member roster, and a Steam lobby id is a join key — the same
    reason `/live` withholds it. Answering only about ids the caller already
    holds does the job and discloses nothing new.

    Capped per request so the endpoint cannot be walked to rebuild the roster
    a bounded number of guesses at a time.
    """
    if len(body.steam_ids) > 64 or len(body.lobby_ids) > 64:
        raise HTTPException(413, "ask about at most 64 ids per request")
    trackable = auth.trackable_ids()
    known = set(store.sanctioned_lobbies())
    return {
        "opted_in": sorted(set(body.steam_ids) & trackable),
        "sanctioned_lobbies": sorted(set(body.lobby_ids) & known),
    }


class SanctionBody(BaseModel):
    lobby_id: int
    series_id: str | None = None


@app.post("/lobby/sanction")
def lobby_sanction(body: SanctionBody,
                   ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Record that a lobby was set up for a ladder match.

    The host's client can arm itself locally, but the guest's cannot — it
    never saw the lobby being created. Registering it here is what lets both
    sides agree that the match counts.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "publish_live_match")
    guard(auth.require_trackable, acc)
    store.sanction_lobby(body.lobby_id, body.series_id, created_by=acc.id)
    return {"lobby_id": body.lobby_id, "sanctioned": True}


@app.get("/admin/roster")
def admin_roster(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """The full roster — admins only.

    The operator holds this data anyway; the point is that nobody else can
    fetch it. Clients use POST /eligibility/check instead.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "link_other_account")
    return {"authoritative": True,
            "steam_ids": sorted(auth.trackable_ids()),
            "sanctioned_lobbies": store.sanctioned_lobbies()}


# ----------------------------------------------------------- Running matches
class PublishBody(BaseModel):
    mode_key: str
    mode_label: str
    players: list[str]
    slots_used: int
    slots_total: int = 9
    lobby_id: int | None = None
    tournament: str | None = None


@app.get("/live")
def live_list():
    """Public — without the lobby ID, which only admitted people get."""
    return {"matches": live.listing()}


@app.post("/live")
def live_publish(body: PublishBody, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    m = guard(live.publish, acc, body.mode_key, body.mode_label, body.players,
              body.slots_used, body.slots_total, body.lobby_id, body.tournament)
    return {"match_id": m.id}


@app.post("/live/{match_id}/heartbeat")
def live_heartbeat(match_id: str, slots_used: int | None = None):
    live.heartbeat(match_id, slots_used)
    return {"ok": True}


@app.delete("/live/{match_id}")
def live_finish(match_id: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    require(session_token(ladder_session, authorization))
    live.finish(match_id)
    return {"ok": True}


@app.post("/live/{match_id}/accepting")
def live_accepting(match_id: str, value: bool,
                   ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(live.set_accepting, acc, match_id, value)
    return {"accepting_requests": value}


# ------------------------------------------------------- Spectator requests
@app.post("/live/{match_id}/observe")
def observe(match_id: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    r = guard(live.request_observer, acc, match_id)
    return {"request_id": r.id, "state": r.state.value, "reason": r.reason}


@app.get("/observe/requests")
def my_requests(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    return {"pending": [
        {"id": r.id, "match_id": r.match_id, "who": r.display_name}
        for r in live.pending_for_host(acc)]}


@app.post("/observe/{request_id}/answer")
def answer(request_id: str, approve: bool, reason: str = "",
           ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    r = guard(live.answer, acc, request_id, approve, reason)
    return {"state": r.state.value, "reason": r.reason}


@app.get("/observe/{request_id}/join")
def join(request_id: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    return guard(live.join_info, acc, request_id)


# ------------------------------------------------------------- Tournaments
class TournamentBody(BaseModel):
    #: Same three options the page offers. Kept in step deliberately: two ways
    #: in that build different brackets from the same list would be worse than
    #: one way in.
    seeding: str = "rating"
    best_of: int | None = None
    name: str
    mode_key: str = "tournament_1v1"
    #: [{"name": "...", "rating": 1800, "members": ["..."]}, ...]
    participants: list[dict]


class ResultBody(BaseModel):
    winner: str
    score: tuple[int, int] | None = None
    match_keys: list[str] = []


@app.get("/tournaments")
def tournaments_list():
    return {"tournaments": store.list_tournaments()}


@app.post("/tournaments")
def tournament_create(body: TournamentBody,
                      ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    # This is where the grant matters: a tournament host may do this
    # without being an admin.
    guard(acc.require, "create_tournament")
    try:
        mode = BY_KEY.get(body.mode_key) or BY_KEY["tournament_1v1"]
        t = Tournament(body.name, [
            Participant(p["name"], float(p.get("rating", 1000)),
                        p.get("members", []))
            for p in body.participants], mode=mode,
            seeding=body.seeding if body.seeding in
            ("rating", "listed", "random") else "rating",
            best_of=body.best_of if body.best_of in (1, 3, 5, 7) else None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    tid = secrets.token_hex(6)
    store.create_tournament(tid, t, created_by=acc.id)
    return {"id": tid, "bracket": t.bracket()}


@app.get("/tournaments/{tid}")
def tournament_show(tid: str):
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {"name": t.name, "mode": t.mode.label, "bracket": t.bracket(),
            "best_of": t.series_length(), "seeding": t.seeding,
            "playable": [m.id for m in t.playable()],
            "champion": t.champion.name if t.champion else None}


@app.post("/tournaments/{tid}/matches/{match_id}")
def tournament_report(tid: str, match_id: str, body: ResultBody,
                      ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "run_tournament")
    try:
        t = store.load_tournament(tid)
        t.report(match_id, body.winner, body.score, body.match_keys)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        # Domain refusal: impossible score, or a winner who is not playing.
        raise HTTPException(400, str(e)) from e
    store.record_result(tid, match_id, body.winner, body.score, body.match_keys)
    if t.finished:
        store.mark_finished(tid)
    return {"bracket": t.bracket(),
            "champion": t.champion.name if t.champion else None}


# ------------------------------------------------------------------ Admin
class GrantBody(BaseModel):
    target_id: str
    grant: str


@app.post("/admin/grant")
def add_grant(body: GrantBody, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Grant a permission — tournament host, referee, caster.

    Deliberately separate from /admin/role: a grant is a responsibility, not
    a transfer of power.
    """
    acc = require(session_token(ladder_session, authorization))
    target = auth.accounts.get(body.target_id)
    if target is None:
        raise HTTPException(404, "unknown account")
    try:
        g = Grant(body.grant)
    except ValueError:
        raise HTTPException(400, f"unknown grant {body.grant!r}. "
                                 f"Available: {[x.value for x in Grant]}")
    guard(auth.grant_permission, acc, target, g)
    store.save_account(target, granted_by=acc.id)
    return {"account": target.id,
            "grants": sorted(x.value for x in target.grants)}


@app.delete("/admin/grant")
def remove_grant(body: GrantBody, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    target = auth.accounts.get(body.target_id)
    if target is None:
        raise HTTPException(404, "unknown account")
    guard(auth.revoke_permission, acc, target, Grant(body.grant))
    store.save_account(target, granted_by=acc.id)
    return {"account": target.id,
            "grants": sorted(x.value for x in target.grants)}


@app.post("/admin/role")
def set_role(target_id: str, role: str,
             ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    target = auth.accounts.get(target_id)
    if target is None:
        raise HTTPException(404, "unknown account")
    try:
        wanted = Role[role.upper()]
    except KeyError:
        raise HTTPException(400, f"unknown role {role!r}")
    guard(auth.grant_role, acc, target, wanted)
    # Persisted, or the promotion lasts until the next restart — which is how
    # long a role that was granted over the API used to survive.
    store.save_account(target, granted_by=acc.id)
    return {"account": target.id, "role": target.role.label}


# ------------------------------------------------------------------- Drafting
class DraftCreate(BaseModel):
    map_pool: list[str]
    commander_pool: list[str]
    best_of: int = 3
    commander_bans_per_side: int = 1
    step_seconds: float | None = 30.0


@app.post("/drafts")
def draft_create(body: DraftCreate, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.create, acc, body.map_pool, body.commander_pool,
              body.best_of, body.commander_bans_per_side, body.step_seconds)
    store.save_draft(s)
    return {"id": s.id, "join_code": s.join_code,
            "state": s.public_state(acc)}


@app.post("/drafts/join/{join_code}")
def draft_join(join_code: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.join, acc, join_code)
    store.save_draft(s)
    return {"id": s.id, "state": s.public_state(acc)}


@app.get("/drafts/{draft_id}")
def draft_state(draft_id: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    # Ticked on read: whoever polls first advances an expired step, so one
    # side going quiet cannot stall the other.
    if s.tick():
        # The clock changed the draft, so what is now in memory has to be
        # written down as well.
        store.save_draft(s)
    return guard(s.public_state, acc)


@app.delete("/drafts/{draft_id}")
def draft_cancel(draft_id: str, ladder_session: str | None = Cookie(None),
                 authorization: str | None = Header(None)):
    """Leave a draft. Either side may; the other is told who left."""
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.cancel, acc)
    store.save_draft(s)
    # Whoever left is out of the queue too, or the client would offer to
    # rejoin a match it just abandoned.
    queue.leave(acc)
    return state


class LobbyBody(BaseModel):
    lobby_id: str


@app.post("/drafts/{draft_id}/lobby")
def draft_lobby(draft_id: str, body: LobbyBody,
                ladder_session: str | None = Cookie(None),
                authorization: str | None = Header(None)):
    """The host names the Steam lobby, and the ladder sanctions it.

    This is the join in both senses: the other side gets a link into the game,
    and the lobby id becomes the one recorded games are matched against.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    try:
        lobby = int(body.lobby_id)
    except ValueError as e:
        raise HTTPException(400, "lobby id must be a number") from e
    state = guard(s.set_lobby, acc, lobby)
    store.save_draft(s)
    # Sanctioned here rather than by the client: the server knows this lobby
    # came out of a draft it ran, which is exactly what sanctioning means.
    store.sanction_lobby(lobby, s.series_id or s.id, created_by=acc.id)
    return state


class DraftMove(BaseModel):
    value: str


@app.post("/drafts/{draft_id}/apply")
def draft_apply(draft_id: str, body: DraftMove,
                ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.apply, acc, body.value)
    # After every move, not at the end: a draft that only persisted once
    # finished would lose exactly the case this is for.
    store.save_draft(s)
    return state


# --------------------------------------------------------------------- Queue
class QueueJoin(BaseModel):
    rating: float = 1000.0
    mode: str = "ranked_1v1"


@app.get("/queue/modes")
def queue_modes():
    """What can be queued, and how many are waiting in each.

    Public: the number waiting is what tells someone whether it is worth
    starting a search, and it says nothing about who they are.
    """
    return {"modes": queue.modes()}


@app.post("/queue")
def queue_join(body: QueueJoin, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    return guard(queue.join, acc, body.rating, body.mode)


@app.delete("/queue")
def queue_leave(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    return guard(queue.leave, require(session_token(ladder_session, authorization)))


@app.get("/queue")
def queue_status(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    return guard(queue.status, require(session_token(ladder_session, authorization)))


@app.post("/queue/accept")
def queue_accept(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    return guard(queue.accept, require(session_token(ladder_session, authorization)))


@app.post("/queue/decline")
def queue_decline(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    return guard(queue.decline, require(session_token(ladder_session, authorization)))


class PoolConfig(BaseModel):
    map_pool: list[str]
    commander_pool: list[str]


@app.get("/queue/pools")
def queue_pools():
    """Whether the queue is usable at all, and how large the pools are.

    Public because "the ladder is not set up yet" is not a secret, and a client
    that cannot tell the difference between that and its own fault shows the
    wrong error.
    """
    return {"configured": bool(queue.map_pool and queue.commander_pool),
            "maps": len(queue.map_pool),
            "commanders": len(queue.commander_pool)}


@app.put("/admin/pools")
def set_pools(body: PoolConfig, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Operator sets the pools. Never the client — a client-supplied map pool
    would let one side choose the list before the veto starts."""
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "create_tournament")
    queue.configure(body.map_pool, body.commander_pool)
    return {"maps": len(body.map_pool), "commanders": len(body.commander_pool)}


# ----------------------------------------------------------------- The page
#
# One page, for one purpose: the login has to land somewhere, and connecting a
# client needs a code that only a signed-in session can be given. Everything
# else a person looks at is in the client.
@app.get("/", response_class=HTMLResponse)
def index(ladder_session: str | None = Cookie(None),
          authorization: str | None = Header(None)):
    acc = current(session_token(ladder_session, authorization))
    if acc is None:
        return page.signed_out("/auth/discord/start")
    return page.signed_in(
        discord=acc.discord_name, ufer_name=acc.ufer_name,
        steam_id=acc.steam_id, consent=acc.tracking_consent,
        role=acc.role.label, steam_url="/auth/steam/start", code=None,
        is_admin=acc.may("link_other_account"),
        can_host=acc.may("create_tournament") or acc.may("run_tournament"),
        pending_name=acc.ufer_claim)


@app.post("/auth/pair/page", response_class=HTMLResponse)
def pair_from_page(ladder_session: str | None = Cookie(None),
                   authorization: str | None = Header(None)):
    """Same as POST /auth/pair, but answers with the page showing the code.

    A browser cannot issue the JSON call, and telling someone to run curl after
    they have just logged in is not an instruction, it is an obstacle.
    """
    acc = require(session_token(ladder_session, authorization))
    code = auth.begin_pairing(acc)
    return page.signed_in(
        discord=acc.discord_name, ufer_name=acc.ufer_name,
        steam_id=acc.steam_id, consent=acc.tracking_consent,
        role=acc.role.label, steam_url="/auth/steam/start", code=code,
        is_admin=acc.may("link_other_account"),
        can_host=acc.may("create_tournament") or acc.may("run_tournament"),
        pending_name=acc.ufer_claim)


@app.post("/me/consent/on")
def consent_on_page(ladder_session: str | None = Cookie(None),
                    authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(auth.set_tracking_consent, acc, True)
    store.save_account(acc)
    return RedirectResponse("/", status_code=303)


@app.post("/me/consent/off")
def consent_off_page(ladder_session: str | None = Cookie(None),
                     authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(auth.set_tracking_consent, acc, False)
    store.save_account(acc)
    return RedirectResponse("/", status_code=303)


@app.post("/auth/logout/page")
def logout_page(ladder_session: str | None = Cookie(None),
                authorization: str | None = Header(None)):
    token = session_token(ladder_session, authorization)
    if token:
        auth.logout(token)
    r = RedirectResponse("/", status_code=303)
    r.delete_cookie(SESSION_COOKIE)
    return r


# ------------------------------------------------------ Admin & host pages
#
# Both are forms — a list of accounts with roles, a list of entrants, a result
# — and a form is what a browser is good at. They live here rather than in the
# client because a WPF window would be a slower way to build the same thing,
# and because the people who need them are not always at the machine that has
# Forts installed. The client keeps what needs the game: queue, draft, live.
#
# Under /manage and not /tournaments/page, because /tournaments/{tid} is
# already registered and would swallow "page" as an id.

def _login_first(return_to: str) -> RedirectResponse:
    """A page nobody is signed in for sends them to sign in, not to a 401."""
    return RedirectResponse(f"/auth/discord/start?return_to={return_to}",
                            status_code=303)


def _roster_rows() -> list[dict]:
    """Accounts as the admin page wants them: highest rank first."""
    return [{"id": a.id, "discord": a.discord_name, "role": a.role.label,
             "ufer_name": a.ufer_name, "steam_id": a.steam_id,
             "steam_name": a.steam_name, "claim": a.ufer_claim,
             "tracked": a.trackable,
             "grants": sorted(g.value for g in a.grants)}
            for a in sorted(auth.accounts.values(),
                            key=lambda x: (-int(x.role),
                                           (x.discord_name or "").lower()))]


def _admin_page(acc, error: str = "") -> HTMLResponse:
    claims = [{"id": a.id, "discord": a.discord_name, "claim": a.ufer_claim,
               "steam_id": a.steam_id, "steam_name": a.steam_name}
              for a in auth.pending_claims()]
    return HTMLResponse(page.admin(
        accounts=_roster_rows(), grants=[g.value for g in Grant],
        my_id=acc.id, pools=queue_pools(), ranking_count=len(ranking.players),
        may_set_roles=acc.may("grant_role"), error=error, claims=claims))


@app.post("/admin/name", response_class=HTMLResponse)
async def admin_name(request: Request,
                     ladder_session: str | None = Cookie(None),
                     authorization: str | None = Header(None)):
    """Confirm or reject a held ladder name.

    An identity statement, so it takes an admin and a person: the name decides
    which row of the community spreadsheet an account is, and getting it wrong
    hands someone else's rating to the wrong player.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "link_other_account")
    form = await request.form()
    target = auth.accounts.get(str(form.get("account") or ""))
    if target is None:
        raise HTTPException(404, "unknown account")
    try:
        if str(form.get("decision")) == "confirm":
            auth.confirm_ufer_name(acc, target)
        else:
            auth.reject_ufer_name(acc, target)
    except AuthError as e:
        return _admin_page(acc, str(e))
    store.save_account(target, granted_by=acc.id)
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(ladder_session: str | None = Cookie(None),
               authorization: str | None = Header(None)):
    acc = current(session_token(ladder_session, authorization))
    if acc is None:
        return _login_first("/admin")
    guard(acc.require, "link_other_account")
    return _admin_page(acc)


@app.post("/admin/save", response_class=HTMLResponse)
async def admin_save(request: Request,
                     ladder_session: str | None = Cookie(None),
                     authorization: str | None = Header(None)):
    """One row of the admin page: a role and a set of grants.

    Everything is validated before anything is written, so a typo in one field
    cannot leave an account half-changed.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "grant_permission")
    form = await request.form()
    target = auth.accounts.get(str(form.get("account") or ""))
    if target is None:
        raise HTTPException(404, "unknown account")
    if target.id == acc.id:
        return _admin_page(acc, "You cannot change your own account here.")

    role_name = str(form.get("role") or "")
    try:
        wanted_role = Role[role_name.upper()] if role_name else target.role
        wanted_grants = {Grant(v) for v in form.getlist("grant")}
    except (KeyError, ValueError):
        return _admin_page(acc, "Unknown role or grant in the form.")

    add = wanted_grants - target.grants
    drop = target.grants - wanted_grants
    try:
        if wanted_role is not target.role:
            auth.grant_role(acc, target, wanted_role)
        for g in add:
            auth.grant_permission(acc, target, g)
        for g in drop:
            auth.revoke_permission(acc, target, g)
    except AuthError as e:
        return _admin_page(acc, str(e))
    store.save_account(target, granted_by=acc.id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/ranking/reload/page")
def ranking_reload_page(ladder_session: str | None = Cookie(None),
                        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "link_other_account")
    ranking.reload()
    return RedirectResponse("/admin", status_code=303)


#: Tournament modes first in the picker — they are what an event uses — with
#: the rest still available for a host who wants a different series length.
def _mode_choices() -> list[tuple[str, str]]:
    keys = sorted(BY_KEY, key=lambda k: (not k.startswith("tournament"), k))
    return [(k, f"{BY_KEY[k].label} · Bo{BY_KEY[k].best_of}") for k in keys]


def _parse_entrants(text: str) -> list[Participant]:
    """One entrant per line, `name` or `name, rating`.

    A textarea rather than a growing list of inputs: a host has the names in
    a message or a spreadsheet already and pastes them.
    """
    out: list[Participant] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        name, _, rating = line.rpartition(",")
        if not name:
            out.append(Participant(line))
            continue
        try:
            out.append(Participant(name.strip(), float(rating.strip())))
        except ValueError:
            # A comma that was part of the name, not a rating.
            out.append(Participant(line))
    return out


@app.get("/manage/tournaments", response_class=HTMLResponse)
def tournaments_page(ladder_session: str | None = Cookie(None),
                     authorization: str | None = Header(None)):
    acc = current(session_token(ladder_session, authorization))
    if acc is None:
        return _login_first("/manage/tournaments")
    # Reporting is enough to need the list: a referee corrects results in
    # brackets they did not build, and had no way to find one otherwise.
    if not acc.may("create_tournament"):
        guard(acc.require, "run_tournament")
    return HTMLResponse(page.tournaments(
        listing=store.list_tournaments(), modes=_mode_choices(),
        is_admin=acc.may("link_other_account"),
        can_create=acc.may("create_tournament")))


@app.post("/manage/tournaments", response_class=HTMLResponse)
async def tournaments_page_create(request: Request,
                                  ladder_session: str | None = Cookie(None),
                                  authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "create_tournament")
    form = await request.form()
    name = str(form.get("name") or "").strip()
    entrants = str(form.get("entrants") or "")
    mode = BY_KEY.get(str(form.get("mode") or "")) or BY_KEY["tournament_1v1"]
    seeding = str(form.get("seeding") or "rating")
    if seeding not in ("rating", "listed", "random"):
        seeding = "rating"
    raw_bo = str(form.get("best_of") or "").strip()
    best_of = int(raw_bo) if raw_bo.isdigit() and int(raw_bo) in (1, 3, 5, 7) \
        else None

    def again(msg: str) -> HTMLResponse:
        return HTMLResponse(page.tournaments(
            listing=store.list_tournaments(), modes=_mode_choices(),
            is_admin=acc.may("link_other_account"), error=msg,
            name=name, entrants=entrants))

    if not name:
        return again("Give the tournament a name.")
    try:
        t = Tournament(name, _parse_entrants(entrants), mode=mode,
                       seeding=seeding, best_of=best_of)
    except ValueError as e:
        return again(str(e))
    tid = secrets.token_hex(6)
    store.create_tournament(tid, t, created_by=acc.id)
    return RedirectResponse(f"/manage/tournaments/{tid}", status_code=303)


def _bracket_page(acc, tid: str, error: str = "") -> HTMLResponse:
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    # Renaming stops at the first reported result: from then on the pairings
    # and the stored results both rest on these names.
    started = any(m["winner"] and not m["bye"]
                  for r in t.bracket() for m in r["matches"])
    return HTMLResponse(page.bracket(
        name=t.name, mode=t.mode.label, best_of=t.series_length(),
        rounds=t.bracket(), tid=tid, data=viewer_data(t, tid),
        champion=t.champion.name if t.champion else None,
        is_admin=acc.may("link_other_account"),
        can_report=acc.may("run_tournament"),
        can_host=acc.may("create_tournament") or acc.may("run_tournament"),
        entrants=[{"seat": i, "seed": i + 1, "name": p.name}
                  for i, p in enumerate(t.participants)],
        editable=acc.may("create_tournament") and not started,
        error=error))


@app.get("/manage/tournaments/{tid}", response_class=HTMLResponse)
def bracket_page(tid: str, ladder_session: str | None = Cookie(None),
                 authorization: str | None = Header(None)):
    """Readable by anyone signed in — an entrant wants to see their own
    bracket. Only a host or referee gets the report forms."""
    acc = current(session_token(ladder_session, authorization))
    if acc is None:
        return _login_first(f"/manage/tournaments/{tid}")
    return _bracket_page(acc, tid)


@app.get("/tournaments/{tid}/viewer")
def tournament_viewer_data(tid: str):
    """The bracket in `brackets-model` shape.

    Public, like `GET /tournaments/{tid}`: it is the same information in the
    format a bracket viewer reads. Having it as its own route means the page is
    not the only thing that can draw this tournament.
    """
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return viewer_data(t, tid)


@app.post("/manage/tournaments/{tid}/rename", response_class=HTMLResponse)
async def bracket_page_rename(tid: str, request: Request,
                              ladder_session: str | None = Cookie(None),
                              authorization: str | None = Header(None)):
    """Correct an entrant's name while that is still harmless.

    A typo is the commonest thing to fix and used to mean building the bracket
    again from scratch. The engine refuses once a result exists, because the
    stored results refer to these names.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "create_tournament")
    form = await request.form()
    try:
        seat = int(str(form.get("seat")))
    except ValueError as e:
        raise HTTPException(400, "seat must be a number") from e
    name = str(form.get("name") or "")
    try:
        t = store.load_tournament(tid)
        t.rename(seat, name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        return _bracket_page(acc, tid, str(e))
    store.rename_participant(tid, seat, name.strip())
    return RedirectResponse(f"/manage/tournaments/{tid}", status_code=303)


@app.post("/manage/tournaments/{tid}/report", response_class=HTMLResponse)
async def bracket_page_report(tid: str, request: Request,
                              ladder_session: str | None = Cookie(None),
                              authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "run_tournament")
    form = await request.form()
    match_id = str(form.get("match") or "")
    winner = str(form.get("winner") or "")
    raw = str(form.get("score") or "").strip()

    score: tuple[int, int] | None = None
    if raw:
        parts = raw.replace("-", ":").split(":")
        try:
            score = (int(parts[0]), int(parts[1]))
        except (IndexError, ValueError):
            return _bracket_page(acc, tid,
                                 f"{raw!r} is not a score — write it as 3:1.")
    try:
        t = store.load_tournament(tid)
        t.report(match_id, winner, score)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        return _bracket_page(acc, tid, str(e))
    store.record_result(tid, match_id, winner, score)
    if t.finished:
        store.mark_finished(tid)
    return RedirectResponse(f"/manage/tournaments/{tid}", status_code=303)


# ---------------------------------------------------------------- Results
class ResultBody2(BaseModel):
    #: SteamID64 -> side, straight out of the game log.
    sides: dict[str, int]
    games: int
    #: Games won by the lower side number.
    score_low: int
    played_at: str
    lobby_id: str | None = None
    replays: list[str] = []


@app.post("/results")
def report_result(body: ResultBody2, ladder_session: str | None = Cookie(None),
                  authorization: str | None = Header(None)):
    """Report a finished series.

    Accepted only from a client whose own Steam ID is in it. Both players'
    clients report the same series on purpose — whichever is running gets it
    through, and the second arrival is a no-op.

    A series that may not be rated is still stored, with the reasons, and the
    answer says so. "It did not count" needs to be explainable.
    """
    acc = require(session_token(ladder_session, authorization))
    lobby: int | None = None
    if body.lobby_id:
        try:
            lobby = int(body.lobby_id)
        except ValueError as e:
            raise HTTPException(400, "lobby id must be a number") from e
    r = guard(results.report, acc, lobby_id=lobby, sides=body.sides,
              games=body.games, score_low=body.score_low,
              played_at=body.played_at, replays=body.replays)
    return {"id": r.id, "rated": r.rated, "reasons": r.reasons}


@app.get("/results/mine")
def my_results(ladder_session: str | None = Cookie(None),
               authorization: str | None = Header(None)):
    """Your own reported series — what the ladder has of yours, and what of it
    counted. Nobody else's: this is not a browsable archive."""
    acc = require(session_token(ladder_session, authorization))
    if acc.steam_id is None:
        return {"series": []}
    mine = [r for r in store.load_results() if acc.steam_id in r.sides]
    return {"series": [{"id": r.id, "played_at": r.played_at,
                        "games": r.games, "score_low": r.score_low,
                        "your_side": r.sides[acc.steam_id],
                        "rated": r.rated, "reasons": r.reasons}
                       for r in mine]}


# ---------------------------------------------------------------- Ranking
@app.get("/ranking")
def ranking_get(ladder_session: str | None = Cookie(None),
                authorization: str | None = Header(None)):
    """The shared ranking. Requires a session.

    Not public: the seed is a few hundred real display names and ratings from
    the community spreadsheet, and an open endpoint would be a scrapeable copy
    of someone else's list. Steam IDs are not included — a client recognises
    itself from its own log.
    """
    require(session_token(ladder_session, authorization))
    # Recomputed on read, not cached: a rating held in memory would outlive the
    # consent it was based on.
    results.refresh_ranking()
    return ranking.payload()


@app.post("/admin/ranking/reload")
def ranking_reload(ladder_session: str | None = Cookie(None),
                   authorization: str | None = Header(None)):
    """Re-read the seed after a new season has been uploaded."""
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "link_other_account")
    return {"players": ranking.reload(), "source": ranking.source}


@app.get("/health")
def health():
    return {"ok": True, "accounts": len(auth.accounts),
            "live_matches": len(live.matches)}
