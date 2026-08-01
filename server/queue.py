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
        #: player id -> when their dodge cooldown runs out. Separate from the
        #: queue's own penalty, which lives on a queue entry and disappears the
        #: moment somebody leaves — exactly what a dodger does.
        self.cooldowns: dict[str, float] = {}
        #: player id -> how many series they have walked out of, so the second
        #: time costs more than the first.
        self.dodges: dict[str, int] = {}
        #: One offence ledger shared by every mode's queue. Per queue it would
        #: reset by switching from ranked to unranked and back, which would make
        #: the escalating cooldown something you can opt out of.
        self.offences: dict = {}
        #: player id -> when they last asked about the queue. The client polls
        #: every second while searching, so this is a heartbeat that costs
        #: nothing extra.
        self.seen: dict[str, float] = {}

    #: How long an entry survives without its client asking about it.
    #:
    #: Closing the client used to leave the account in the queue for good: it
    #: kept being paired, and each pairing burned a whole accept window of
    #: somebody who *was* at the keyboard. Twenty-five seconds is many times
    #: the slowest poll and still gone before the next person notices.
    STALE_AFTER_S = 25.0

    def _sweep(self, now: float) -> list[str]:
        """Drop entries whose client stopped asking.

        Called from `status`, which every client hits constantly — so the
        players still there are the ones who clear out the ones who are not.
        """
        dropped = []
        for pid, mode_key in list(self.joined.items()):
            if now - self.seen.get(pid, 0.0) <= self.STALE_AFTER_S:
                continue
            # Mid-proposal is left alone: the accept window is short, it expires
            # on its own, and the penalty for letting one lapse is the queue's
            # own business rather than something a sweep should pre-empt.
            queue = self.queues.get(mode_key)
            if queue is not None and queue._proposal_for(pid) is not None:
                continue
            if queue is not None:
                queue.leave(pid)
            self.joined.pop(pid, None)
            self.seen.pop(pid, None)
            dropped.append(pid)
        return dropped

    #: A first dodge, doubling per repeat and capped. Long enough to be worth
    #: avoiding, short enough that an accident does not end someone's evening —
    #: and the other player is the one who lost the whole match.
    DODGE_PENALTY_S = 300
    DODGE_PENALTY_MAX_S = 3600

    #: How long an unfinished series may keep somebody out of the queue.
    #:
    #: There has to be a limit. Nothing calls `DraftService.prune`, a client can
    #: be closed mid-series, and a process can be redeployed — so without a
    #: cutoff a draft nobody ever finished would lock a player out permanently,
    #: which is a far worse bug than the dodging this prevents. Three hours is
    #: several times the longest Bo5 anybody plays.
    SERIES_BLOCK_MAX_S = 3 * 3600

    #: How long a draft that never reached a lobby may keep somebody out of the
    #: queue.
    #:
    #: Three hours is right for a series being played and far too long for one
    #: that never started: both clients closing during the picking left a draft
    #: on the server that held two people out of matchmaking for an evening. A
    #: draft that has not produced a lobby in a quarter of an hour is not a
    #: match anybody is in the middle of.
    UNSTARTED_BLOCK_MAX_S = 15 * 60

    def open_series(self, account: Account):
        """A series of theirs that is still live, if any.

        The newest one: an older unsettled draft is the residue of a crash or a
        restart, and it is not what they are sitting in front of.
        """
        now = self._now()
        cutoff = now - self.SERIES_BLOCK_MAX_S
        unstarted = now - self.UNSTARTED_BLOCK_MAX_S
        mine = [s for s in self.drafts.sessions.values()
                if account.id in s.seats and not s.settled and s.full()
                and s.created_at > cutoff
                # A draft that never reached a lobby is not a match in progress.
                and (s.lobby_id is not None or s.created_at > unstarted)]
        return max(mine, key=lambda s: s.created_at) if mine else None

    def note_dodge(self, account: Account) -> float:
        """Charge somebody for walking out of a live series.

        Returns when they may queue again. The penalty is deliberately not tied
        to a queue entry: leaving the queue is the first thing a dodger does.
        """
        self.dodges[account.id] = n = self.dodges.get(account.id, 0) + 1
        seconds = min(self.DODGE_PENALTY_S * 2 ** (n - 1),
                      self.DODGE_PENALTY_MAX_S)
        until = self._now() + seconds
        self.cooldowns[account.id] = max(
            self.cooldowns.get(account.id, 0.0), until)
        return self.cooldowns[account.id]

    def cooldown_left(self, account: Account) -> int:
        return max(0, round(self.cooldowns.get(account.id, 0.0) - self._now()))

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
            # The same ledger for every mode: a cooldown you can shed by
            # switching from ranked to unranked is not a cooldown.
            self.queues[mode_key] = Queue(offences=self.offences)
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
            raise AuthError(
                "this server has no map or commander pool yet, so there is "
                "nothing to draft. An admin sets it from a client that has "
                "Forts installed — the server cannot read the game files.")
        # One match at a time. Somebody drafting against you is waiting for
        # you specifically, and a second match found while the first is unplayed
        # produces two people waiting instead of one.
        if (live := self.open_series(account)) is not None:
            raise AuthError(
                "you are still in a series (" + live.id + "). Play it out and "
                "close it, or agree a void with your opponent, before looking "
                "for another match.")
        if (left := self.cooldown_left(account)):
            raise AuthError(
                f"you left a series early, so the queue is closed to you for "
                f"another {left}s.")
        q = self._queue(mode_key)
        # One queue at a time. Standing in two and being offered both at once
        # means one offer lapses and earns a penalty for nothing.
        if (prev := self.joined.get(account.id)) and prev != mode_key:
            self.queues[prev].leave(account.id)
        self.joined[account.id] = mode_key
        self.seen[account.id] = self._now()
        q.join(account.id, rating, self._now())
        return self.status(account)

    def leave(self, account: Account) -> dict:
        self.seen.pop(account.id, None)
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

    def waiting(self) -> dict[str, int]:
        """Searchers per mode, for callers that do not want a whole status.

        Swept first: an idle client asking for the counts is exactly the moment a
        client that has closed should stop being counted.
        """
        now = self._now()
        self._sweep(now)
        return {key: len(q.searching(now)) for key, q in self.queues.items()}

    def status(self, account: Account) -> dict:
        now = self._now()
        # Asking is the heartbeat. Stamped before the sweep, or a client would
        # be swept by its own poll.
        self.seen[account.id] = now
        self._sweep(now)
        # Every queue is ticked, not just the caller's: whoever polls keeps the
        # whole thing moving, and a mode nobody is looking at should not stall.
        for key, q in self.queues.items():
            for proposal in q.tick(now):
                if proposal.ready:
                    self._start_draft(proposal, key)

        mode_key = self.joined.get(account.id)
        q = self.queues.get(mode_key) if mode_key else None
        # A live series counts as the current draft even after the client left
        # the queue — which it does the moment a draft starts. Without this the
        # way back to your own board disappears on the next poll.
        live = self.open_series(account)
        draft_id = self.ready.get(account.id) or (live.id if live else None)
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
            # Both kinds of block, as one number: the queue's own penalty for
            # a lapsed accept, and the cooldown for walking out of a series.
            "penalised_until": max(
                round(entry.penalty_until - now)
                if entry and entry.penalty_until > now else 0,
                self.cooldown_left(account)),
            # Why the queue is closed, when it is. A cooldown with no reason
            # reads as the client being broken.
            "blocked_by_series": live.id if live else None,
            # How many are searching in each mode, carried on the poll that is
            # already happening. The client used to read this from /queue/modes
            # once at startup and never again, so the picker said "0 waiting" all
            # evening no matter who was in there.
            "waiting": {key: len(q.searching(now))
                        for key, q in self.queues.items()},
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
    """Seat the second player of a queue match.

    This path bypasses `DraftService.join`, which is where a seat's Steam ID is
    normally recorded — so it has to be recorded here too, or the guest has no
    join target when side B ends up hosting.
    """
    from ladder.draft import Side

    from .draft import Seat, _name
    if account.steam_id:
        session._steam_ids[account.id] = account.steam_id
    return Seat(Side.B, account.id, _name(account))
