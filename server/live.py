"""Live matches and spectator requests.

A list of what is being played right now; whoever wants to watch asks, and
the host decides. Today that happens as "can I come in?" in Discord, which
the people currently playing are not reading.

The hard limit stays hard: Forts allows nine clients and spectators count
towards it. A request can therefore be refused for a reason that has nothing
to do with the person — there is simply no room — and it says so, or someone
takes it personally.

The host decides, with two exceptions: they can switch requests off
entirely, and admins get in anyway per the league rules. Even an admin does
not fit into a full lobby.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

from .auth import Account, AuthError, Role

#: Without a heartbeat for this long a match counts as over, so a crashed
#: client does not leave a ghost entry in the list.
STALE_AFTER_S = 180.0


class RequestState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"


@dataclass
class ObserverRequest:
    id: str
    match_id: str
    account_id: str
    display_name: str
    created_at: float
    state: RequestState = RequestState.PENDING
    reason: str = ""


@dataclass
class LiveMatch:
    id: str
    host_account_id: str
    mode_key: str
    mode_label: str
    players: list[str]
    #: Total client slots in use (players plus spectators).
    slots_used: int
    slots_total: int
    lobby_id: int | None = None
    #: The host's SteamID64. Steam's join URL needs the lobby owner's account —
    #: without it the link does not join, which is how the drafted handoff
    #: failed until it was passed properly.
    host_steam: str | None = None
    password_protected: bool = True
    observers: list[str] = field(default_factory=list)
    accepting_requests: bool = True
    started_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    tournament: str | None = None

    @property
    def free_slots(self) -> int:
        return max(0, self.slots_total - self.slots_used)

    def stale(self, now: float) -> bool:
        return now - self.last_seen > STALE_AFTER_S

    def public(self) -> dict:
        """What anyone may see — deliberately without the lobby ID and
        password, which only admitted spectators get."""
        return {
            "id": self.id,
            "mode": self.mode_label,
            "players": self.players,
            "observers": self.observers,
            "free_slots": self.free_slots,
            "accepting_requests": self.accepting_requests and self.free_slots > 0,
            "running_for_s": round(time.time() - self.started_at),
            "tournament": self.tournament,
        }


class LiveService:
    def __init__(self, now=time.time) -> None:
        self._now = now
        self.matches: dict[str, LiveMatch] = {}
        self.requests: dict[str, ObserverRequest] = {}

    # ------------------------------------------------------------ Publishing
    def publish(self, host: Account, mode_key: str, mode_label: str,
                players: list[str], slots_used: int, slots_total: int,
                lobby_id: int | None = None,
                tournament: str | None = None) -> LiveMatch:
        host.require("publish_live_match")
        # A live entry names who is playing, so it is publication like any
        # other. The host cannot consent for the table, only for themselves.
        if not host.trackable:
            raise AuthError(
                "you have not agreed to be listed — opt in first "
                "(POST /me/consent)")
        m = LiveMatch(
            id=secrets.token_hex(6), host_account_id=host.id,
            mode_key=mode_key, mode_label=mode_label, players=list(players),
            slots_used=slots_used, slots_total=slots_total,
            lobby_id=lobby_id, tournament=tournament,
            # Taken from the account rather than the request body: the host is
            # whoever is logged in, and their Steam ID is already proven.
            host_steam=host.steam_id,
            started_at=self._now(), last_seen=self._now())
        self.matches[m.id] = m
        return m

    def heartbeat(self, match_id: str, slots_used: int | None = None) -> None:
        m = self.matches.get(match_id)
        if m is None:
            return
        m.last_seen = self._now()
        if slots_used is not None:
            m.slots_used = slots_used

    def finish(self, match_id: str) -> None:
        self.matches.pop(match_id, None)

    def prune(self) -> list[str]:
        """Drop orphaned entries, returning the removed ids."""
        now = self._now()
        gone = [mid for mid, m in self.matches.items() if m.stale(now)]
        for mid in gone:
            del self.matches[mid]
            for r in self.requests.values():
                if r.match_id == mid and r.state is RequestState.PENDING:
                    r.state = RequestState.EXPIRED
                    r.reason = "match is over"
        return gone

    def listing(self) -> list[dict]:
        self.prune()
        return [m.public() for m in
                sorted(self.matches.values(), key=lambda m: m.started_at)]

    # -------------------------------------------------------------- Requests
    def set_accepting(self, actor: Account, match_id: str, value: bool) -> None:
        m = self._match(match_id)
        if m.host_account_id != actor.id and not actor.may("override_observer_lock"):
            raise AuthError("only the host can switch requests on or off")
        m.accepting_requests = value

    def requests_for(self, account: Account) -> list[dict]:
        """This account's own requests, with the answer.

        Without it a spectator pressed "ask to watch" and never learned the
        outcome: the only route was the host's inbox, which is somebody else's.
        """
        out = []
        for r in self.requests.values():
            if r.account_id != account.id:
                continue
            m = self.matches.get(r.match_id)
            row = {"id": r.id, "match_id": r.match_id,
                   "state": r.state.value, "reason": r.reason,
                   "players": m.players if m else [],
                   "mode": m.mode_label if m else None}
            if r.state is RequestState.APPROVED and m is not None:
                # The lobby id is the thing that lets someone in, so it appears
                # only here and only once the host has said yes.
                row["lobby_id"] = str(m.lobby_id) if m.lobby_id else None
                row["join_url"] = self._join_url(m)
            out.append(row)
        return sorted(out, key=lambda x: x["id"])

    @staticmethod
    def _join_url(m: "LiveMatch") -> str | None:
        """Steam's join link, with the lobby owner's account in it.

        Leaving the owner out and letting Steam work it out does not join — the
        drafted handoff proved that the hard way.
        """
        if not m.lobby_id:
            return None
        owner = m.host_steam or "0"
        return f"steam://joinlobby/410900/{m.lobby_id}/{owner}"

    def request_observer(self, account: Account, match_id: str) -> ObserverRequest:
        account.require("request_observer")
        m = self._match(match_id)

        # Ranked games are not a spectator sport here. A watcher in a rated
        # series is one more person who knows what is on the board, and the
        # scene's own habit is to keep those closed. Admins and casters are the
        # exception because arbitrating needs seeing.
        if m.mode_key.startswith("ranked")                 and not account.may("override_observer_lock"):
            r = ObserverRequest(secrets.token_hex(6), match_id, account.id,
                                account.ufer_name or account.discord_name or "?",
                                self._now())
            r.state = RequestState.DECLINED
            r.reason = ("ranked matches are not open to spectators — "
                        "unranked and tournament games are")
            self.requests[r.id] = r
            return r

        if any(r.account_id == account.id and r.match_id == match_id
               and r.state is RequestState.PENDING for r in self.requests.values()):
            raise AuthError("a request is already pending")

        r = ObserverRequest(secrets.token_hex(6), match_id, account.id,
                            account.ufer_name or account.discord_name or "?",
                            self._now())
        self.requests[r.id] = r

        # "No room" is not a judgement about the person, and the reply has
        # to say so.
        if m.free_slots <= 0:
            r.state = RequestState.DECLINED
            r.reason = ("lobby is full — Forts allows nine clients, "
                        "spectators included")
            return r

        if not m.accepting_requests:
            if account.may("override_observer_lock"):
                # League rules: admins may always observe.
                r.state = RequestState.APPROVED
                r.reason = "admin — always admitted per the rules"
                self._admit(m, r)
                return r
            r.state = RequestState.DECLINED
            r.reason = "the host switched requests off"
            return r
        return r

    def answer(self, actor: Account, request_id: str, approve: bool,
               reason: str = "") -> ObserverRequest:
        r = self.requests.get(request_id)
        if r is None:
            raise AuthError("unknown request")
        m = self._match(r.match_id)
        if m.host_account_id != actor.id and not actor.may("override_observer_lock"):
            raise AuthError("only the host answers requests for their match")
        if r.state is not RequestState.PENDING:
            raise AuthError(f"request is already {r.state.value}")

        if approve and m.free_slots <= 0:
            r.state = RequestState.DECLINED
            r.reason = "no room left by now"
            return r
        r.state = RequestState.APPROVED if approve else RequestState.DECLINED
        r.reason = reason or ("admitted" if approve else "declined")
        if approve:
            self._admit(m, r)
        return r

    def _admit(self, m: LiveMatch, r: ObserverRequest) -> None:
        m.observers.append(r.display_name)
        m.slots_used += 1

    def pending_for_host(self, host: Account) -> list[ObserverRequest]:
        mine = {m.id for m in self.matches.values()
                if m.host_account_id == host.id}
        return [r for r in self.requests.values()
                if r.match_id in mine and r.state is RequestState.PENDING]

    def join_info(self, account: Account, request_id: str) -> dict:
        """Hand out the lobby ID, only to admitted spectators.

        That is why `public()` omits it: the lobby ID is what actually lets
        someone join.
        """
        r = self.requests.get(request_id)
        if r is None or r.account_id != account.id:
            raise AuthError("unknown request")
        if r.state is not RequestState.APPROVED:
            raise AuthError(f"request is {r.state.value}, not admitted")
        m = self._match(r.match_id)
        return {
            "lobby_id": str(m.lobby_id) if m.lobby_id else None,
            "join_url": self._join_url(m),
            "password_protected": m.password_protected,
        }

    def _match(self, match_id: str) -> LiveMatch:
        m = self.matches.get(match_id)
        if m is None:
            raise AuthError("unknown or finished match")
        return m
