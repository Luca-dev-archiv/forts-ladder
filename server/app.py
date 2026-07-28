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
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ladder.modes import BY_KEY
from ladder.tournament import Participant, Tournament

from .auth import (
    AuthError, AuthService, Grant, Role, discord_authorize_url,
    exchange_discord_code, steam_openid_url, verify_steam_openid,
)
from .draft import DraftService
from .live import LiveService
from .queue import QueueService
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
live = LiveService()
drafts = DraftService()
queue = QueueService(auth, drafts)

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
def discord_start(return_to: str = "/"):
    p = guard(auth.begin_login, "discord", return_to)
    return {"url": discord_authorize_url(p.state,
                                         f"{BASE_URL}/auth/discord/callback"),
            "state": p.state}


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
def steam_start(return_to: str = "/"):
    guard(auth.begin_login, "steam", return_to)
    return {"url": steam_openid_url(f"{BASE_URL}/auth/steam/callback", BASE_URL)}


@app.get("/auth/steam/callback")
def steam_callback(request: Request, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Attach a verified Steam ID to the account that is already signed in.

    Steam proves *which Steam account*, Discord proves *which person on the
    ranking*. Requiring the session here is what binds the two together —
    without it, a verified Steam ID would arrive with nobody to attach it to.
    """
    acc = require(session_token(ladder_session, authorization))
    steam_id = guard(verify_steam_openid, dict(request.query_params))
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
            "role": acc.role.label, "verified": acc.verified,
            "tracking_consent": acc.tracking_consent,
            "consent_since": acc.consent_since}


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
            for p in body.participants], mode=mode)
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
    return {"id": s.id, "join_code": s.join_code,
            "state": s.public_state(acc)}


@app.post("/drafts/join/{join_code}")
def draft_join(join_code: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.join, acc, join_code)
    return {"id": s.id, "state": s.public_state(acc)}


@app.get("/drafts/{draft_id}")
def draft_state(draft_id: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    # Ticked on read: whoever polls first advances an expired step, so one
    # side going quiet cannot stall the other.
    s.tick()
    return guard(s.public_state, acc)


class DraftMove(BaseModel):
    value: str


@app.post("/drafts/{draft_id}/apply")
def draft_apply(draft_id: str, body: DraftMove,
                ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    return guard(s.apply, acc, body.value)


# --------------------------------------------------------------------- Queue
class QueueJoin(BaseModel):
    rating: float = 1000.0


@app.post("/queue")
def queue_join(body: QueueJoin, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    return guard(queue.join, acc, body.rating)


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


@app.put("/admin/pools")
def set_pools(body: PoolConfig, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """Operator sets the pools. Never the client — a client-supplied map pool
    would let one side choose the list before the veto starts."""
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "create_tournament")
    queue.configure(body.map_pool, body.commander_pool)
    return {"maps": len(body.map_pool), "commanders": len(body.commander_pool)}


@app.get("/health")
def health():
    return {"ok": True, "accounts": len(auth.accounts),
            "live_matches": len(live.matches)}
