"""Pick and ban for maps and commanders.

Forts has neither. Ranked assigns a map and lets you pick a commander blind;
the community's only commander rule is that one is spent after a win with it.

What makes a draft enforceable here is the game log: it records the map and
both commanders for every game, so `verify()` can hold the plan against what
was actually played. Nobody has to review replays.

Maps run as a veto. Commanders run in two stages: global bans for the whole
series, then a blind simultaneous pick per game — sequential picking would
give whoever chooses second a counter-pick advantage unrelated to skill. The
rule that a winning commander is spent for that side carries over unchanged.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path

from .paths import find_forts_dir


def available_commanders(forts: Path | None = None) -> list[str]:
    """Read commanders from the game install rather than hardcoding them.

    Each one is a mod directory `data/mods/commander-<x>`, so DLC shows up by
    itself and no game data ends up in the repository.
    """
    forts = forts or find_forts_dir()
    if forts is None:
        return []
    mods = forts / "data" / "mods"
    if not mods.is_dir():
        return []
    return sorted(d.name for d in mods.iterdir()
                  if d.is_dir() and re.fullmatch(r"commander-[a-z0-9-]+", d.name))


def short_name(commander: str) -> str:
    """`commander-da-overclocker` -> `overclocker` (internal short form)."""
    return commander.removeprefix("commander-").split("-", 1)[-1]


@lru_cache(maxsize=4)
def commander_names(language: str = "English") -> dict[str, str]:
    """Display names from the game: `commander-da-builder` -> `Architect`.

    The internal ids mean nothing to players — `commander-iba-miser` is
    *Pinchfist* in game. Read from the game's own language files rather than
    typed out here, so DLC, renames and other languages all come for free.
    """
    forts = find_forts_dir()
    if forts is None:
        return {}
    base = forts / "data" / "mods" / f"language-{language}" / "mods"
    out: dict[str, str] = {}
    for strings in base.glob("commander-*/strings.lua"):
        raw = strings.read_bytes()
        text = None
        for enc in ("utf-8-sig", "utf-16-le"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            continue
        m = re.search(r'Name\s*=\s*L"([^"]+)"', text)
        if m:
            out[strings.parent.name] = m.group(1)
    return out


def display_name(commander: str, language: str = "English") -> str:
    """Display name, with a readable fallback for unknown mods."""
    known = commander_names(language).get(commander)
    if known:
        return known
    return short_name(commander).replace("-", " ").title()


class Action(str, Enum):
    BAN_MAP = "ban_map"
    PICK_MAP = "pick_map"
    BAN_COMMANDER = "ban_commander"
    PICK_COMMANDER = "pick_commander"


class Side(str, Enum):
    A = "A"
    B = "B"

    @property
    def other(self) -> "Side":
        return Side.B if self is Side.A else Side.A


@dataclass(frozen=True)
class Step:
    side: Side | None          # None = both at once (blind pick)
    action: Action
    game: int | None = None

    def describe(self) -> str:
        both = self.side is None
        who = "both sides" if both else f"side {self.side.value}"
        what = {
            Action.BAN_MAP: "ban a map",
            Action.PICK_MAP: "pick a map",
            Action.BAN_COMMANDER: "ban a commander",
            Action.PICK_COMMANDER: ("pick a commander blind" if both
                                    else "pick a commander"),
        }[self.action]
        suffix = f" (game {self.game})" if self.game else ""
        return f"{who} {what}{suffix}"


@dataclass
class Choice:
    step_index: int
    side: Side | None
    action: Action
    value: str
    game: int | None = None


def build_steps(best_of: int, commander_bans_per_side: int = 1,
                map_pool_size: int = 5,
                first_ban: Side = Side.A) -> list[Step]:
    """Build the step list for a Bo(n) — symmetric by construction.

        ban    ban   |  pick   pick  |  (ban   ban)  |  remainder
         A      B    |    B      A   |    A     B    |   decider

    Both sides ban equally often and pick equally often. Whoever bans first
    picks second, so the first-ban disadvantage is offset by the last pick.
    The leftover map was chosen by nobody and becomes the decider.

    An earlier version only banned down to the required count, which at six
    maps and Bo3 let one side ban twice. Drawing lots for that advantage does
    not remove it.

    `best_of` is odd, so the pick count is always even; the ban count is only
    even if the pool is odd. `Draft` guarantees that before calling here.
    """
    if best_of < 1 or best_of % 2 == 0:
        raise ValueError("best_of must be odd and at least 1")
    if map_pool_size < best_of:
        raise ValueError(
            f"map pool ({map_pool_size}) smaller than the number of games "
            f"({best_of}) — the last games would have no map")
    bans = map_pool_size - best_of
    if bans % 2:
        raise ValueError(
            f"{map_pool_size} maps and Bo{best_of} give {bans} bans — one "
            "side would have to ban more often. The pool must be reduced to "
            "an odd size first.")

    picks = best_of - 1
    steps: list[Step] = []

    def alternate(count: int, start: Side, action: Action,
                  numbered: bool = False) -> None:
        side = start
        for i in range(count):
            steps.append(Step(side, action, game=i + 1 if numbered else None))
            side = side.other

    # Bans are split around the picks on purpose: being able to ban after
    # seeing the picks makes the late bans matter.
    first_half = (bans // 2) - (bans // 2) % 2 if bans >= 4 else bans
    alternate(first_half, first_ban, Action.BAN_MAP)
    # Whoever banned first picks second.
    alternate(picks, first_ban.other, Action.PICK_MAP, numbered=True)
    alternate(bans - first_half,
              first_ban if first_half % 2 == 0 else first_ban.other,
              Action.BAN_MAP)

    for _ in range(commander_bans_per_side):
        steps.append(Step(Side.A, Action.BAN_COMMANDER))
        steps.append(Step(Side.B, Action.BAN_COMMANDER))

    for game in range(1, best_of + 1):
        # side=None: both lock in at once and are revealed together.
        steps.append(Step(None, Action.PICK_COMMANDER, game=game))
    return steps


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
@dataclass
class Draft:
    map_pool: list[str]
    commander_pool: list[str]
    best_of: int = 3
    commander_bans_per_side: int = 1
    first_ban: Side = Side.A
    strike_seed: int | None = None
    # Seconds per step. None = no limit (post-hoc entry, tests).
    step_seconds: float | None = 30.0

    steps: list[Step] = field(init=False)
    choices: list[Choice] = field(default_factory=list, init=False)
    # Blind picks are held until both sides have locked in.
    _pending_blind: dict[Side, str] = field(default_factory=dict, init=False)
    _step_started: float | None = field(default=None, init=False)

    #: Clock as a field so tests can substitute their own instead of
    #: letting real seconds pass.
    _now = staticmethod(time.monotonic)

    def __post_init__(self) -> None:
        self.map_pool = list(dict.fromkeys(self.map_pool))
        self.commander_pool = list(dict.fromkeys(self.commander_pool))

        # An odd pool is what makes the ban count split evenly. Striking one
        # map by lot is fair to both sides; letting one side ban twice is not.
        # The seed is reproducible so the draw can be checked afterwards.
        # Validate the format first, or a Bo2 fails inside the draw and the
        # caller gets a random-number error instead of a clear message.
        if self.best_of < 1 or self.best_of % 2 == 0:
            raise ValueError("best_of must be odd and at least 1")

        self.neutral_strike: str | None = None
        if (len(self.map_pool) - self.best_of) % 2:
            # Seed as a string (Random rejects tuples). Without an explicit
            # seed the pool itself decides, so the same pool always strikes
            # the same map.
            seed = (str(self.strike_seed) if self.strike_seed is not None
                    else "|".join(self.map_pool))
            self.neutral_strike = random.Random(seed).choice(self.map_pool)
            self.map_pool = [m for m in self.map_pool if m != self.neutral_strike]

        self.steps = build_steps(self.best_of, self.commander_bans_per_side,
                                 len(self.map_pool), self.first_ban)

    # ------------------------------------------------------------- Queries
    @property
    def step_index(self) -> int:
        return self._computed_index()

    def _computed_index(self) -> int:
        """First step that is not fully done yet.

        Derived from the choices rather than counted: a simultaneous pick is
        only finished once both sides have locked in, and a counter could not
        represent that in-between state.
        """
        idx = 0
        for step in self.steps:
            if step.action == Action.PICK_COMMANDER and step.side is None:
                got = sum(1 for c in self.choices
                          if c.action == Action.PICK_COMMANDER
                          and c.game == step.game)
                if got < 2:
                    return idx
            else:
                if not any(c.step_index == idx for c in self.choices):
                    return idx
            idx += 1
        return len(self.steps)

    @property
    def done(self) -> bool:
        return self._computed_index() >= len(self.steps)

    @property
    def current(self) -> Step | None:
        i = self._computed_index()
        return self.steps[i] if i < len(self.steps) else None

    def ban_counts(self) -> dict[Side, int]:
        """Map bans per side — shown in the UI as a fairness check."""
        out = {Side.A: 0, Side.B: 0}
        for st in self.steps:
            if st.action == Action.BAN_MAP and st.side is not None:
                out[st.side] += 1
        return out

    def banned_maps(self) -> list[str]:
        return [c.value for c in self.choices if c.action == Action.BAN_MAP]

    def picked_maps(self) -> dict[int, str]:
        return {c.game: c.value for c in self.choices
                if c.action == Action.PICK_MAP and c.game}

    def remaining_maps(self) -> list[str]:
        gone = set(self.banned_maps()) | set(self.picked_maps().values())
        return [m for m in self.map_pool if m not in gone]

    def pick_counts(self) -> dict[Side, int]:
        out = {Side.A: 0, Side.B: 0}
        for st in self.steps:
            if st.action == Action.PICK_MAP and st.side is not None:
                out[st.side] += 1
        return out

    def is_symmetric(self) -> bool:
        """Does each side ban and pick equally often? Must always be true."""
        b, p = self.ban_counts(), self.pick_counts()
        return b[Side.A] == b[Side.B] and p[Side.A] == p[Side.B]

    def banned_commanders(self) -> list[str]:
        return [c.value for c in self.choices if c.action == Action.BAN_COMMANDER]

    def burned(self, side: Side) -> set[str]:
        """Commanders this side has already won with — spent for them.

        Filled by `note_result`; nothing is spent before the series starts.
        """
        return self._burned.setdefault(side, set())

    _burned: dict[Side, set[str]] = field(default_factory=dict, init=False)

    def available_commanders_for(self, side: Side) -> list[str]:
        gone = set(self.banned_commanders()) | self.burned(side)
        return [c for c in self.commander_pool if c not in gone]

    def legal_options(self, side: Side | None = None) -> list[str]:
        step = self.current
        if step is None:
            return []
        if step.action in (Action.BAN_MAP, Action.PICK_MAP):
            return self.remaining_maps()
        if step.action == Action.BAN_COMMANDER:
            return [c for c in self.commander_pool
                    if c not in self.banned_commanders()]
        if step.action == Action.PICK_COMMANDER:
            if side is None:
                return []
            if side in self._pending_blind:
                return []          # this side already locked in
            return self.available_commanders_for(side)
        return []

    # ------------------------------------------------------------- Actions
    def apply(self, value: str, side: Side | None = None) -> Choice:
        step = self.current
        if step is None:
            raise ValueError("draft is finished")

        if step.side is not None:
            if side is not None and side is not step.side:
                raise ValueError(
                    f"side {side.value} is not to move — "
                    f"{step.describe()}")
            side = step.side
        elif side is None:
            raise ValueError("simultaneous picks need an explicit side")

        options = self.legal_options(side)
        if value not in options:
            raise ValueError(
                f"{value!r} cannot be chosen here. Available: "
                + (", ".join(options) if options else "nothing"))

        if step.action == Action.PICK_COMMANDER:
            # Blind: collect until both sides are in, then reveal together.
            self._pending_blind[side] = value
            if len(self._pending_blind) == 2:
                idx = self._computed_index()
                for s, v in self._pending_blind.items():
                    self.choices.append(Choice(idx, s, step.action, v, step.game))
                self._pending_blind.clear()
                self._step_started = self._now()
            return Choice(self._computed_index(), side, step.action, value,
                          step.game)

        c = Choice(self._computed_index(), side, step.action, value, step.game)
        self.choices.append(c)
        # Manual move: the next deadline starts now.
        self._step_started = self._now()
        return c

    # -------------------------------------------------------------- Timing
    def deadline(self) -> float | None:
        """When the current step must be done at the latest."""
        if self.step_seconds is None or self.current is None:
            return None
        if self._step_started is None:
            self._step_started = self._now()
        return self._step_started + self.step_seconds

    def seconds_left(self, now: float | None = None) -> float | None:
        d = self.deadline()
        return None if d is None else max(0.0, d - (now if now is not None
                                                    else self._now()))

    def tick(self, now: float | None = None) -> list[str]:
        """Resolve expired steps automatically. Returns log messages.

        Without this any draft eventually hangs: one player walking away
        blocks the whole series and nobody can finish it.

        Expired steps are drawn by lot rather than skipped — a skipped ban
        would break the symmetry and a skipped pick would leave a game
        without a map. The draw is reproducible so nobody can claim the tool
        handed them the worst commander on purpose.
        """
        now = now if now is not None else self._now()
        messages: list[str] = []
        while not self.done:
            d = self.deadline()
            if d is None or now < d:
                break
            step = self.current
            assert step is not None
            rng = random.Random(f"auto|{len(self.choices)}|{step.action.value}")
            if step.side is None:
                # Simultaneous pick: only draw for sides that have not locked
                # in yet — a choice made in time stays valid.
                for side in (Side.A, Side.B):
                    if side in self._pending_blind:
                        continue
                    options = self.legal_options(side)
                    if not options:
                        break
                    pick = rng.choice(options)
                    self.apply(pick, side)
                    messages.append(
                        f"time up — side {side.value} was drawn "
                        f"{display_name(pick)}")
            else:
                options = self.legal_options(step.side)
                if not options:
                    break
                pick = rng.choice(options)
                self.apply(pick, step.side)
                what = ("map" if step.action in (Action.BAN_MAP, Action.PICK_MAP)
                        else "commander")
                messages.append(
                    f"time up — {what} drawn for side {step.side.value}: "
                    + (pick if what == "map" else display_name(pick)))
            # The next deadline starts at the expired one, not at "now" —
            # otherwise every following step is granted its full time again
            # and a draft left idle for an hour needs another hour to catch
            # up.
            self._step_started = d
        return messages

    def note_result(self, game: int, winner: Side) -> None:
        """Report a game result, spending the winner's commander."""
        for c in self.choices:
            if c.action == Action.PICK_COMMANDER and c.game == game \
                    and c.side is winner:
                self.burned(winner).add(c.value)

    # ------------------------------------------------------------- Outcome
    def plan(self) -> list[dict]:
        """What is to be played: map and both commanders per game."""
        chosen = self.picked_maps()
        # Whatever survives all bans and picks is the decider — chosen by
        # neither side.
        leftover = self.remaining_maps()
        out = []
        for game in range(1, self.best_of + 1):
            picks = {c.side: c.value for c in self.choices
                     if c.action == Action.PICK_COMMANDER and c.game == game}
            is_decider = game == self.best_of and self.best_of > 1
            if is_decider:
                map_name = leftover[0] if len(leftover) == 1 else None
            else:
                map_name = chosen.get(game)
                if map_name is None and self.best_of == 1:
                    map_name = leftover[0] if len(leftover) == 1 else None
            picked_by = next((c.side for c in self.choices
                              if c.action == Action.PICK_MAP and c.game == game),
                             None)
            out.append({
                "game": game,
                "map": map_name,
                "map_picked_by": picked_by.value if picked_by else None,
                "commander_a": picks.get(Side.A),
                "commander_b": picks.get(Side.B),
                "decider": is_decider,
            })
        return out

    def verify(self, matches: list[dict],
               side_of: dict[Side, int] | None = None) -> list[str]:
        """Was the draft actually played? Returns the deviations.

        `matches` are recorder entries in game order; `side_of` maps draft
        side to game side (default A=1, B=2).

        This is what turns the draft from an agreement into a rule: the log
        holds map and commanders, so a deviation is provable rather than
        arguable.
        """
        side_of = side_of or {Side.A: 1, Side.B: 2}
        problems: list[str] = []
        plan = self.plan()

        for i, m in enumerate(matches):
            if i >= len(plan):
                problems.append(f"game {i + 1}: more games played than "
                                f"drafted ({len(plan)})")
                break
            want = plan[i]
            got_map = m.get("map")
            if want["map"] and got_map and got_map != want["map"]:
                problems.append(
                    f"game {i + 1}: played {got_map}, drafted "
                    f"{want['map']}")
            commanders = m.get("commanders") or {}
            for side, key in ((Side.A, "commander_a"), (Side.B, "commander_b")):
                expected = want[key]
                if not expected:
                    continue
                got = commanders.get(f"side{side_of[side]}")
                if got and got != expected:
                    problems.append(
                        f"game {i + 1}: side {side.value} played "
                        f"{display_name(got)}, drafted "
                        f"{display_name(expected)}")
        return problems

    def summary(self) -> str:
        lines = [f"Bo{self.best_of}"]
        if self.neutral_strike:
            lines.append(f"struck neutrally by lot: {self.neutral_strike}")
        if self.banned_maps():
            lines.append("maps banned: " + ", ".join(self.banned_maps()))
        if self.banned_commanders():
            lines.append("commanders banned: "
                         + ", ".join(display_name(c) for c in self.banned_commanders()))
        for g in self.plan():
            a = display_name(g["commander_a"]) if g["commander_a"] else "?"
            b = display_name(g["commander_b"]) if g["commander_b"] else "?"
            tag = ("  (decider)" if g["decider"]
                   else f"  (picked by {g['map_picked_by']})"
                   if g["map_picked_by"] else "")
            lines.append(f"  game {g['game']}: {g['map']}  —  {a} vs {b}{tag}")
        return "\n".join(lines)
