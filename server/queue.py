"""Matchmaking over the network.

`ladder/matchmaking.py` holds the pairing rules — rating gap widening with
wait time, the accept window, the penalty for letting a proposal lapse, the
weekly cap per pairing. This wraps it in the parts that only matter with real
players on the other end:

  * The queue is ticked on **every** request rather than by a background
    thread. One process, a few hundred players, and the tick is a sort over a
    handful of entries — a scheduler would be more moving parts for no gain,
    and it would go quiet if the thread ever died.
  * Only accounts that opted in may queue. Being paired means appearing in
    someone else's match, which is the thing consent covers.
  * A proposal accepted by both sides creates the draft immediately. There is
    no window in which two players are matched but have nothing to click.
"""

from __future__ import annotations

import time

from ladder.matchmaking import ACCEPT_TIMEOUT_S, Proposal, Queue

from .auth import Account, AuthError, AuthService
from .draft import DraftService


class QueueService:
    def __init__(self, auth: AuthService, drafts: DraftService,
                 now=time.time) -> None:
        self._now = now
        self.auth = auth
        self.drafts = drafts
        self.queue = Queue()
        #: player id -> draft id, for a proposal that turned into a draft.
        self.ready: dict[str, str] = {}
        self.map_pool: list[str] = []
        self.commander_pool: list[str] = []

    def configure(self, map_pool: list[str], commander_pool: list[str]) -> None:
        """Pools come from the operator, not from a client.

        A client-supplied pool would let one side pick the map list it prefers
        before the veto even starts.
        """
        self.map_pool = list(map_pool)
        self.commander_pool = list(commander_pool)

    def join(self, account: Account, rating: float) -> dict:
        account.require("join_queue")
        self.auth.require_trackable(account)
        if not self.map_pool or not self.commander_pool:
            raise AuthError("the operator has not configured the pools yet")
        self.queue.join(account.id, rating, self._now())
        return self.status(account)

    def leave(self, account: Account) -> dict:
        self.queue.leave(account.id)
        self.ready.pop(account.id, None)
        return self.status(account)

    def accept(self, account: Account) -> dict:
        proposal = self.queue.accept(account.id, self._now())
        if proposal is not None:
            self._start_draft(proposal)
        return self.status(account)

    def decline(self, account: Account) -> dict:
        self.queue.decline(account.id, self._now())
        return self.status(account)

    def status(self, account: Account) -> dict:
        now = self._now()
        for proposal in self.queue.tick(now):
            # A proposal both sides had already accepted can only surface here
            # if it completed during this very tick.
            if proposal.ready:
                self._start_draft(proposal)

        draft_id = self.ready.get(account.id)
        entry = self.queue.entries.get(account.id)
        proposal = self.queue._proposal_for(account.id)

        return {
            "in_queue": entry is not None,
            "state": entry.state.value if entry else None,
            "waited_s": round(entry.waited(now)) if entry else 0,
            "queue_size": len(self.queue.searching(now)),
            "proposal": None if proposal is None else {
                "accepted_by_you": account.id in proposal.accepted,
                "accepted_count": len(proposal.accepted),
                "seconds_left": max(
                    0, round(proposal.created_at + ACCEPT_TIMEOUT_S - now)),
            },
            "draft_id": draft_id,
            "penalised_until": (round(entry.penalty_until - now)
                                if entry and entry.penalty_until > now else 0),
        }

    def _start_draft(self, proposal: Proposal) -> None:
        a, b = proposal.players
        if a in self.ready or b in self.ready:
            return                                   # already created
        acc_a = self.auth.accounts.get(a)
        acc_b = self.auth.accounts.get(b)
        if acc_a is None or acc_b is None:
            return
        # A series id derived from the pairing, so a later report can be tied
        # back to the proposal that authorised the match.
        series = f"q-{min(a, b)}-{max(a, b)}-{int(proposal.created_at)}"
        session = self.drafts.create(
            acc_a, self.map_pool, self.commander_pool, series_id=series)
        session.seats[acc_b.id] = session.seats.get(acc_b.id) or _seat_b(
            session, acc_b)
        self.ready[a] = session.id
        self.ready[b] = session.id


def _seat_b(session, account: Account):
    from ladder.draft import Side

    from .draft import Seat, _name
    return Seat(Side.B, account.id, _name(account))
