"""HTTP interface — a thin layer over `auth` and `live`.

The rules live in the domain modules, not here. This file only translates
between HTTP and calls, which is what makes the rules testable without
starting a server (see tests/test_server.py — 26 tests, no network).

Two things are missing before this can go live:

  * **A Discord application.** Client ID and secret have to be created by
    whoever runs the instance (discord.com/developers) and come from the
    environment; a secret in the repo is public after the first push.
  * **Token exchange and OpenID verification.** The two spots below refuse
    loudly instead of faking a result. Shipping them unverified gives you a
    login where anyone can claim to be anyone — worse than no login.

State lives in SQLite (`LADDER_DB`, default `data/ladder.sqlite`) and is
loaded at startup. Running matches stay deliberately volatile: after a
server restart the games carry on, and the clients re-announce themselves
within seconds.

    uvicorn server.app:app --reload
"""

from __future__ import annotations

import os
import secrets

from fastapi import Cookie, FastAPI, HTTPException, Response
from pydantic import BaseModel

from ladder.modes import BY_KEY
from ladder.tournament import Participant, Tournament

from .auth import (
    AuthError, AuthService, Grant, Role, discord_authorize_url,
    steam_openid_url,
)
from .live import LiveService
from .store import Store

app = FastAPI(title="Forts Ladder", version="0.1.0")

# Load persisted state at startup, or a restart logs everyone out and loses
# every running tournament.
store = Store(os.environ.get("LADDER_DB", "data/ladder.sqlite"))
auth = store.restore_auth(AuthService())
live = LiveService()

BASE_URL = os.environ.get("LADDER_BASE_URL", "http://localhost:8000")
SESSION_COOKIE = "ladder_session"


def current(token: str | None):
    return auth.account_for(token)


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
    pending = guard(auth.consume_state, state)

    # OPEN: the `code` still has to be exchanged for a token and used to
    # call /users/@me. Without that the login is worthless, so it refuses
    # loudly instead of continuing with invented data.
    raise HTTPException(
        501, "Discord token exchange is not wired up yet. Without it the "
             "login would be a mere claim.")


@app.get("/auth/steam/start")
def steam_start(return_to: str = "/"):
    guard(auth.begin_login, "steam", return_to)
    return {"url": steam_openid_url(f"{BASE_URL}/auth/steam/callback", BASE_URL)}


@app.get("/auth/steam/callback")
def steam_callback():
    # OPEN: Steam OpenID requires a `check_authentication` call back to
    # Steam. Without it anyone can append an arbitrary Steam ID.
    raise HTTPException(
        501, "OpenID verification is still missing — without it any Steam "
             "ID could be claimed.")


@app.post("/auth/logout")
def logout(response: Response, ladder_session: str | None = Cookie(None)):
    if ladder_session:
        auth.logout(ladder_session)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/me")
def me(ladder_session: str | None = Cookie(None)):
    acc = current(ladder_session)
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
def consent_grant(ladder_session: str | None = Cookie(None)):
    """Agree to have your results tracked.

    Separate from signing in on purpose: this project was cleared on the
    condition that it only tracks people who want it to, so the agreement is
    its own deliberate act rather than a side effect of logging in.
    """
    acc = require(ladder_session)
    guard(auth.set_tracking_consent, acc, True)
    store.save_account(acc)
    return {"tracking_consent": True, "since": acc.consent_since}


@app.delete("/me/consent")
def consent_withdraw(ladder_session: str | None = Cookie(None)):
    """Withdraw again. Past results stop counting on the next recompute."""
    acc = require(ladder_session)
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
                   ladder_session: str | None = Cookie(None)):
    """Record that a lobby was set up for a ladder match.

    The host's client can arm itself locally, but the guest's cannot — it
    never saw the lobby being created. Registering it here is what lets both
    sides agree that the match counts.
    """
    acc = require(ladder_session)
    guard(acc.require, "publish_live_match")
    guard(auth.require_trackable, acc)
    store.sanction_lobby(body.lobby_id, body.series_id, created_by=acc.id)
    return {"lobby_id": body.lobby_id, "sanctioned": True}


@app.get("/admin/roster")
def admin_roster(ladder_session: str | None = Cookie(None)):
    """The full roster — admins only.

    The operator holds this data anyway; the point is that nobody else can
    fetch it. Clients use POST /eligibility/check instead.
    """
    acc = require(ladder_session)
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
def live_publish(body: PublishBody, ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    m = guard(live.publish, acc, body.mode_key, body.mode_label, body.players,
              body.slots_used, body.slots_total, body.lobby_id, body.tournament)
    return {"match_id": m.id}


@app.post("/live/{match_id}/heartbeat")
def live_heartbeat(match_id: str, slots_used: int | None = None):
    live.heartbeat(match_id, slots_used)
    return {"ok": True}


@app.delete("/live/{match_id}")
def live_finish(match_id: str, ladder_session: str | None = Cookie(None)):
    require(ladder_session)
    live.finish(match_id)
    return {"ok": True}


@app.post("/live/{match_id}/accepting")
def live_accepting(match_id: str, value: bool,
                   ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    guard(live.set_accepting, acc, match_id, value)
    return {"accepting_requests": value}


# ------------------------------------------------------- Spectator requests
@app.post("/live/{match_id}/observe")
def observe(match_id: str, ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    r = guard(live.request_observer, acc, match_id)
    return {"request_id": r.id, "state": r.state.value, "reason": r.reason}


@app.get("/observe/requests")
def my_requests(ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    return {"pending": [
        {"id": r.id, "match_id": r.match_id, "who": r.display_name}
        for r in live.pending_for_host(acc)]}


@app.post("/observe/{request_id}/answer")
def answer(request_id: str, approve: bool, reason: str = "",
           ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    r = guard(live.answer, acc, request_id, approve, reason)
    return {"state": r.state.value, "reason": r.reason}


@app.get("/observe/{request_id}/join")
def join(request_id: str, ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
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
                      ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
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
                      ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
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
def add_grant(body: GrantBody, ladder_session: str | None = Cookie(None)):
    """Grant a permission — tournament host, referee, caster.

    Deliberately separate from /admin/role: a grant is a responsibility, not
    a transfer of power.
    """
    acc = require(ladder_session)
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
def remove_grant(body: GrantBody, ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    target = auth.accounts.get(body.target_id)
    if target is None:
        raise HTTPException(404, "unknown account")
    guard(auth.revoke_permission, acc, target, Grant(body.grant))
    store.save_account(target, granted_by=acc.id)
    return {"account": target.id,
            "grants": sorted(x.value for x in target.grants)}


@app.post("/admin/role")
def set_role(target_id: str, role: str,
             ladder_session: str | None = Cookie(None)):
    acc = require(ladder_session)
    target = auth.accounts.get(target_id)
    if target is None:
        raise HTTPException(404, "unknown account")
    try:
        wanted = Role[role.upper()]
    except KeyError:
        raise HTTPException(400, f"unknown role {role!r}")
    guard(auth.grant_role, acc, target, wanted)
    return {"account": target.id, "role": target.role.label}


@app.get("/health")
def health():
    return {"ok": True, "accounts": len(auth.accounts),
            "live_matches": len(live.matches)}
