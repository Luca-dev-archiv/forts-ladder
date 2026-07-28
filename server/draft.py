"""Two-player drafts over the network.

`ladder/draft.py` holds the rules and is unchanged. What this adds is the part
that only exists once two people are involved: who is allowed to act, and
**what each of them is allowed to see**.

That second half is the reason this module exists rather than the client
running the engine twice. In the local hot-seat draft, "blind commander pick"
is a UI convention — one person is looking at both sides anyway. Over a
network it has to be a property of the server: if the state sent to A contains
B's pending pick, then blind is a promise the client makes and anyone with a
debugger or an HTTP proxy breaks it. So the pending pick never leaves this
module until both sides have locked in.

The same argument applies to the clock. A client-side timer can be stalled by
pausing the process, and two clients disagree about `now` anyway, so the
deadline is evaluated here on every request.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from ladder.draft import Action, Draft, Side

from .auth import Account, AuthError


@dataclass
class Seat:
    side: Side
    account_id: str
    display: str


@dataclass
class DraftSession:
    id: str
    join_code: str
    draft: Draft
    seats: dict[str, Seat] = field(default_factory=dict)   # account_id -> Seat
    created_at: float = field(default_factory=time.time)
    #: Set once, from the queue or a tournament match, so a report can be tied
    #: back to the draft that authorised it.
    series_id: str | None = None
    #: The pool as it was handed in. `Draft` removes the neutrally struck map
    #: from its own copy, so saving that one and rebuilding from it would strike
    #: a second map and change the board under the players.
    original_map_pool: list[str] = field(default_factory=list)
    #: Steam lobby the two of them agreed to play in, read out of the host's
    #: game log. This is the whole handoff: without it a finished draft is a
    #: list of maps and no way to get into a game.
    lobby_id: int | None = None
    #: Which side is hosting the lobby. The other side gets the join link.
    lobby_host: str | None = None
    #: Set when someone walked away. Kept rather than deleted, so the other
    #: side is told what happened instead of getting "unknown draft".
    cancelled_by: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancelled_by is not None

    # ------------------------------------------------------------------ Seats
    def seat_of(self, account: Account) -> Seat:
        seat = self.seats.get(account.id)
        if seat is None:
            raise AuthError("you are not in this draft")
        return seat

    def full(self) -> bool:
        return len(self.seats) >= 2

    def opponent_of(self, seat: Seat) -> Seat | None:
        return next((s for s in self.seats.values() if s.side is not seat.side),
                    None)

    # ------------------------------------------------------------------ State
    def public_state(self, account: Account | None) -> dict:
        """What one participant may see.

        Deliberately built per viewer. The blind pick of the *other* side is
        never included — only whether they have locked in, which both sides
        need to know and neither can exploit.
        """
        d = self.draft
        seat = self.seats.get(account.id) if account else None
        step = d.current

        waiting_on = None
        if step is not None:
            waiting_on = ("both" if step.side is None else step.side.value)

        # `d._pending_blind` is the one piece of state that must not be
        # serialised. Only the fact that a side has committed is exposed.
        locked = sorted(s.value for s in d._pending_blind)

        mine = None
        if seat is not None and seat.side in d._pending_blind:
            # Your own pick comes back to you, so the UI can show what you
            # chose while you wait for the other side.
            mine = d._pending_blind[seat.side]

        return {
            "id": self.id,
            "your_side": seat.side.value if seat else None,
            "cancelled": self.cancelled,
            "cancelled_by": self.cancelled_by,
            "lobby_id": str(self.lobby_id) if self.lobby_id else None,
            "lobby_host": self.lobby_host,
            "seats": {s.side.value: s.display for s in self.seats.values()},
            "full": self.full(),
            "done": d.done,
            "step_index": d.step_index,
            "step_total": len(d.steps),
            "waiting_on": waiting_on,
            "action": step.action.value if step else None,
            "game": step.game if step else None,
            # Not asked while a seat is empty: reading it starts the clock.
            "seconds_left": d.seconds_left() if self.full() else None,
            "map_pool": list(d.map_pool),
            "neutral_strike": d.neutral_strike,
            "banned_maps": d.banned_maps(),
            "picked_maps": {str(k): v for k, v in d.picked_maps().items()},
            "banned_commanders": d.banned_commanders(),
            # Ids only. Display names live in the game's own language files, and
            # the server has no Forts installation — computing them here
            # produced "Overclocker" for what the game calls Overdrive. The
            # client has the game and resolves them.
            "commander_pool": list(d.commander_pool),
            "locked_in": locked,
            "your_pending_pick": mine,
            "options": (d.legal_options(seat.side) if seat and step else []),
            "plan": d.plan(),
        }

    # ------------------------------------------------------------------ Moves
    def cancel(self, account: Account) -> dict:
        """Walk away. Either side may, and both are told who did.

        Not a delete: the other player is sitting in front of a board waiting
        for a move, and "no draft with that id" is a worse answer than "the
        other side left".
        """
        seat = self.seat_of(account)
        if not self.cancelled:
            self.cancelled_by = seat.side.value
        return self.public_state(account)

    def set_lobby(self, account: Account, lobby_id: int) -> dict:
        """Name the lobby the series will be played in.

        Only a seat may, and only once: the id is what decides which recorded
        games count for this series, so letting it be rewritten mid-series
        would let one side re-point it at a different game.
        """
        seat = self.seat_of(account)
        if self.cancelled:
            raise AuthError("this draft was cancelled")
        if not self.draft.done:
            raise AuthError("the draft is not finished yet")
        if self.lobby_id is not None and self.lobby_id != lobby_id:
            raise AuthError(f"this draft is already in lobby {self.lobby_id}")
        self.lobby_id = int(lobby_id)
        self.lobby_host = seat.side.value
        return self.public_state(account)

    def apply(self, account: Account, value: str) -> dict:
        """Make a move on behalf of one participant."""
        if self.cancelled:
            raise AuthError(f"side {self.cancelled_by} left this draft")
        if not self.full():
            raise AuthError("waiting for the second player")
        seat = self.seat_of(account)
        self.tick()                      # a timeout may have moved things on
        step = self.draft.current
        if step is None:
            raise AuthError("the draft is finished")
        if step.side is not None and step.side is not seat.side:
            raise AuthError(f"side {step.side.value} is on turn, not you")
        if step.action is Action.PICK_COMMANDER and seat.side in self.draft._pending_blind:
            raise AuthError("you already locked in for this game")
        try:
            self.draft.apply(value, seat.side)
        except ValueError as e:
            raise AuthError(str(e)) from e
        return self.public_state(account)

    def tick(self) -> list[str]:
        """Resolve anything the clock decided. Called on every request, so a
        client that stops polling cannot freeze the draft for the other.

        Does nothing while a seat is empty. `deadline()` starts the clock the
        first time it is asked, so merely *looking* at a draft that was waiting
        for an opponent started the timer and steps were then drawn by lot with
        nobody there to make them.
        """
        if not self.full() or self.cancelled:
            return []
        return self.draft.tick()


class DraftService:
    def __init__(self, now=time.time) -> None:
        self._now = now
        self.sessions: dict[str, DraftSession] = {}

    def create(self, host: Account, map_pool: list[str],
               commander_pool: list[str], best_of: int = 3,
               commander_bans_per_side: int = 1,
               step_seconds: float | None = 30.0,
               series_id: str | None = None) -> DraftSession:
        host.require("join_queue")
        # The strike seed is stored in the session, so the neutral strike can
        # be recomputed by anyone checking the draft afterwards. Drawing it
        # per request would make it unverifiable.
        seed = secrets.randbelow(2**31) if hasattr(secrets, "randbelow") \
            else secrets.randbits(31)
        try:
            draft = Draft(map_pool=list(map_pool),
                          commander_pool=list(commander_pool),
                          best_of=best_of,
                          commander_bans_per_side=commander_bans_per_side,
                          strike_seed=seed, step_seconds=step_seconds)
        except ValueError as e:
            raise AuthError(str(e)) from e

        s = DraftSession(id=secrets.token_hex(6),
                         join_code=secrets.token_hex(3).upper(),
                         draft=draft, series_id=series_id,
                         created_at=self._now(),
                         original_map_pool=list(map_pool))
        s.seats[host.id] = Seat(Side.A, host.id, _name(host))
        self.sessions[s.id] = s
        return s

    def join(self, account: Account, join_code: str) -> DraftSession:
        account.require("join_queue")
        s = next((x for x in self.sessions.values()
                  if x.join_code == join_code.strip().upper()), None)
        if s is None:
            raise AuthError("no draft with that code")
        if account.id in s.seats:
            return s
        if s.full():
            raise AuthError("this draft already has two players")
        s.seats[account.id] = Seat(Side.B, account.id, _name(account))
        # The first step gets its whole window from here, not from whenever
        # someone last looked at the lobby.
        s.draft._step_started = None
        return s

    def get(self, session_id: str) -> DraftSession:
        s = self.sessions.get(session_id)
        if s is None:
            raise AuthError("unknown draft")
        return s

    def restore(self, rows: list[dict]) -> int:
        """Rebuild drafts from stored setup and moves.

        The moves are replayed through the engine rather than a state being
        loaded, so a restored draft can only be something the engine would have
        produced itself. A move the rules now reject stops the replay for that
        draft instead of forcing it in — better a short board than an impossible
        one.
        """
        restored = 0
        for row in rows:
            try:
                draft = Draft(map_pool=list(row["map_pool"]),
                              commander_pool=list(row["commander_pool"]),
                              best_of=row["best_of"],
                              commander_bans_per_side=row["bans_per_side"],
                              strike_seed=row["strike_seed"],
                              step_seconds=row["step_seconds"])
            except ValueError:
                continue
            s = DraftSession(id=row["id"], join_code=row["join_code"],
                             draft=draft, series_id=row.get("series_id"),
                             created_at=row["created_at"],
                             original_map_pool=list(row["map_pool"]),
                             lobby_id=row.get("lobby_id"),
                             lobby_host=row.get("lobby_host"),
                             cancelled_by=row.get("cancelled_by"))
            for seat in row["seats"]:
                s.seats[seat["account_id"]] = Seat(
                    Side(seat["side"]), seat["account_id"], seat["display"])
            for c in row["choices"]:
                try:
                    draft.apply(c["value"], Side(c["side"]) if c["side"] else None)
                except (ValueError, KeyError):
                    break
            # A restored draft starts its current step fresh: the players were
            # not looking at a deadline that ran while the server was down.
            draft._step_started = None
            self.sessions[s.id] = s
            restored += 1
        return restored

    def prune(self, max_age_s: float = 6 * 3600) -> list[str]:
        """Drop stale sessions. A draft nobody finished should not sit in
        memory until the process restarts."""
        cutoff = self._now() - max_age_s
        gone = [i for i, s in self.sessions.items() if s.created_at < cutoff]
        for i in gone:
            del self.sessions[i]
        return gone


def _name(a: Account) -> str:
    return a.ufer_name or a.discord_name or "player"
