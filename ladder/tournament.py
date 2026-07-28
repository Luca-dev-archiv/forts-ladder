"""Tournament brackets: plan, seed, advance.

Single elimination with seeding and byes. Pure logic — no clock, no network,
no UI. A tournament here is a state plus rules for how it changes when a
result arrives.

Not included: double elimination, group stages and scheduling. Left out
rather than half-built; a group stage feeding playoffs is its own structure,
not a flag on this one.

Three decisions worth stating:

- **Byes go to the top seeds**, not to whoever signed up first. Distributing
  them randomly wastes the seeding; giving them to the first entries rewards
  being early.
- **Seeding comes from rating.** Standard 1-16, 2-15, ... so favourites meet
  as late as possible.
- **Results are reported, never inferred.** Recorded games can be attached
  via `match_key` so it stays traceable what a result rests on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .modes import Mode, TOURNAMENT_1V1


@dataclass
class Participant:
    """One entrant: a player in 1v1, a team in 2v2."""
    name: str
    rating: float = 1000.0
    members: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.members:
            self.members = [self.name]


@dataclass
class Match:
    id: str
    round_index: int
    slot: int
    a: Participant | None = None
    b: Participant | None = None
    winner: Participant | None = None
    score: tuple[int, int] | None = None
    match_keys: list[str] = field(default_factory=list)
    # Bye: no opponent, the entrant advances without playing.
    bye: bool = False

    @property
    def ready(self) -> bool:
        return self.a is not None and self.b is not None and self.winner is None

    @property
    def done(self) -> bool:
        return self.winner is not None

    def label(self) -> str:
        a = self.a.name if self.a else "—"
        b = self.b.name if self.b else "—"
        if self.bye:
            return f"{a} (bye)"
        s = f"  {self.score[0]}:{self.score[1]}" if self.score else ""
        return f"{a} vs {b}{s}"


def seed_order(size: int) -> list[int]:
    """Standard seeding order for a round of `size` entrants.

    For 8 that is [1, 8, 4, 5, 2, 7, 3, 6] — seeds 1 and 2 can only meet in
    the final. Built recursively because the formula is shorter and less
    error-prone than a lookup table.
    """
    if size < 2:
        return [1]
    half = seed_order(size // 2)
    out: list[int] = []
    for s in half:
        out.append(s)
        out.append(size + 1 - s)
    return out


@dataclass
class Tournament:
    name: str
    participants: list[Participant]
    mode: Mode = TOURNAMENT_1V1
    #: How the entrants are ordered into seeds.
    #:
    #: "rating" is the default and the right answer for a league event. The
    #: other two exist because a host often knows the bracket they want:
    #: "listed" takes the order they were typed in, which makes the pairings
    #: exactly what the list says, and "random" is a draw.
    seeding: str = "rating"
    #: Overrides the mode's series length. A mode says Bo5 because that is what
    #: it usually is, not because a host may never run a Bo3 cup.
    best_of: int | None = None

    rounds: list[list[Match]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if len(self.participants) < 2:
            raise ValueError("a tournament needs at least two entrants")
        self._build()

    # ----------------------------------------------------------- Building
    def series_length(self) -> int:
        """What a match in this tournament is played as."""
        return self.best_of or self.mode.best_of

    def _seeded(self) -> list[Participant]:
        if self.seeding == "listed":
            return list(self.participants)
        if self.seeding == "random":
            # Seeded from the names, so the same entrants always produce the
            # same draw: a bracket that reshuffled every time it was rebuilt
            # from storage would not be the same tournament.
            import hashlib
            return sorted(self.participants, key=lambda p: hashlib.sha256(
                f"{self.name}|{p.name}".encode()).hexdigest())
        return sorted(self.participants, key=lambda p: -p.rating)

    def _build(self) -> None:
        ordered = self._seeded()
        size = 2 ** math.ceil(math.log2(len(ordered)))
        # Seed -> entrant; missing seats stay empty and become byes.
        by_seed: dict[int, Participant | None] = {
            i + 1: (ordered[i] if i < len(ordered) else None)
            for i in range(size)
        }

        order = seed_order(size)
        first: list[Match] = []
        for slot, (s1, s2) in enumerate(zip(order[::2], order[1::2])):
            a, b = by_seed[s1], by_seed[s2]
            m = Match(id=f"R1M{slot + 1}", round_index=0, slot=slot, a=a, b=b)
            if a is not None and b is None:
                # Decided immediately so the next round does not wait for a
                # game that will never happen.
                m.bye = True
                m.winner = a
            elif a is None and b is not None:
                m.bye = True
                m.winner = b
            first.append(m)
        self.rounds = [first]

        n = len(first)
        r = 1
        while n > 1:
            n //= 2
            self.rounds.append([
                Match(id=f"R{r + 1}M{i + 1}", round_index=r, slot=i)
                for i in range(n)
            ])
            r += 1
        self._propagate()

    # ------------------------------------------------------------ Advance
    def _propagate(self) -> None:
        """Push winners forward for as long as anything is decided."""
        changed = True
        while changed:
            changed = False
            for r, matches in enumerate(self.rounds[:-1]):
                for m in matches:
                    if m.winner is None:
                        continue
                    nxt = self.rounds[r + 1][m.slot // 2]
                    target = "a" if m.slot % 2 == 0 else "b"
                    if getattr(nxt, target) is not m.winner:
                        setattr(nxt, target, m.winner)
                        changed = True
                    # The following match can be a bye too, once it is clear
                    # no opponent can arrive.
                    if (nxt.a is not None and nxt.b is None
                            and self._no_opponent_possible(r + 1, nxt.slot, "b")):
                        nxt.bye, nxt.winner = True, nxt.a
                        changed = True
                    if (nxt.b is not None and nxt.a is None
                            and self._no_opponent_possible(r + 1, nxt.slot, "a")):
                        nxt.bye, nxt.winner = True, nxt.b
                        changed = True

    def _no_opponent_possible(self, round_index: int, slot: int, side: str) -> bool:
        """Can anyone still arrive in this slot?"""
        if round_index == 0:
            return True
        feeder_slot = slot * 2 + (0 if side == "a" else 1)
        prev = self.rounds[round_index - 1]
        if feeder_slot >= len(prev):
            return True
        feeder = prev[feeder_slot]
        return feeder.done and feeder.winner is None

    def report(self, match_id: str, winner_name: str,
               score: tuple[int, int] | None = None,
               match_keys: list[str] | None = None) -> Match:
        m = self.match(match_id)
        if m.winner is not None and not m.bye:
            raise ValueError(f"{match_id} is already decided "
                             f"({m.winner.name})")
        if m.a is None or m.b is None:
            raise ValueError(f"{match_id} does not have two entrants yet")
        winner = next((p for p in (m.a, m.b) if p.name == winner_name), None)
        if winner is None:
            raise ValueError(
                f"{winner_name!r} does not play in {match_id} "
                f"({m.a.name} vs {m.b.name})")
        if score is not None:
            needed = self.series_length() // 2 + 1
            if max(score) < needed:
                raise ValueError(
                    f"{score[0]}:{score[1]} does not decide a "
                    f"Bo{self.series_length()} — {needed} wins are needed")
        m.winner = winner
        m.score = score
        m.match_keys = match_keys or []
        self._propagate()
        return m

    def rename(self, seat: int, name: str) -> None:
        """Correct an entrant's name.

        Only before anything has been reported: from the first result on, the
        pairings and the stored results both refer to these names, and changing
        one would silently detach a result from the player who earned it.
        """
        if any(m.winner is not None and not m.bye
               for r in self.rounds for m in r):
            raise ValueError("a result has been reported — names are fixed now")
        name = name.strip()
        if not name:
            raise ValueError("an entrant needs a name")
        if not 0 <= seat < len(self.participants):
            raise KeyError(f"no entrant {seat}")
        if any(p.name == name for i, p in enumerate(self.participants)
               if i != seat):
            raise ValueError(f"{name!r} is already in this tournament")
        old = self.participants[seat]
        old.name = name
        if old.members == [old.members[0]] and len(old.members) == 1:
            old.members = [name]
        self.rounds = []
        self._build()

    # ------------------------------------------------------------- Queries
    def match(self, match_id: str) -> Match:
        for r in self.rounds:
            for m in r:
                if m.id == match_id:
                    return m
        raise KeyError(f"no match {match_id!r}")

    def playable(self) -> list[Match]:
        """What can be played right now — the organiser's worklist."""
        return [m for r in self.rounds for m in r if m.ready and not m.bye]

    @property
    def champion(self) -> Participant | None:
        return self.rounds[-1][0].winner if self.rounds else None

    @property
    def finished(self) -> bool:
        return self.champion is not None

    def round_key(self, index: int) -> str:
        """Language-neutral key; the client translates it.

        The server must not ship finished text, or a German server puts
        German round names into an English client — which is what happened
        on the first run.
        """
        remaining = len(self.rounds) - index
        return {1: "final", 2: "semi", 3: "quarter",
                4: "r16"}.get(remaining, f"r{index + 1}")

    def round_name(self, index: int) -> str:
        """Only for command-line output."""
        return {"final": "Final", "semi": "Semi-final",
                "quarter": "Quarter-final", "r16": "Round of 16"
                }.get(self.round_key(index), f"Round {index + 1}")

    def bracket(self) -> list[dict]:
        return [{
            "round_key": self.round_key(r),
            "round": self.round_name(r),
            "matches": [{
                "id": m.id, "label": m.label(),
                "a_name": m.a.name if m.a else None,
                "b_name": m.b.name if m.b else None,
                "score": list(m.score) if m.score else None,
                "a": m.a.name if m.a else None,
                "b": m.b.name if m.b else None,
                "winner": m.winner.name if m.winner else None,
                "bye": m.bye, "ready": m.ready,
            } for m in matches],
        } for r, matches in enumerate(self.rounds)]

    def summary(self) -> str:
        lines = [f"{self.name} -- {self.mode.describe()}, "
                 f"{len(self.participants)} entrants"]
        for r, matches in enumerate(self.rounds):
            lines.append(f"\n{self.round_name(r)}:")
            for m in matches:
                mark = "  " if m.done else "> " if m.ready else "  "
                won = f"   -> {m.winner.name}" if m.winner else ""
                lines.append(f"  {mark}{m.id}: {m.label()}{won}")
        if self.finished:
            lines.append(f"\nSieger: {self.champion.name}")
        return "\n".join(lines)
