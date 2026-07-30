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

import math
import os
import secrets
from urllib.parse import quote

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
from .presence import Presence
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
# Who is at their client right now. In memory on purpose: presence is true for
# a minute at a time, and a stored one would survive a redeploy claiming people
# are there who closed the client hours ago.
presence = Presence()
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


def safe_return_to(value: str | None) -> str:
    r"""A place on this site, or the front page.

    `return_to` decides where somebody lands *after proving who they are*, which
    is precisely the moment a redirect is worth stealing: the link is ours, the
    login is real, and only the destination is not. So anything that is not a
    plain path here is dropped rather than repaired — a redirect that is nearly
    right is the dangerous kind.

    What gets rejected, and why each one matters:

      * anything not starting with `/` — an absolute URL, with or without a
        scheme;
      * `//host` and `/\host` — both protocol-relative to a browser, which
        makes them absolute URLs wearing a path's clothes;
      * anything with a control character in it, which is how header splitting
        is attempted.
    """
    if not value or not value.startswith("/"):
        return "/"
    if value[1:2] in ("/", "\\"):
        return "/"
    if any(c in value for c in "\r\n\t\x00"):
        return "/"
    return value


def path_for(*parts: str) -> str:
    """Build a site path out of values that came from outside.

    Each part is escaped whole, so an id can never contribute a `/` or a `?` and
    turn a path into something else.
    """
    return "/" + "/".join(quote(p.strip("/"), safe="") for p in parts if p)


def reason(e: BaseException) -> str:
    """The part of a refusal that belongs in front of a person.

    Every caller of this catches a refusal the rules raised on purpose —
    `AuthError`, or a `ValueError` from `ladder/` — and those messages exist to
    be read: refusing with an explanation instead of a bare 400 is most of what
    makes this usable. They are HTML-escaped at every sink.

    It goes through one function anyway, for two reasons. A length cap, so no
    message can turn a card into a wall of text. And a single place that decides
    what reaches a page, so widening an `except` somewhere does not quietly
    start showing the inside of the program to whoever tripped over it.
    """
    if isinstance(e, (AuthError, ValueError, KeyError)):
        return str(e).strip()[:300]
    return "That did not work. Try again, or ask an admin to look."


def guard(fn, *a, **kw):
    """Pass domain refusals through as 403, not as a server error."""
    try:
        return fn(*a, **kw)
    except AuthError as e:
        raise HTTPException(403, reason(e)) from e


# -------------------------------------------------------------------- Login
@app.get("/auth/discord/start")
def discord_start(return_to: str = "/", json: int = 0):
    """Begin a Discord login.

    Redirects, because the only thing that opens this is a browser and a person
    looking at a JSON blob has to copy a URL out of it by hand. `?json=1` keeps
    the machine-readable form for anything scripted.
    """
    # Checked before it is stored, so nothing unsafe is ever kept.
    p = guard(auth.begin_login, "discord", safe_return_to(return_to))
    url = guard(discord_authorize_url, p.state,
                f"{BASE_URL}/auth/discord/callback")
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

    # Checked again on the way out: a pending login created before the check
    # above existed would otherwise still be honoured.
    response = RedirectResponse(safe_return_to(pending.return_to),
                                status_code=303)
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
    guard(auth.begin_login, "steam", safe_return_to(return_to))
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
    if (token := session_token(ladder_session, authorization)):
        # Logging out is a statement, not a timeout: leaving the queue and
        # going offline should happen now rather than in half a minute. Done
        # before the session dies, or there is no account left to act on.
        if (acc := current(token)) is not None:
            presence.gone(acc.id)
            queue.leave(acc)
        auth.logout(token)
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
def live_list(ladder_session: str | None = Cookie(None),
              authorization: str | None = Header(None)):
    """Public — without the lobby ID, which only admitted people get.

    The session is read if there is one, purely to mark the caller's own match.
    A client cannot work that out for itself: the listing carries no lobby id,
    and the guest of a series is not recorded on the match at all.
    """
    acc = current(session_token(ladder_session, authorization))
    rows = live.listing()
    for row in rows:
        row["yours"] = acc is not None and _plays_in(acc, row["id"])
    return {"matches": rows}


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
@app.post("/live/{match_id}/spectators")
def live_allow_spectators(match_id: str, value: bool,
                          ladder_session: str | None = Cookie(None),
                          authorization: str | None = Header(None)):
    """Allow or forbid spectators for this match at all.

    Different from `/accepting`, which means "not right now": a match closed here
    declines everybody, a caster included.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(live.set_spectators_allowed, acc, match_id, value)
    return {"allow_spectators": value}


@app.get("/observe/terms")
def observer_terms():
    """What a spectator accepts by being admitted.

    Public, because it has to be readable before asking. A spectator sees both
    forts, and in a rated series that is everything one side is paying to keep
    hidden — the delay is what makes casting possible without turning the stream
    into a scouting feed.
    """
    return {"terms": LiveService.OBSERVER_TERMS}


def _plays_in(acc, match_id: str) -> bool:
    """Whether this account is one of the players in that live match.

    The host is known directly. The other side is not recorded on the match at
    all — only display names are — so it is found through the series: a drafted
    seat whose series is being played in this lobby is a player in it.
    """
    m = live.matches.get(match_id)
    if m is None:
        return False
    if m.host_account_id == acc.id:
        return True
    if m.lobby_id is None:
        return False
    return any(acc.id in s.seats and not s.settled
               and s.lobby_id == m.lobby_id
               for s in drafts.sessions.values())


@app.post("/live/{match_id}/observe")
def observe(match_id: str, ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    # Watching your own match is not watching. It would also be a way around
    # every rule above this one: a spectator slot in your own series shows you
    # the opponent's fort, which is the one thing a blind pick is for.
    if _plays_in(acc, match_id):
        raise HTTPException(403, "you are playing in this match")
    r = guard(live.request_observer, acc, match_id)
    return {"request_id": r.id, "state": r.state.value, "reason": r.reason}


@app.get("/observe/requests")
def my_requests(ladder_session: str | None = Cookie(None),
        authorization: str | None = Header(None)):
    """The host's inbox: who is asking to watch a match of theirs."""
    acc = require(session_token(ladder_session, authorization))
    return {"pending": [
        {"id": r.id, "match_id": r.match_id, "who": r.display_name}
        for r in live.pending_for_host(acc)]}


@app.get("/observe/mine")
def my_own_requests(ladder_session: str | None = Cookie(None),
                    authorization: str | None = Header(None)):
    """Your own requests and what became of them.

    The route above is somebody else's inbox, so without this a spectator asked
    to watch and never found out the answer.
    """
    acc = require(session_token(ladder_session, authorization))
    return {"requests": live.requests_for(acc)}


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
        raise HTTPException(400, reason(e)) from e
    tid = secrets.token_hex(6)
    store.create_tournament(tid, t, created_by=acc.id)
    return {"id": tid, "bracket": t.bracket()}


@app.get("/tournaments/{tid}")
def tournament_show(tid: str):
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        # A fixed sentence rather than the exception's text: the message names
        # the id that was asked for, which is input coming straight back out.
        # It is escaped everywhere it is rendered, and it still tells the caller
        # nothing they did not already type.
        raise HTTPException(404, "no such tournament") from e
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
    except KeyError as e:
        raise HTTPException(404, "no such tournament") from e
    try:
        t.report(match_id, body.winner, body.score, body.match_keys)
    except KeyError as e:
        # A missing *match*, which is a different mistake from a missing
        # tournament and used to be reported as one.
        raise HTTPException(404, "no such match in this tournament") from e
    except ValueError as e:
        # Domain refusal: impossible score, or a winner who is not playing.
        raise HTTPException(400, reason(e)) from e
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
    """Leave a draft. Either side may; the other is told who left.

    Leaving a *queue* match that is still live is a dodge and costs a cooldown:
    the other player was paired with you by the server and gets nothing out of
    the match if you walk off. Asked before the state changes, because a draft
    the opponent already left is not one you are abandoning.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    dodge = s.is_dodge(acc)
    state = guard(s.cancel, acc)
    store.save_draft(s)
    # Whoever left is out of the queue too, or the client would offer to
    # rejoin a match it just abandoned.
    queue.leave(acc)
    if dodge:
        queue.note_dodge(acc)
        state["dodge_cooldown_s"] = queue.cooldown_left(acc)
    return state


@app.post("/drafts/{draft_id}/extend")
def draft_ask_extension(draft_id: str,
                        ladder_session: str | None = Cookie(None),
                        authorization: str | None = Header(None)):
    """Ask the other side for two more minutes of handoff time.

    A game that will not start is usually a port or a Steam problem rather than
    a refusal, and the answer to that is more time — not a penalty.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.ask_extension, acc)
    store.save_draft(s)
    return state


@app.post("/drafts/{draft_id}/extend/grant")
def draft_grant_extension(draft_id: str,
                          ladder_session: str | None = Cookie(None),
                          authorization: str | None = Header(None)):
    """Grant it. Only the side that was *not* asking may."""
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.grant_extension, acc)
    store.save_draft(s)
    return state


@app.post("/drafts/{draft_id}/ready")
def draft_note_ready(draft_id: str,
                     ladder_session: str | None = Cookie(None),
                     authorization: str | None = Header(None)):
    """The guest is in the lobby, which stops the join clock.

    Reported by the client that got in, because it is the only one that knows:
    the host sees a player connect but not which ladder account it is.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.note_ready, acc)
    store.save_draft(s)
    return state


@app.post("/drafts/{draft_id}/conclude")
def draft_conclude(draft_id: str, ladder_session: str | None = Cookie(None),
                   authorization: str | None = Header(None)):
    """Close out a decided series, freeing both sides to queue again.

    Either side may — both are equally stuck until somebody does, and the result
    is already in, so there is nothing left to disagree about.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.conclude, acc)
    store.save_draft(s)
    queue.leave(acc)
    return state


@app.post("/drafts/{draft_id}/host")
def draft_claim_host(draft_id: str, ladder_session: str | None = Cookie(None),
                     authorization: str | None = Header(None)):
    """Claim the host role, before the lobby exists.

    Without this both clients offered "I am hosting" until one pressed it, which
    is two people about to open the same match. Whoever is first settles it, and
    the other side switches to waiting for the join link.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.claim_host, acc)
    store.save_draft(s)
    return state


class VoidBody(BaseModel):
    #: "series" or "game:N".
    scope: str
    reason: str = ""


@app.post("/drafts/{draft_id}/void")
def draft_request_void(draft_id: str, body: VoidBody,
                       ladder_session: str | None = Cookie(None),
                       authorization: str | None = Header(None)):
    """Ask for a game or the series not to count. Takes effect when both agree.

    A crash, the wrong commander, the wrong map — the alternative to a mutual
    void is a rated result both players know is wrong. One-sided it must never
    be: that is exactly the claim a losing player has an interest in making
    alone.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.request_void, acc, body.scope, body.reason)
    store.save_draft(s)
    return state


@app.delete("/drafts/{draft_id}/void")
def draft_withdraw_void(draft_id: str,
                        ladder_session: str | None = Cookie(None),
                        authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.withdraw_void, acc)
    store.save_draft(s)
    return state


class GameBody(BaseModel):
    game: int
    #: "A" or "B" — the side that won, as the draft numbers them.
    winner: str
    #: Every SteamID64 the game log listed for this match. Checked against the
    #: two accounts that drafted: a game played by somebody else is not that
    #: match, and must not become a rating change.
    steam_ids: list[str] = []


@app.post("/drafts/{draft_id}/game")
def draft_note_game(draft_id: str, body: GameBody,
                    ladder_session: str | None = Cookie(None),
                    authorization: str | None = Header(None)):
    """Record one finished game of a drafted series.

    The clients report it from their own game log, because that is the only
    place the result exists. Three things follow: the winner's commander is
    spent, the next game's commanders are revealed to both sides, and the series
    can end at two wins instead of running all three games.
    """
    acc = require(session_token(ladder_session, authorization))
    s = guard(drafts.get, draft_id)
    state = guard(s.note_game, acc, body.game, body.winner, body.steam_ids)
    store.save_draft(s)
    return state


class LobbyBody(BaseModel):
    lobby_id: str
    #: The password the host's client generated. Steam's join link has no field
    #: for it and the game asks on entry, so without this the guest is sent to a
    #: prompt for something only the host knows.
    password: str | None = None
    #: The host wrote these settings into a *running* Forts, which reads them
    #: only while starting. Carried so the guest learns why no password arrives
    #: — they cannot see the other machine, and "he never sent it" was the
    #: conclusion they drew instead.
    restart_pending: bool = False


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
    state = guard(s.set_lobby, acc, lobby, body.password)
    s.host_restart_pending = bool(body.restart_pending)
    # Recomputed after the flag is set, or the answer to the host describes the
    # state one field out of date.
    state = s.public_state(acc)
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
    acc = require(session_token(ladder_session, authorization))
    presence.seen(acc.id)
    state = guard(queue.status, acc)
    # Carried on the poll that is already happening, so the client never has to
    # ask twice for two numbers.
    state["online"] = presence.online()
    return state


@app.post("/presence")
def presence_ping(ladder_session: str | None = Cookie(None),
                  authorization: str | None = Header(None)):
    """"I am still here" — and how many others are.

    The queue poll only runs while somebody is queueing, so a client sitting on
    any other screen needs a way to say so. A count comes back, never a list:
    people agreed to have their matches tracked, which is not the same as
    publishing when they are at their computer.
    """
    acc = require(session_token(ladder_session, authorization))
    presence.seen(acc.id)
    # The searcher counts ride along. This is the only call an idle client makes,
    # so without them the mode picker keeps whatever number it last saw — which
    # is how "1 waiting" survived the queue being empty.
    return {"online": presence.online(), "waiting": queue.waiting()}


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
    # Encoded, or a return path containing `&` or `#` would silently become
    # something else — and it is checked again at the other end anyway.
    return RedirectResponse(
        "/auth/discord/start?return_to=" + quote(safe_return_to(return_to),
                                                 safe="/"),
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
    flags = [{"id": r.id, "played_at": r.played_at, "games": r.games,
              "score_low": r.score_low, "rated": r.rated,
              "reasons": r.reasons, "flag_note": r.flag_note}
             for r in results.flagged()]
    return HTMLResponse(page.admin(
        accounts=_roster_rows(), grants=[g.value for g in Grant],
        my_id=acc.id, pools=queue_pools(), ranking_count=len(ranking.players),
        may_set_roles=acc.may("grant_role"), error=error, claims=claims,
        flags=flags))


@app.post("/admin/relink", response_class=HTMLResponse)
async def admin_relink(request: Request,
                       ladder_session: str | None = Cookie(None),
                       authorization: str | None = Header(None)):
    """Correct a wrong link, or set a ladder name directly.

    Steam proves a link and the person who made it cannot undo it — right for a
    claim, wrong for a mistake. Somebody who linked the wrong Steam account
    otherwise has no way back at all.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "link_other_account")
    form = await request.form()
    target = auth.accounts.get(str(form.get("account") or ""))
    if target is None:
        raise HTTPException(404, "unknown account")

    what = str(form.get("do") or "")
    try:
        if what == "unlink_steam":
            auth.unlink_steam(acc, target)
        elif what == "unlink_discord":
            auth.unlink_discord(acc, target)
        elif what == "name":
            name = str(form.get("ufer_name") or "").strip()
            if not name:
                return _admin_page(acc, "A ladder name cannot be empty.")
            auth.set_ladder_name(acc, target, name)
        else:
            return _admin_page(acc, "Nothing to do.")
    except AuthError as e:
        return _admin_page(acc, reason(e))
    store.save_account(target, granted_by=acc.id)
    # Out of the queue as well: being paired needs a proven Steam ID, and an
    # account that is already waiting would otherwise be matched without one.
    if what == "unlink_steam":
        queue.leave(target)
    return RedirectResponse("/admin", status_code=303)


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
        return _admin_page(acc, reason(e))
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
        return _admin_page(acc, reason(e))
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


def _planner_page(acc, tid: str, error: str = "") -> HTMLResponse:
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        # A fixed sentence rather than the exception's text: the message names
        # the id that was asked for, which is input coming straight back out.
        # It is escaped everywhere it is rendered, and it still tells the caller
        # nothing they did not already type.
        raise HTTPException(404, "no such tournament") from e
    return HTMLResponse(page.planner(
        name=t.name, mode=t.mode.key, best_of=t.series_length(),
        seeding=t.seeding,
        entrants=[{"name": p.name, "rating": p.rating} for p in t.participants],
        modes=_mode_choices(), tid=tid,
        is_admin=acc.may("link_other_account"),
        data=viewer_data(t, tid) if len(t.participants) >= 2 else None,
        error=error))


@app.get("/manage/plan/{tid}", response_class=HTMLResponse)
def plan_page(tid: str, ladder_session: str | None = Cookie(None),
              authorization: str | None = Header(None)):
    """A tournament being built.

    Separate from the bracket page on purpose: while planning, everything is
    editable and nothing can be reported; afterwards it is the other way round.
    """
    acc = current(session_token(ladder_session, authorization))
    if acc is None:
        return _login_first(path_for("manage", "plan", tid))
    guard(acc.require, "create_tournament")
    if not store.is_planning(tid):
        return RedirectResponse(path_for("manage", "tournaments", tid),
                            status_code=303)
    return _planner_page(acc, tid)


@app.post("/manage/plan/{tid}", response_class=HTMLResponse)
async def plan_edit(tid: str, request: Request,
                    ladder_session: str | None = Cookie(None),
                    authorization: str | None = Header(None)):
    """One change to a tournament being planned.

    Every action is small and immediately visible in the bracket underneath,
    which is the difference between planning something and filling in a form.
    """
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "create_tournament")
    if not store.is_planning(tid):
        raise HTTPException(400, "this tournament has already started")

    form = await request.form()
    what = str(form.get("do") or "")
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        # A fixed sentence rather than the exception's text: the message names
        # the id that was asked for, which is input coming straight back out.
        # It is escaped everywhere it is rendered, and it still tells the caller
        # nothing they did not already type.
        raise HTTPException(404, "no such tournament") from e
    people = list(t.participants)

    def rating_of(raw: str) -> float:
        """A number, or the default. Never NaN or infinity: both parse and
        neither can be stored or sorted."""
        try:
            value = float(raw)
        except ValueError:
            return 1000.0
        return value if math.isfinite(value) else 1000.0

    if what == "add":
        # A pasted block counts as one per line, because that is how a sign-up
        # list arrives.
        for line in str(form.get("name") or "").splitlines():
            parsed = _parse_entrants(line)
            for person in parsed:
                if person.rating == 1000.0 and form.get("rating"):
                    person.rating = rating_of(str(form.get("rating")))
                if any(x.name == person.name for x in people):
                    continue
                people.append(person)
    elif what in ("edit", "remove", "up", "down"):
        try:
            seat = int(str(form.get("seat")))
        except ValueError as e:
            raise HTTPException(400, "seat must be a number") from e
        if not 0 <= seat < len(people):
            return _planner_page(acc, tid, "That entrant is no longer there.")
        if what == "remove":
            people.pop(seat)
        elif what == "edit":
            name = str(form.get("name") or "").strip()
            if not name:
                return _planner_page(acc, tid, "An entrant needs a name.")
            if any(x.name == name for i, x in enumerate(people) if i != seat):
                return _planner_page(acc, tid, f"{name} is already in it.")
            people[seat].name = name
            people[seat].members = [name]
            people[seat].rating = rating_of(str(form.get("rating") or "1000"))
        elif what == "up" and seat > 0:
            people[seat - 1], people[seat] = people[seat], people[seat - 1]
        elif what == "down" and seat < len(people) - 1:
            people[seat + 1], people[seat] = people[seat], people[seat + 1]
    elif what == "format":
        mode = BY_KEY.get(str(form.get("mode") or "")) or t.mode
        raw_bo = str(form.get("best_of") or "").strip()
        best_of = int(raw_bo) if raw_bo.isdigit() and int(raw_bo) in (1, 3, 5, 7) \
            else None
        seeding = str(form.get("seeding") or "rating")
        if seeding not in ("rating", "listed", "random"):
            seeding = "rating"
        name = str(form.get("name") or "").strip() or t.name
        store.set_tournament_format(tid, name, mode.key, seeding, best_of)
        return RedirectResponse(path_for("manage", "plan", tid), status_code=303)
    elif what == "start":
        if len(people) < 2:
            return _planner_page(acc, tid, "A tournament needs two entrants.")
        store.set_planning(tid, False)
        return RedirectResponse(path_for("manage", "tournaments", tid),
                            status_code=303)
    else:
        return _planner_page(acc, tid, "Nothing to do.")

    store.replace_participants(tid, people)
    return RedirectResponse(path_for("manage", "plan", tid), status_code=303)


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
        # Opened as a plan: the entrant list is filled in from here on, and
        # the two-entrant minimum applies when it starts.
        t = Tournament(name, _parse_entrants(entrants), mode=mode,
                       seeding=seeding, best_of=best_of, planning=True)
    except ValueError as e:
        return again(reason(e))
    tid = secrets.token_hex(6)
    # Created as a plan, not as a finished bracket: a host adds people as they
    # sign up and looks at what comes out before anything is fixed.
    store.create_tournament(tid, t, created_by=acc.id, planning=True)
    return RedirectResponse(path_for("manage", "plan", tid), status_code=303)


def _bracket_page(acc, tid: str, error: str = "") -> HTMLResponse:
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        # A fixed sentence rather than the exception's text: the message names
        # the id that was asked for, which is input coming straight back out.
        # It is escaped everywhere it is rendered, and it still tells the caller
        # nothing they did not already type.
        raise HTTPException(404, "no such tournament") from e
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
        return _login_first(path_for("manage", "tournaments", tid))
    # Still being built: the planner is where it belongs, and only somebody who
    # may create one gets to see it half-finished.
    if store.is_planning(tid) and acc.may("create_tournament"):
        return RedirectResponse(path_for("manage", "plan", tid), status_code=303)
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
        # A fixed sentence rather than the exception's text: the message names
        # the id that was asked for, which is input coming straight back out.
        # It is escaped everywhere it is rendered, and it still tells the caller
        # nothing they did not already type.
        raise HTTPException(404, "no such tournament") from e
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
    # The two lookups are kept apart. Under one handler they shared a message,
    # and after that message became a fixed sentence a bad *seat* started
    # reporting a missing *tournament* — an answer that sends somebody looking
    # in the wrong place entirely.
    try:
        t = store.load_tournament(tid)
    except KeyError as e:
        raise HTTPException(404, "no such tournament") from e
    try:
        t.rename(seat, name)
    except KeyError:
        return _bracket_page(acc, tid, f"There is no entrant {seat} here.")
    except ValueError as e:
        return _bracket_page(acc, tid, reason(e))
    store.rename_participant(tid, seat, name.strip())
    return RedirectResponse(path_for("manage", "tournaments", tid),
                            status_code=303)


@app.post("/manage/tournaments/{tid}/report", response_class=HTMLResponse)
async def bracket_page_report(tid: str, request: Request,
                              ladder_session: str | None = Cookie(None),
                              authorization: str | None = Header(None)):
    acc = require(session_token(ladder_session, authorization))
    guard(acc.require, "run_tournament")
    if store.is_planning(tid):
        raise HTTPException(400, "this tournament has not started yet")
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
    except KeyError as e:
        raise HTTPException(404, "no such tournament") from e
    try:
        t.report(match_id, winner, score)
    except KeyError:
        return _bracket_page(acc, tid, "There is no such match in this bracket.")
    except ValueError as e:
        return _bracket_page(acc, tid, reason(e))
    store.record_result(tid, match_id, winner, score)
    if t.finished:
        store.mark_finished(tid)
    return RedirectResponse(path_for("manage", "tournaments", tid),
                            status_code=303)


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


class FlagBody(BaseModel):
    note: str = ""


@app.post("/results/{result_id}/flag")
def flag_result(result_id: str, body: FlagBody,
                ladder_session: str | None = Cookie(None),
                authorization: str | None = Header(None)):
    """Ask for a human to look at one of your own series.

    This is why a series that cannot be rated is stored rather than dropped:
    "it did not count" is sometimes the software being wrong, and the person it
    happened to is the only one who knows. Without this they would have to find
    an admin on Discord and describe a match from memory.
    """
    acc = require(session_token(ladder_session, authorization))
    r = guard(results.flag, acc, result_id, body.note)
    return {"id": r.id, "flagged": r.flagged}


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
                        "rated": r.rated, "reasons": r.reasons,
                        "flagged": r.flagged, "flag_note": r.flag_note}
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
