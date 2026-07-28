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
  * **One queue per mode.** A single shared queue would pair a 1v1 player with
    someone waiting for 2v2 and then hand them a draft neither asked for. The
    modes already exist in `ladder/modes.py`; this keeps them apart.

Only 1v1 modes are offered for now: pairing a team mode needs four or six
players matched as *sides*, not two individuals, and pretending otherwise would
produce a queue that never resolves. It is refused with a reason instead.
"""

from __future__ import annotations

import time

from ladder.matchmaking import ACCEPT_TIMEOUT_S, Proposal, Queue
from ladder.modes import ALL, BY_KEY

from .auth import Account, AuthError, AuthService
from .draft import DraftService


class QueueService:
    def __init__(self, auth: AuthService, drafts: DraftService,
                 now=time.time) -> None:
        self._now = now
        self.auth = auth
        self.drafts = drafts
        #: mode key -> its own queue. Never one shared queue: it would pair
        #: someone waiting for 1v1 with someone waiting for 2v2.
        self.queues: dict[str, Queue] = {}
        #: player id -> draft id, for a proposal that turned into a draft.
        self.ready: dict[str, str] = {}
        #: player id -> the mode they are queued for, so status and leave do not
        #: have to search every queue.
        self.joined: dict[str, str] = {}
        self.map_pool: list[str] = []
        self.commander_pool: list[str] = []

    #: Offered in the queue. Team modes are listed but refused until pairing
    #: whole sides exists, which is a different problem from pairing two people.
    QUEUEABLE = ("ranked_1v1", "unranked_1v1")

    def modes(self) -> list[dict]:
        """What the client offers, with the ones it cannot use marked."""
        out = []
        for m in ALL:
            # Tournament modes are entered through a bracket, not a queue.
            if m.key.startswith("tournament"):
                continue
            out.append({
                "key": m.key,
                "label": m.label,
                "best_of": m.best_of,
                "rated": m.rated,
                "team_size": m.team_size,
                "available": m.key in self.QUEUEABLE,
                "waiting": len(self.queues[m.key].searching(self._now()))
                           if m.key in self.queues else 0,
            })
        return out

    def _queue(self, mode_key: str) -> Queue:
        if mode_key not in self.QUEUEABLE:
            raise AuthError(
                f"{mode_key} cannot be queued yet — team modes need whole sides "
                "paired, not two individuals")
        if mode_key not in self.queues:
            self.queues[mode_key] = Queue()
        return self.queues[mode_key]

    def configure(self, map_pool: list[str], commander_pool: list[str]) -> None:
        """Pools come from the operator, not from a client.

        A client-supplied pool would let one side pick the map list it prefers
        before the veto even starts.
        """
        self.map_pool = list(map_pool)
        self.commander_pool = list(commander_pool)

    def join(self, account: Account, rating: float,
             mode_key: str = "ranked_1v1") -> dict:
        account.require("join_queue")
        self.auth.require_trackable(account)
        if not self.map_pool or not self.commander_pool:
            raise AuthError("the operator has not configured the pools yet")
        q = self._queue(mode_key)
        # One queue at a time. Standing in two and being offered both at once
        # means one offer lapses and earns a penalty for nothing.
        if (prev := self.joined.get(account.id)) and prev != mode_key:
            self.queues[prev].leave(account.id)
        self.joined[account.id] = mode_key
        q.join(account.id, rating, self._now())
        return self.status(account)

    def leave(self, account: Account) -> dict:
        if (mode_key := self.joined.pop(account.id, None)):
            self.queues[mode_key].leave(account.id)
        self.ready.pop(account.id, None)
        return self.status(account)

    def accept(self, account: Account) -> dict:
        q = self._current_queue(account)
        if q is not None:
            proposal = q.accept(account.id, self._now())
            if proposal is not None:
                self._start_draft(proposal, self.joined.get(account.id, "ranked_1v1"))
        return self.status(account)

    def decline(self, account: Account) -> dict:
        q = self._current_queue(account)
        if q is not None:
            q.decline(account.id, self._now())
        return self.status(account)

    def _current_queue(self, account: Account) -> Queue | None:
        key = self.joined.get(account.id)
        return self.queues.get(key) if key else None

    def status(self, account: Account) -> dict:
        now = self._now()
        # Every queue is ticked, not just the caller's: whoever polls keeps the
        # whole thing moving, and a mode nobody is looking at should not stall.
        for key, q in self.queues.items():
            for proposal in q.tick(now):
                if proposal.ready:
                    self._start_draft(proposal, key)

        mode_key = self.joined.get(account.id)
        q = self.queues.get(mode_key) if mode_key else None
        draft_id = self.ready.get(account.id)
        entry = q.entries.get(account.id) if q else None
        proposal = q._proposal_for(account.id) if q else None

        return {
            "mode": mode_key,
            "in_queue": entry is not None,
            "state": entry.state.value if entry else None,
            "waited_s": round(entry.waited(now)) if entry else 0,
            "queue_size": len(q.searching(now)) if q else 0,
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

    def _start_draft(self, proposal: Proposal, mode_key: str) -> None:
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
        mode = BY_KEY.get(mode_key)
        session = self.drafts.create(
            acc_a, self.map_pool, self.commander_pool,
            best_of=mode.best_of if mode else 3, series_id=series)
        session.seats[acc_b.id] = session.seats.get(acc_b.id) or _seat_b(
            session, acc_b)
        self.ready[a] = session.id
        self.ready[b] = session.id


def _seat_b(session, account: Account):
    from ladder.draft import Side

    from .draft import Seat, _name
    return Seat(Side.B, account.id, _name(account))
