"""Queue, pairing and acceptance — from clicking play to the draft.

Pure logic: time arrives as a parameter rather than from a background clock.
Matchmaking without controllable time is untestable, and a bug in it only
shows up once half the scene is stuck in the queue.

Three decisions that matter:

- **The search window widens with waiting time.** With maybe eight people
  queueing on an evening, a fixed window finds nothing. It starts tight and
  opens until anyone may meet anyone — a lopsided game beats no game.
- **The open ladder's weekly cap is honoured at pairing time.** Two people
  who have used up their quota against each other are not put together, or
  you queue ten minutes for a game that will not count.
- **Declining costs time, not reacting costs more.** Otherwise the queue
  fills with people who left long ago.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

from .ratings import tier_of

# (seconds waited, allowed rating gap). After ten minutes the gap stops
# mattering — in a small scene an unfair game beats no game.
SEARCH_WINDOW = ((0, 100), (30, 200), (90, 350), (180, 600), (600, 10_000))

ACCEPT_TIMEOUT_S = 25

#: The first cooldown for wasting somebody's accept window, and what each
#: further one adds.
#:
#: Declining and letting the offer lapse cost the same, because to the player
#: waiting they *are* the same: the match did not happen. Ten minutes for a
#: missed offer was the old rule and it was punishment rather than deterrence —
#: in a scene this size it ends an evening, and it lands hardest on somebody
#: whose game crashed while the offer was on screen. Two minutes stops
#: cherry-picking; the weight belongs on the pattern, so every repeat adds
#: three more.
PENALTY_BASE_S = 120
PENALTY_STEP_S = 180
#: A clean day wipes the record. Without a horizon the counter is a permanent
#: mark for one bad evening months ago.
PENALTY_FORGET_S = 24 * 3600


def allowed_gap(waited_s: float) -> int:
    gap = SEARCH_WINDOW[0][1]
    for after, g in SEARCH_WINDOW:
        if waited_s >= after:
            gap = g
    return gap


class EntryState(str, Enum):
    SEARCHING = "searching"
    PROPOSED = "proposed"
    PLAYING = "playing"
    LEFT = "left"


@dataclass
class Offences:
    """How often somebody has wasted an offer, and when they last did.

    Kept apart from `Entry` on purpose: an entry is created fresh on every join,
    so a counter living there could be reset by leaving the queue and coming
    back — which made the escalation opt-out.
    """
    count: int = 0
    last_at: float = 0.0


@dataclass
class Entry:
    player: str
    rating: float
    joined_at: float
    state: EntryState = EntryState.SEARCHING
    penalty_until: float = 0.0
    declines: int = 0

    def waited(self, now: float) -> float:
        return max(0.0, now - self.joined_at)

    def searchable(self, now: float) -> bool:
        return self.state is EntryState.SEARCHING and now >= self.penalty_until


@dataclass
class Proposal:
    a: str
    b: str
    created_at: float
    best_of: int = 3
    accepted: set[str] = field(default_factory=set)
    declined: set[str] = field(default_factory=set)

    @property
    def players(self) -> tuple[str, str]:
        return (self.a, self.b)

    def expired(self, now: float) -> bool:
        return now - self.created_at > ACCEPT_TIMEOUT_S

    @property
    def ready(self) -> bool:
        return len(self.accepted) == 2


class Queue:
    """Queue for one ladder (open ladder or tournament mode)."""

    def __init__(self, pair_cap: "PairCap | None" = None,
                 offences: "dict[str, Offences] | None" = None) -> None:
        self.entries: dict[str, Entry] = {}
        self.proposals: list[Proposal] = []
        self.pair_cap = pair_cap
        self.log: list[str] = []
        #: Shared ledger, when the caller passes one. There is a queue per mode,
        #: so a per-queue counter would reset by switching from ranked to
        #: unranked and back.
        self.offences: dict[str, Offences] = \
            offences if offences is not None else {}

    def _charge(self, player: str, now: float) -> float:
        """Record an offence and return how long it blocks them.

        One ledger for declining and for not reacting: the difference matters to
        the person who did it and not at all to the person who was waiting.
        """
        rec = self.offences.get(player)
        if rec is None or now - rec.last_at > PENALTY_FORGET_S:
            rec = Offences()
        rec.count += 1
        rec.last_at = now
        self.offences[player] = rec
        return PENALTY_BASE_S + PENALTY_STEP_S * (rec.count - 1)

    # ---------------------------------------------------------- Joining
    def join(self, player: str, rating: float, now: float) -> Entry:
        e = self.entries.get(player)
        if e and e.state in (EntryState.SEARCHING, EntryState.PROPOSED):
            return e                      # joining twice is harmless
        keep_penalty = e.penalty_until if e else 0.0
        declines = e.declines if e else 0
        self.entries[player] = e = Entry(player, rating, now,
                                         penalty_until=keep_penalty,
                                         declines=declines)
        return e

    def leave(self, player: str) -> None:
        if player in self.entries:
            self.entries[player].state = EntryState.LEFT

    def searching(self, now: float) -> list[Entry]:
        return [e for e in self.entries.values() if e.searchable(now)]

    # ---------------------------------------------------------- Pairing
    def _may_pair(self, a: Entry, b: Entry, now: float) -> bool:
        gap = min(allowed_gap(a.waited(now)), allowed_gap(b.waited(now)))
        if abs(a.rating - b.rating) > gap:
            return False
        if self.pair_cap is not None and self.pair_cap.exhausted(a.player, b.player):
            return False
        return True

    def tick(self, now: float) -> list[Proposal]:
        """One pass: clear expired proposals, then pair."""
        self._expire(now)

        # Longest waiting first, or the extremes of the rating range never
        # get matched at all.
        pool = sorted(self.searching(now), key=lambda e: e.joined_at)
        made: list[Proposal] = []
        used: set[str] = set()

        for a, b in itertools.combinations(pool, 2):
            if a.player in used or b.player in used:
                continue
            if not self._may_pair(a, b, now):
                continue
            a.state = b.state = EntryState.PROPOSED
            used.update({a.player, b.player})
            p = Proposal(a.player, b.player, now)
            self.proposals.append(p)
            made.append(p)
            self.log.append(
                f"{now:.0f}: paired {a.player} ({a.rating:.0f}) vs "
                f"{b.player} ({b.rating:.0f}), gap "
                f"{abs(a.rating - b.rating):.0f}")
        return made

    def _expire(self, now: float) -> None:
        for p in list(self.proposals):
            if p.ready or not p.expired(now):
                continue
            for name in p.players:
                e = self.entries.get(name)
                if e is None:
                    continue
                if name in p.accepted:
                    # Whoever accepted must not be punished for the other
                    # side sleeping: back to the queue, no penalty, waiting
                    # time kept.
                    e.state = EntryState.SEARCHING
                else:
                    e.state = EntryState.SEARCHING
                    block = self._charge(name, now)
                    e.penalty_until = now + block
                    self.log.append(f"{now:.0f}: {name} did not react "
                                    f"— {block:.0f}s block")
            self.proposals.remove(p)

    # -------------------------------------------------------- Responding
    def accept(self, player: str, now: float) -> Proposal | None:
        p = self._proposal_for(player)
        if p is None or p.expired(now):
            return None
        p.accepted.add(player)
        if p.ready:
            for name in p.players:
                self.entries[name].state = EntryState.PLAYING
            self.proposals.remove(p)
            self.log.append(f"{now:.0f}: match confirmed {p.a} vs {p.b}")
            return p
        return None

    def decline(self, player: str, now: float) -> None:
        p = self._proposal_for(player)
        if p is None:
            return
        for name in p.players:
            e = self.entries.get(name)
            if e is None:
                continue
            e.state = EntryState.SEARCHING
            if name == player:
                # Repeated declining gets more expensive: once is an
                # accident, three times is cherry-picking.
                e.declines += 1
                block = self._charge(name, now)
                e.penalty_until = now + block
            else:
                # The other side did nothing wrong and keeps its waiting
                # time so the search window does not snap back.
                pass
        self.proposals.remove(p)
        self.log.append(f"{now:.0f}: {player} declined "
                        f"({self.entries[player].penalty_until - now:.0f}s block)")

    def _proposal_for(self, player: str) -> Proposal | None:
        return next((p for p in self.proposals if player in p.players), None)

    # ------------------------------------------------------------ Display
    def status(self, now: float) -> list[dict]:
        out = []
        for e in sorted(self.entries.values(), key=lambda x: x.joined_at):
            if e.state is EntryState.LEFT:
                continue
            out.append({
                "player": e.player,
                "rating": round(e.rating, 1),
                "title": tier_of(e.rating).title,
                "state": e.state.value,
                "waited_s": round(e.waited(now)),
                "window": allowed_gap(e.waited(now)),
                "blocked_for_s": max(0, round(e.penalty_until - now)),
            })
        return out


class PairCap:
    """Weekly cap per pairing — the same rule as in the open ladder.

    Handed to the queue so a pairing that would end up unrated is never
    created in the first place.
    """

    def __init__(self, limit: int, played: dict[tuple[str, str], int] | None = None):
        self.limit = limit
        self.played = played or {}

    @staticmethod
    def key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def note(self, a: str, b: str, games: int) -> None:
        k = self.key(a, b)
        self.played[k] = self.played.get(k, 0) + games

    def remaining(self, a: str, b: str) -> int:
        return max(0, self.limit - self.played.get(self.key(a, b), 0))

    def exhausted(self, a: str, b: str) -> bool:
        return self.remaining(a, b) <= 0
