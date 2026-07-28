"""Login and permissions — without ever seeing a password.

Discord is the identity anchor because the ranking sheet lists Discord
names: logging in with Discord proves the very name a player is listed
under. That is the difference between "says they are X" and "is the Discord
account listed as X".

The Steam ID comes separately through Steam OpenID, giving the full chain:

    Discord account -> ladder name -> SteamID64 -> recorded matches
        (proven)        (sheet)       (proven)      (game log)

Both flows are redirects: the user authenticates with Discord or Steam and
we only receive a short-lived token. This module therefore contains no
password fields, no hashes and no credentials. Application secrets come from
environment variables and never belong in the repository.

Logging in is optional. Recording your own matches needs no account; one is
only required to appear on the ladder, queue, or run a tournament.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum

#: Short enough that a forgotten machine is not permanent access, long
#: enough for a tournament evening.
SESSION_TTL_S = 7 * 24 * 3600
#: The redirect state must expire, or an intercepted link can be replayed
#: later.
OAUTH_STATE_TTL_S = 600

#: A pairing code is read off a screen and typed by hand, so it is short — and
#: anything short has to be short-lived.
PAIRING_TTL_S = 300


class Role(IntEnum):
    """Authority level. Higher means more."""
    GUEST = 0
    PLAYER = 1
    CASTER = 2
    ADMIN = 3
    OWNER = 4

    @property
    def label(self) -> str:
        return {Role.GUEST: "Guest", Role.PLAYER: "Player",
                Role.CASTER: "Caster", Role.ADMIN: "Admin",
                Role.OWNER: "Owner"}[self]


class Grant(str, Enum):
    """Additional permissions, independent of rank.

    Rank (`Role`) is authority: who may arbitrate, who may hand out roles. A
    grant unlocks one capability without making anyone an admin.

    The official Discord works the same way — Map Creator, Mod Maker and
    Content Creator are badges and responsibilities, not moderation levels. A
    tournament host should be able to create tournaments without being able
    to read other people's accounts.
    """
    TOURNAMENT_HOST = "tournament_host"
    REFEREE = "referee"
    CASTER = "caster"
    MAP_MAKER = "map_maker"
    MOD_MAKER = "mod_maker"
    CONTENT_CREATOR = "content_creator"

    @property
    def label(self) -> str:
        return {
            Grant.TOURNAMENT_HOST: "Tournament Host",
            Grant.REFEREE: "Referee",
            Grant.CASTER: "Caster",
            Grant.MAP_MAKER: "Map Creator",
            Grant.MOD_MAKER: "Mod Maker",
            Grant.CONTENT_CREATOR: "Content Creator",
        }[self]


# One explicit table instead of scattered checks, so there is a single place
# that says why something is refused.
REQUIRED_ROLE: dict[str, Role] = {
    "report_own_match": Role.PLAYER,
    "join_queue": Role.PLAYER,
    "request_observer": Role.PLAYER,
    "publish_live_match": Role.PLAYER,
    "create_tournament": Role.ADMIN,
    "run_tournament": Role.ADMIN,
    "report_any_match": Role.ADMIN,
    "link_other_account": Role.ADMIN,
    "override_observer_lock": Role.ADMIN,
    "grant_role": Role.OWNER,
    "grant_permission": Role.ADMIN,
}

#: What each grant unlocks, without a promotion.
GRANT_UNLOCKS: dict[Grant, set[str]] = {
    Grant.TOURNAMENT_HOST: {"create_tournament", "run_tournament"},
    # A referee has to correct results and watch any match, or they cannot
    # arbitrate.
    Grant.REFEREE: {"report_any_match", "override_observer_lock",
                    "run_tournament"},
    Grant.CASTER: {"override_observer_lock"},
    Grant.MAP_MAKER: set(),
    Grant.MOD_MAKER: set(),
    Grant.CONTENT_CREATOR: set(),
}


class AuthError(Exception):
    """Authentication or permission failure."""


@dataclass
class Account:
    id: str
    discord_id: str | None = None
    discord_name: str | None = None
    steam_id: str | None = None
    ufer_name: str | None = None
    role: Role = Role.PLAYER
    grants: set[Grant] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    #: Agreement to have results tracked. Defaults to False and stays a
    #: separate act from signing in: an account created to watch a stream is
    #: not a request to be rated. Only accounts with this set may appear in
    #: the ladder, which is the condition this project was cleared under.
    tracking_consent: bool = False
    consent_since: str | None = None

    @property
    def verified(self) -> bool:
        """Full chain: Discord proves the name, Steam proves the ID."""
        return self.discord_id is not None and self.steam_id is not None

    @property
    def trackable(self) -> bool:
        """Consent alone is not enough — an unverified account could consent
        on someone else's behalf, so the Steam ID has to be proven too."""
        return self.tracking_consent and self.steam_id is not None

    def unlocked(self) -> set[str]:
        """Everything the held grants unlock."""
        out: set[str] = set()
        for g in self.grants:
            out |= GRANT_UNLOCKS.get(g, set())
        return out

    def may(self, action: str) -> bool:
        needed = REQUIRED_ROLE.get(action)
        if needed is None:
            raise KeyError(f"unknown action {action!r}")
        return self.role >= needed or action in self.unlocked()

    def require(self, action: str) -> None:
        if self.may(action):
            return
        needed = REQUIRED_ROLE[action]
        # Name both routes, or nobody learns that a grant exists alongside
        # the rank.
        via = [g.label for g, acts in GRANT_UNLOCKS.items() if action in acts]
        hint = f" or the {', '.join(via)} grant" if via else ""
        raise AuthError(
            f"{action} requires {needed.label}{hint}; "
            f"your account is {self.role.label}"
            + (f" with {', '.join(g.label for g in sorted(self.grants, key=str))}"
               if self.grants else " with no extra grants"))


@dataclass
class Session:
    token: str
    account_id: str
    created_at: float
    expires_at: float

    def alive(self, now: float | None = None) -> bool:
        return (now or time.time()) < self.expires_at


@dataclass
class PendingLogin:
    """A login in progress (the OAuth `state`)."""
    state: str
    provider: str
    created_at: float
    #: Where to continue after a successful login.
    return_to: str = "/"

    def alive(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at < OAUTH_STATE_TTL_S


class AuthService:
    """Account management. Storage is pluggable (see `store`)."""

    def __init__(self, now=time.time) -> None:
        self._now = now
        self.accounts: dict[str, Account] = {}
        self.sessions: dict[str, Session] = {}
        self.pending: dict[str, PendingLogin] = {}
        #: code -> (account id, expiry). Not persisted: a pairing code that
        #: survived a restart would outlive the screen it was shown on.
        self._pairings: dict[str, tuple[str, float]] = {}

    # ---------------------------------------------------------- Login flow
    def begin_login(self, provider: str, return_to: str = "/") -> PendingLogin:
        """Step 1: create the state the provider hands back.

        The random value guards against forged logins: a response without a
        matching state was started by someone else.
        """
        if provider not in ("discord", "steam"):
            raise AuthError(f"unknown provider {provider!r}")
        p = PendingLogin(secrets.token_urlsafe(24), provider, self._now(),
                         return_to)
        self.pending[p.state] = p
        return p

    def consume_state(self, state: str) -> PendingLogin:
        p = self.pending.pop(state, None)
        if p is None:
            raise AuthError("unknown or already used login attempt")
        if not p.alive(self._now()):
            raise AuthError("login attempt expired — please start again")
        return p

    # ----------------------------------------------------------- Accounts
    def login_discord(self, discord_id: str, discord_name: str) -> Account:
        """Step 2: accept a confirmed Discord identity.

        Only called *after* the provider validated the token. This module
        never talks to Discord itself, which keeps it testable offline.
        """
        acc = next((a for a in self.accounts.values()
                    if a.discord_id == discord_id), None)
        if acc is None:
            acc = Account(id=secrets.token_hex(8), discord_id=discord_id,
                          discord_name=discord_name)
            self.accounts[acc.id] = acc
        else:
            # Discord names change, the ID does not.
            acc.discord_name = discord_name
        return acc

    def attach_steam(self, account: Account, steam_id: str) -> None:
        """Attach a Steam ID, proven via Steam OpenID."""
        if not (steam_id.isdigit() and len(steam_id) == 17):
            raise AuthError(f"{steam_id!r} is not a SteamID64")
        other = next((a for a in self.accounts.values()
                      if a.steam_id == steam_id and a.id != account.id), None)
        if other is not None:
            # Two accounts sharing a Steam ID would rate the same matches
            # twice. A human has to resolve that.
            raise AuthError(
                "This Steam ID already belongs to another account. "
                "An admin can merge them.")
        account.steam_id = steam_id

    def claim_ufer_name(self, account: Account, ufer_name: str,
                        by_admin: bool = False) -> None:
        """Claim a ladder name.

        With a Discord login this is more than a claim: the sheet lists
        Discord names, so a matching login name proves it. If they differ an
        admin must confirm — plenty of people are listed under something else.
        """
        taken = next((a for a in self.accounts.values()
                      if a.ufer_name == ufer_name and a.id != account.id), None)
        if taken is not None:
            raise AuthError(f"{ufer_name!r} already belongs to another account")
        matches_discord = (account.discord_name or "").casefold() == \
            ufer_name.casefold()
        if not matches_discord and not by_admin:
            raise AuthError(
                f"Your Discord name ({account.discord_name}) is not "
                f"{ufer_name!r}. An admin can confirm the link.")
        account.ufer_name = ufer_name

    # -------------------------------------------------------------- Consent
    def set_tracking_consent(self, account: Account, value: bool) -> None:
        """Opt in or out of being tracked.

        Withdrawal is not a courtesy feature — the whole gate is worthless if
        it only works in one direction. Nothing is deleted here: results stop
        counting because the rating is recomputed from events and the filter
        stops passing them.
        """
        if value and account.steam_id is None:
            raise AuthError(
                "link your Steam account first — consent without a proven "
                "Steam ID could be given on someone else's behalf")
        account.tracking_consent = value
        account.consent_since = time.strftime("%Y-%m-%d") if value else None

    def require_trackable(self, account: Account) -> None:
        if account.trackable:
            return
        raise AuthError(
            "this account has not agreed to be tracked"
            if account.steam_id is not None else
            "this account has no linked Steam ID and cannot be tracked")

    def trackable_ids(self) -> set[str]:
        """The consent roster the clients sync. Ids only — a client does not
        need to know who else has an account to check whether a match
        counts."""
        return {a.steam_id for a in self.accounts.values()
                if a.trackable and a.steam_id}

    def grant_permission(self, actor: Account, target: Account,
                         grant: Grant) -> None:
        """Grant a permission — tournament host, referee, caster.

        Requires admin, not owner: grants are responsibilities, not a
        transfer of power.
        """
        actor.require("grant_permission")
        target.grants.add(grant)

    def revoke_permission(self, actor: Account, target: Account,
                          grant: Grant) -> None:
        actor.require("grant_permission")
        target.grants.discard(grant)

    def grant_role(self, actor: Account, target: Account, role: Role) -> None:
        actor.require("grant_role")
        if role >= actor.role and actor.role is not Role.OWNER:
            raise AuthError("cannot grant a role you do not outrank")
        target.role = role

    # ------------------------------------------------------------- Sessions
    def start_session(self, account: Account) -> Session:
        now = self._now()
        s = Session(secrets.token_urlsafe(32), account.id, now,
                    now + SESSION_TTL_S)
        self.sessions[s.token] = s
        return s

    # ------------------------------------------------------- Pairing a client
    def begin_pairing(self, account: Account) -> str:
        """A short code the desktop client can trade for a session.

        Needed because the login happens in a browser and the cookie stays
        there — the client is a separate process with its own cookie jar and
        can never see it. Embedding a browser instead would mean shipping one
        and asking people to type their Discord password into our window,
        which is worse in every way.

        Short, single-use, and short-lived: it is read off a screen and typed,
        so it has to be small, and anything small has to expire.
        """
        code = "-".join(secrets.token_hex(2).upper() for _ in range(2))
        self._pairings[code] = (account.id, self._now() + PAIRING_TTL_S)
        return code

    def claim_pairing(self, code: str) -> Session:
        entry = self._pairings.pop(code.strip().upper(), None)
        if entry is None:
            raise AuthError("unknown or already used pairing code")
        account_id, expires = entry
        if self._now() > expires:
            raise AuthError("this pairing code has expired")
        account = self.accounts.get(account_id)
        if account is None:
            raise AuthError("the account behind this code is gone")
        return self.start_session(account)

    def account_for(self, token: str | None) -> Account | None:
        """Account for a session token, or None when not logged in.

        Returns None rather than raising: most reads work without an account,
        so an exception would be the wrong default.
        """
        if not token:
            return None
        s = self.sessions.get(token)
        if s is None or not s.alive(self._now()):
            self.sessions.pop(token, None)
            return None
        return self.accounts.get(s.account_id)

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)


# ---------------------------------------------------------------------------
# Redirect URLs. Secrets come from the environment — a client secret in the
# repository would be public after the first push.
# ---------------------------------------------------------------------------
def discord_authorize_url(state: str, redirect_uri: str) -> str:
    client_id = os.environ.get("DISCORD_CLIENT_ID")
    if not client_id:
        raise AuthError("DISCORD_CLIENT_ID is not set — no login without "
                        "your own Discord application")
    from urllib.parse import urlencode
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # The minimum: id and display name. No messages, no guild list.
        "scope": "identify",
        "state": state,
    })


def exchange_discord_code(code: str, redirect_uri: str) -> dict:
    """Turn an authorization code into the user's Discord id and name.

    Two calls, both mandatory. The code proves nothing on its own: it is
    handed to us by the browser, so anyone can send one. Only the exchange —
    which requires the client secret we alone hold — establishes that Discord
    issued it to *our* application, and only the `/users/@me` call establishes
    who it belongs to.
    """
    client_id = os.environ.get("DISCORD_CLIENT_ID")
    client_secret = os.environ.get("DISCORD_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise AuthError(
            "DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET must be set — "
            "without the secret the code cannot be verified, and an "
            "unverified login is worse than none")

    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    def _post_form(url: str, form: dict) -> dict:
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    try:
        token = _post_form("https://discord.com/api/oauth2/token", {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })
    except urllib.error.HTTPError as e:
        # The usual cause is a redirect_uri that differs by one character from
        # the one registered in the Discord application, so say so.
        raise AuthError(
            f"Discord rejected the code ({e.code}). The redirect URI must "
            f"match the application exactly: {redirect_uri}") from e
    except (urllib.error.URLError, OSError) as e:
        raise AuthError(f"could not reach Discord: {e}") from e

    access = token.get("access_token")
    if not access:
        raise AuthError("Discord returned no access token")

    req = urllib.request.Request(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            me = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError) as e:
        raise AuthError(f"could not read the Discord profile: {e}") from e

    if not me.get("id"):
        raise AuthError("Discord profile has no id")
    # `username` is the handle; `global_name` is the display name people
    # actually go by, which is what the ranking lists.
    return {"id": str(me["id"]),
            "name": me.get("global_name") or me.get("username") or "?"}


#: Steam hands the claimed identity back as a URL ending in the SteamID64.
_STEAM_ID_RE = re.compile(r"^https?://steamcommunity\.com/openid/id/(\d{17})$")


def verify_steam_openid(params: dict[str, str]) -> str:
    """Verify a Steam OpenID response and return the SteamID64.

    The whole security of this rests on one thing: the parameters arrive in
    the *user's* query string, so they can be edited freely. Reading
    `openid.claimed_id` and trusting it would let anyone claim any Steam
    account. Steam has to be asked whether it really signed this response,
    which is what `check_authentication` does — the signature cannot be forged
    without Steam's key.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    claimed = params.get("openid.claimed_id", "")
    m = _STEAM_ID_RE.match(claimed)
    if not m:
        raise AuthError(f"not a Steam identity: {claimed!r}")

    # Echo every openid.* parameter back unchanged; the signature covers them.
    form = {k: v for k, v in params.items() if k.startswith("openid.")}
    form["openid.mode"] = "check_authentication"
    try:
        req = urllib.request.Request(
            "https://steamcommunity.com/openid/login",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode()
    except (urllib.error.URLError, OSError) as e:
        raise AuthError(f"could not reach Steam: {e}") from e

    # Steam answers a tiny key:value document. Anything other than an explicit
    # `is_valid:true` is a rejection — including a missing line.
    valid = any(line.strip() == "is_valid:true" for line in body.splitlines())
    if not valid:
        raise AuthError("Steam did not confirm this login")
    return m.group(1)


def steam_openid_url(return_to: str, realm: str) -> str:
    from urllib.parse import urlencode
    return "https://steamcommunity.com/openid/login?" + urlencode({
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    })
