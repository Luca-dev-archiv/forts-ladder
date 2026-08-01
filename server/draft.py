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
    #: People per side. 1 for a duel, 2 for a 2v2.
    #:
    #: The sides stay A and B however many people are in them — the draft, the
    #: bans, the commanders and the reveal are all per side, and none of them
    #: cares how many players a side is. What changes is when the draft may
    #: start, which is when every seat is taken rather than when there are two.
    team_size: int = 1
    #: The pool as it was handed in. `Draft` removes the neutrally struck map
    #: from its own copy, so saving that one and rebuilding from it would strike
    #: a second map and change the board under the players.
    original_map_pool: list[str] = field(default_factory=list)
    #: Steam lobby the two of them agreed to play in, read out of the host's
    #: game log. This is the whole handoff: without it a finished draft is a
    #: list of maps and no way to get into a game.
    lobby_id: int | None = None
    #: Which side is hosting the lobby. The other side gets the join link.
    #:
    #: Claimed *before* the lobby exists, not derived from who reported it
    #: first: both clients used to show "I am hosting" until one pressed it,
    #: which is two people about to host the same match.
    lobby_host: str | None = None
    #: SteamID64 of the host, so the other side can be sent straight into their
    #: lobby. Steam's join URL wants the owner's account; a zero there makes it
    #: guess, and it guessed wrong.
    lobby_host_steam: str | None = None
    #: The lobby password the host's client generated.
    #:
    #: Steam's join URL has no field for it and the game asks for it on entry, so
    #: without passing it here the guest was sent to a prompt for something only
    #: the host knew. Handed to the two seats and to nobody else — it keeps
    #: strangers out of the lobby, which is the only thing it is for.
    lobby_password: str | None = None
    #: Set when someone walked away. Kept rather than deleted, so the other
    #: side is told what happened instead of getting "unknown draft".
    cancelled_by: str | None = None

    #: Set when the people in the lobby were not the people who drafted.
    #:
    #: The match is over at that point — not voided by agreement, aborted on a
    #: fact. Which side it was is named, because the other one did nothing wrong
    #: and a shared "aborted" would read as a shared fault.
    aborted_side: str | None = None
    aborted_reason: str | None = None

    @property
    def aborted(self) -> bool:
        return self.aborted_side is not None

    #: Open void requests, side -> (scope, reason). A void needs *both* sides,
    #: because "that game did not count" is exactly the claim a losing player
    #: has an interest in making alone.
    void_requests: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Games both sides agreed not to count. They are played again under the
    #: same number.
    voided_games: set[int] = field(default_factory=set)
    #: Set when both sides agreed to throw the whole series away.
    voided: bool = False
    #: When the draft finished, when the lobby appeared, and when the guest
    #: reported being in it. The three stamps the handoff clock is made of.
    done_at: float | None = None
    lobby_at: float | None = None
    guest_ready_at: float | None = None
    #: Extra time both sides agreed to. A game that will not start is usually a
    #: port or a Steam problem, and the answer to that is more time.
    extra_seconds: float = 0.0
    #: Side that has asked for more time and is waiting to be granted it.
    extension_asked_by: str | None = None
    #: The host wrote lobby settings into a running Forts, which only reads them
    #: at start — so the password the guest is waiting for does not exist yet.
    host_restart_pending: bool = False
    #: What was played differently from what was drafted, per game.
    #:
    #: Kept rather than only refused: both players need to see *why* a game is
    #: being replayed, and a referee looking at the series later needs it more.
    deviations: dict[int, list[str]] = field(default_factory=dict)
    #: Set when a decided series was closed out. Until then it counts as open,
    #: and an open series is what keeps both players out of the queue — a match
    #: that is still being played is not a match you may leave for another.
    concluded: bool = False

    @property
    def cancelled(self) -> bool:
        return self.cancelled_by is not None

    @property
    def settled(self) -> bool:
        """Nothing more will happen here.

        The one question the queue needs answered: may these two play something
        else? Everything that ends a series says yes — decided and closed out,
        walked away from, aborted on a fact, or voided by agreement.
        """
        return (self.concluded or self.cancelled or self.aborted
                or self.voided)

    #: Wall clock, deliberately not the engine's monotonic one.
    #:
    #: The engine measures step deadlines, which only matter while the process
    #: lives, so monotonic is right there. These stamps are written to the
    #: database and read back after a restart, where a monotonic value means
    #: nothing at all.
    _now = staticmethod(time.time)

    #: How long each half of the handoff gets, and what an agreed extension
    #: adds. Three minutes is enough to open a lobby and click a link, and short
    #: enough that nobody spends their evening waiting on somebody who left.
    HOST_WINDOW_S = 180.0
    JOIN_WINDOW_S = 180.0
    EXTENSION_S = 120.0

    def handoff(self) -> dict:
        """Which half of the handoff is running, and how long is left.

        One clock, on the server. Two clients counting their own would disagree
        about when it ran out, which is the one thing a deadline may not do.
        """
        if not self.draft.done or self.settled or self.done_at is None:
            return {"phase": "none", "on": None, "seconds_left": None,
                    "expired": False, "deadline_s": None}

        host = self.assigned_host()
        if self.lobby_at is None:
            phase, on, started, window = "host", host, self.done_at, \
                self.HOST_WINDOW_S
        elif self.guest_ready_at is None:
            guest = "B" if host == "A" else "A"
            phase, on, started, window = "guest", guest, self.lobby_at, \
                self.JOIN_WINDOW_S
        else:
            return {"phase": "playing", "on": None, "seconds_left": None,
                    "expired": False, "deadline_s": None}

        window += self.extra_seconds
        left = started + window - self._now()
        return {"phase": phase, "on": on,
                "seconds_left": max(0, round(left)),
                "expired": left <= 0, "deadline_s": round(window)}

    def late_side(self) -> str | None:
        """Whose deadline has run out, if anybody's."""
        h = self.handoff()
        return h["on"] if h["expired"] else None

    def ask_extension(self, account: Account) -> dict:
        """Ask the other side for two more minutes.

        Asked of the opponent rather than taken, because the time comes out of
        their evening. Whoever is waiting can grant it with one click; whoever is
        late cannot grant it to themselves.
        """
        seat = self.seat_of(account)
        h = self.handoff()
        if h["phase"] in ("none", "playing"):
            raise AuthError("there is nothing waiting on a clock right now")
        self.extension_asked_by = seat.side.value
        return self.public_state(account)

    def grant_extension(self, account: Account) -> dict:
        """Agree to the extra time. Only the *other* side may."""
        seat = self.seat_of(account)
        if self.extension_asked_by is None:
            raise AuthError("nobody asked for more time")
        if self.extension_asked_by == seat.side.value:
            raise AuthError("the other side has to agree to this")
        self.extra_seconds += self.EXTENSION_S
        self.extension_asked_by = None
        return self.public_state(account)

    def note_ready(self, account: Account) -> dict:
        """The guest is in the lobby. Stops the join clock.

        Reported by the client that got in, because it is the only one that
        knows: the host sees a player connect but not which ladder account it is.
        """
        self.seat_of(account)
        if self.lobby_at is not None and self.guest_ready_at is None:
            self.guest_ready_at = self._now()
        return self.public_state(account)

    def can_conclude(self) -> bool:
        """Whether the series is finished but not yet closed out.

        Decided, not merely played: a Bo3 ends at two wins, and the third game
        is not played at all. Waiting for `best_of` results would leave every
        2:0 series open for ever.
        """
        return not self.settled and self.full() and self.series_over()

    def conclude(self, account: Account) -> dict:
        """Close out a decided series.

        Either side may, because both are equally stuck until somebody does, and
        there is nothing left to disagree about — the result is already in.
        """
        self.seat_of(account)
        if self.settled:
            return self.public_state(account)
        if not self.series_over():
            need = self.draft.best_of // 2 + 1
            have = max(self.wins().values())
            raise AuthError(
                f"this series is not decided yet — {have} of {need} games won. "
                "Report the games you have played, or agree a void.")
        self.concluded = True
        return self.public_state(account)

    def is_dodge(self, account: Account) -> bool:
        """Whether leaving right now abandons a match.

        True only for a queue match that is still live: the other player was
        paired with you by the server, has drafted against you, and gets nothing
        out of the evening if you walk off. A draft made with a join code is
        excluded — two people who arranged a match between themselves may also
        call it off between themselves.
        """
        if account.id not in self.seats or self.series_id is None:
            return False
        if self.settled or not self.full():
            return False
        # Whoever was kept waiting leaves for free. Charging them for the other
        # side's silence would be exactly backwards, and it is the reason the
        # handoff needed a clock in the first place.
        late = self.late_side()
        if late is not None and late != self.seats[account.id].side.value:
            return False
        return True

    # ------------------------------------------------------------------ Seats
    def seat_of(self, account: Account) -> Seat:
        seat = self.seats.get(account.id)
        if seat is None:
            raise AuthError("you are not in this draft")
        return seat

    def full(self) -> bool:
        """Everybody is here. Not "there are two" — that was the 1v1 assumption
        that made a 2v2 draft impossible to express."""
        return len(self.seats) >= 2 * self.team_size

    def free_side(self) -> Side | None:
        """A side with room, A before B.

        Filling A first is deliberate: with a join code the host's own side is
        the one people arrive for, and asking each of them which team they meant
        is a question with an obvious answer three times out of four.
        """
        for side in (Side.A, Side.B):
            if sum(1 for s in self.seats.values() if s.side is side) \
                    < self.team_size:
                return side
        return None

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
            # Who is *meant* to host, which is a decision and not a race.
            "lobby_host": self.assigned_host(),
            "lobby_host_steam": self.lobby_host_steam or self._host_steam(),
            # Only the two of them. A spectator gets in through the host, and a
            # password handed to anyone who asks for the state protects nothing.
            "lobby_password": self.lobby_password if seat else None,
            "seats": self._seat_names(seat),
            "full": self.full(),
            "team_size": self.team_size,
            # When the lobby was opened, as a unix time. A game of this series
            # cannot have been played before it existed, and a client that is not
            # hosting has nothing else to tell this series' games apart from the
            # ones the same two people played an hour earlier.
            "lobby_at": self.lobby_at,
            #: How many seats are still open, so the setup strip can say "waiting
            #: for two more" instead of just "waiting".
            "seats_open": max(0, 2 * self.team_size - len(self.seats)),
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
            "plan": self._plan_for(seat),
            # How far the series has got, so a client can say "game 2 of 3" and
            # explain why later games are still blank.
            "revealed_through": d.revealed_through(),
            "games_played": sorted(d.played_games()),
            "wins": self.wins(),
            "series_over": self.series_over(),
            "voided": self.voided,
            "voided_games": sorted(self.voided_games),
            # Why a game was thrown out, for both sides and for whoever reviews
            # it afterwards.
            "deviations": {str(g): list(v)
                           for g, v in sorted(self.deviations.items())},
            # Whether this series is over, and whether it is this viewer's turn
            # to say so. Without it the client cannot tell a series that is
            # waiting for a game from one that is waiting for a click.
            # The handoff clock, so both clients count down the same number.
            "handoff": self.handoff(),
            "extension_asked_by": self.extension_asked_by,
            # The host wrote settings into a running Forts, which only reads them
            # at start. Carried so the *guest* learns why no password arrived —
            # they cannot see the other machine.
            "host_restart_pending": self.host_restart_pending,
            "concluded": self.concluded,
            "can_conclude": self.can_conclude(),
            "settled": self.settled,
            # Whether walking away from here costs a cooldown, so the client can
            # say so *before* the button is pressed rather than after.
            "leaving_penalised": (self.series_id is not None
                                  and not self.settled and self.full()),
            "aborted": self.aborted,
            "aborted_side": self.aborted_side,
            "aborted_reason": self.aborted_reason,
            # Who has asked for what, so a client can say "your opponent wants
            # to void game 2 — crash" and offer to agree.
            "void_requests": {side: {"scope": scope, "reason": reason}
                              for side, (scope, reason)
                              in self.void_requests.items()},
        }

    def _host_steam(self) -> str | None:
        """The assigned host's Steam ID, so the guest has a join target as soon
        as the lobby appears rather than one poll later."""
        side = self.assigned_host()
        if side is None:
            return None
        seat = next((s for s in self.seats.values() if s.side.value == side), None)
        return self._steam_ids.get(seat.account_id) if seat else None

    #: account_id -> SteamID64, filled when a seat is taken. The session does not
    #: hold Account objects, and the guest needs the host's id to join.
    _steam_ids: dict[str, str] = field(default_factory=dict)

    def wins(self) -> dict[str, int]:
        """Games won per side, from the results reported so far.

        Voided games are not in `_results` at all, so they cannot count here
        either — which is the whole point of voiding one.
        """
        out = {"A": 0, "B": 0}
        for side in self.draft._results.values():
            out[side.value] += 1
        return out

    def series_over(self) -> bool:
        """Has somebody taken it? A Bo3 ends at two, not after three games."""
        needed = self.draft.best_of // 2 + 1
        return max(self.wins().values()) >= needed

    def _seat_names(self, seat: Seat | None) -> dict[str, str]:
        """Who is on which side — with the opponents anonymous while picking.

        Knowing who you are against changes how you ban, and a queue match is
        supposed to be decided by the board rather than by the name. Once the
        picking is over it is pointless to hide: you are about to play them.

        Not applied to a draft somebody hosted with a join code — they invited
        specific people and already know who they are.

        One string per side rather than per seat, so a 2v2 reads "Ada, Grace" in
        the place a duel reads "Ada". Everything downstream shows a side.
        """
        names: dict[str, str] = {}
        for side in ("A", "B"):
            people = [s.display for s in self.seats.values()
                      if s.side.value == side]
            if people:
                names[side] = ", ".join(people)
        if self.draft.done or self.series_id is None or seat is None:
            return names
        return {side: (name if side == seat.side.value
                       else ("Opponents" if self.team_size > 1 else "Opponent"))
                for side, name in names.items()}

    def _plan_for(self, seat: Seat | None) -> list[dict]:
        """The plan, with the opponent's later commanders withheld.

        A blind pick decides every game of the series at once. Revealing all of
        them when the draft ends hands over the opponent's game 2 and game 3
        before game 1 is played, which is worse than having no blind pick: it
        turns one hidden choice into three known ones. So the opponent's
        commander appears one game at a time, as results come in.

        Your own picks are always shown — you chose them — and the maps are
        public throughout, because the map veto is open by design.
        """
        plan = self.draft.plan()
        if seat is None:
            # A spectator sees only what has actually been played.
            through = self.draft.revealed_through() - 1
            return [self._hide(g, None) if g["game"] > through else g
                    for g in plan]
        # Strictly what has been played. Game 1 used to open the moment the
        # draft ended — the very game about to be played, which is the one the
        # blind pick is for. You learn the opponent's commander in the game.
        played = self.draft.revealed_through() - 1
        return [g if g["game"] <= played else self._hide(g, seat.side)
                for g in plan]

    @staticmethod
    def _hide(game: dict, keep: Side | None) -> dict:
        """One planned game with the other side's commander removed.

        A copy, not a mutation: the same `plan()` output is handed to both sides
        in turn, and blanking in place would leak to whoever is served second.
        """
        out = dict(game)
        if keep is not Side.A:
            out["commander_a"] = None
        if keep is not Side.B:
            out["commander_b"] = None
        return out

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

    def assigned_host(self) -> str | None:
        """Whose job hosting is, decided rather than offered.

        Side A, once the draft is finished. First-come-first-served left both
        clients showing "I am hosting" until somebody pressed it, which is two
        people about to open the same match — and the person who pressed second
        got an error for doing what the screen invited.

        The other side may still take it over (`claim_host`) for the case that
        the assigned host cannot host.
        """
        if not self.draft.done or self.cancelled:
            return None
        return self.lobby_host or Side.A.value

    def claim_host(self, account: Account) -> dict:
        """Take over hosting from whoever it was assigned to.

        Normally nobody needs this: side A hosts. It exists because the assigned
        host sometimes cannot — no port forwarding, a bad connection — and then
        the series should not be stuck.
        """
        seat = self.seat_of(account)
        if self.cancelled:
            raise AuthError("this draft was cancelled")
        if not self.draft.done:
            raise AuthError("the draft is not finished yet")
        if self.lobby_id is not None and self.lobby_host != seat.side.value:
            # Once a lobby is open, taking over would send the other side to a
            # lobby that no longer matters.
            raise AuthError(f"side {self.lobby_host} already opened a lobby")
        self.lobby_host = seat.side.value
        self.lobby_host_steam = account.steam_id
        return self.public_state(account)

    #: What a void may be asked for. "series" throws the whole thing away;
    #: "game:N" drops one game so it can be played again.
    def _parse_scope(self, scope: str) -> tuple[str, int | None]:
        scope = (scope or "").strip().lower()
        if scope == "series":
            return "series", None
        if scope.startswith("game:"):
            try:
                n = int(scope.split(":", 1)[1])
            except ValueError as e:
                raise AuthError(f"{scope!r} is not a scope") from e
            if not 1 <= n <= self.draft.best_of:
                raise AuthError(f"game {n} is not in a Bo{self.draft.best_of}")
            return "game", n
        raise AuthError("a void is either 'series' or 'game:N'")

    def request_void(self, account: Account, scope: str,
                     reason: str = "") -> dict:
        """Ask for a game or the series not to count.

        Needs both sides. A crash, the wrong commander loaded, the wrong map —
        these happen, and the alternative to a mutual void is a rated result
        that both players know is wrong. What it must never be is one-sided:
        "that game did not count" is precisely the claim a losing player has an
        interest in making alone.

        Asking twice for the same thing is not an error, and asking for
        something different replaces your earlier request — you get one vote,
        not a collection.
        """
        seat = self.seat_of(account)
        kind, game = self._parse_scope(scope)
        if self.voided:
            raise AuthError("this series was already voided")
        if kind == "game" and game in self.voided_games:
            return self.public_state(account)

        self.void_requests[seat.side.value] = (scope.strip().lower(),
                                               (reason or "").strip()[:120])
        wanted = {s for s, (sc, _) in self.void_requests.items()
                  if sc == scope.strip().lower()}
        if {"A", "B"} <= wanted:
            # Both agreed: apply it and clear the votes.
            if kind == "series":
                self.voided = True
            else:
                self.voided_games.add(int(game))
                # Drop its result so the game is genuinely replayable, and give
                # the winner their commander back.
                self.draft._results.pop(int(game), None)
                self.draft._burned.clear()
                for g, side in self.draft._results.items():
                    self.draft.note_result(g, side)
            self.void_requests.clear()
        return self.public_state(account)

    def withdraw_void(self, account: Account) -> dict:
        """Take your vote back before the other side agrees."""
        seat = self.seat_of(account)
        self.void_requests.pop(seat.side.value, None)
        return self.public_state(account)

    def check_roster(self, steam_ids: list[str]) -> list[str]:
        """Which drafted sides did not actually play.

        The draft binds two accounts, each with a proven Steam ID. What must not
        happen is somebody else playing in one of those places: a friend at the
        keyboard, a second account, or simply the wrong lobby. None of that can
        be rated as if the drafted pair had played.

        The test is **absence, not surplus**. Each drafted id has to be in the
        roster; an extra id proves nothing, because a spectator connects as a
        client and the game log lists them like everybody else. The first version
        of this check treated any unknown id as fraud, which would have made
        admitting a caster abort the series — two of this project's own features
        contradicting each other.

        Returns the sides whose player is missing. Empty means both played.
        """
        present = set(steam_ids)
        missing = []
        for seat in self.seats.values():
            expected = self._steam_ids.get(seat.account_id)
            if not expected:
                # Nothing to compare against. An account with no linked Steam ID
                # cannot be checked, and refusing on that basis would punish the
                # wrong thing.
                continue
            if expected not in present:
                missing.append(seat.side.value)
        return sorted(missing)

    def abort(self, side: str, reason: str) -> None:
        """End the series on a fact rather than on an agreement."""
        if self.aborted:
            return
        self.aborted_side = side
        self.aborted_reason = reason[:200]

    def side_of_steam(self, steam_id: str) -> str | None:
        """Which drafted side a Steam ID belongs to.

        The log numbers sides 1 and 2 per game and Forts swaps them between
        games, so a side number says nothing across a series. A Steam ID is the
        same person in every game of it, and the draft already knows which seat
        each one took.
        """
        for seat in self.seats.values():
            if self._steam_ids.get(seat.account_id) == steam_id:
                return seat.side.value
        return None

    def check_against_plan(self, game: int, map_played: str | None,
                           commanders: dict[str, str] | None) -> list[str]:
        """What was played that was not what was drafted.

        Checked here rather than on a client for two reasons. A client only knows
        its *own* commander until the game is over — the opponent's is withheld,
        which is the point of a blind pick — so no client can check both sides.
        And a verdict that lives on one machine is a verdict the other machine
        cannot see.

        `commanders` is keyed by Steam ID; the seats say which side each is.
        """
        plan = self.draft.plan()
        if not 1 <= game <= len(plan):
            return []
        want = plan[game - 1]
        out: list[str] = []

        if map_played and want.get("map") and \
                map_played.strip().casefold() != want["map"].strip().casefold():
            out.append(f"map: drafted {want['map']}, played {map_played}")

        for steam_id, played in (commanders or {}).items():
            side = self.side_of_steam(steam_id)
            if side is None:
                # Somebody who did not draft. That is the roster check's job, and
                # it aborts rather than replays — not this one's business.
                continue
            drafted = want.get(f"commander_{side.lower()}")
            if drafted and played and played != drafted:
                out.append(f"side {side}: drafted {drafted}, played {played}")
        return out

    def note_game(self, account: Account, game: int, winner: str,
                  steam_ids: list[str] | None = None,
                  map_played: str | None = None,
                  commanders: dict[str, str] | None = None,
                  winner_steam: str | None = None) -> dict:
        """Record one finished game of the series.

        Reported by the clients from their own game log, which is the only place
        the result exists. It does three things at once: it spends the winner's
        commander, it opens the next game's commanders for both sides, and it is
        what makes the series end at two wins rather than after three games.

        Idempotent per game, and the first report wins — both clients report the
        same game, and they should agree.
        """
        seat = self.seat_of(account)
        if not self.draft.done:
            raise AuthError("the draft is not finished yet")
        if self.voided:
            raise AuthError("this series was voided by both players")
        if self.aborted:
            raise AuthError(f"this series was aborted: {self.aborted_reason}")
        if self.concluded:
            raise AuthError("this series was already closed out")
        if self.cancelled:
            raise AuthError(f"side {self.cancelled_by} left this series")

        # Who actually played. Checked before the result is counted, because a
        # game played by the wrong people must not become a rating change. An
        # empty roster is a report with nothing to check, which is refused below
        # rather than waved through.
        if steam_ids:
            missing = self.check_roster(steam_ids)
            if missing:
                # Absence and substitution are not the same thing, and treating
                # them the same aborted real series. Whoever quits Forts first
                # hands their client a log the other player has already left, so
                # a drafted side goes missing for a reason that is nobody's
                # fault — and the abort fired on whichever of the two closed the
                # game first.
                #
                # Somebody undrafted in the seat is the case aborting is for.
                # Nobody in the seat is a game that did not happen properly, and
                # that is a replay, exactly like a wrong commander.
                drafted = {self._steam_ids.get(s.account_id)
                           for s in self.seats.values()}
                stranger = any(x and x not in drafted for x in steam_ids)
                if stranger:
                    self.abort(missing[0],
                               f"side {', '.join(missing)} was played by a "
                               "different Steam account than the one that "
                               "drafted")
                else:
                    self.deviations[game] = [
                        f"side {', '.join(missing)} was not in the game — "
                        "somebody left before it was recorded"]
                    self.voided_games.add(game)
                return self.public_state(account)
        if game in self.draft.played_games():
            return self.public_state(account)

        # What was actually played, against what was drafted. A game that does
        # not match is not counted — it is put back for a replay under the same
        # number, because the series is not over: that game has not happened.
        #
        # The result is deliberately *not* recorded first and undone after. A
        # wrong-commander game that briefly counts is a wrong-commander game
        # that the other client may report on top of.
        # No evidence is not the same as evidence of nothing wrong.
        #
        # These fields were optional, and the checks were written as "if we were
        # sent something, compare it". So the way past both the roster check and
        # the plan check was to leave them out — and since the first report of a
        # game wins, the losing client could simply report first, with nothing
        # attached, and be recorded as the winner. Now a report with nothing to
        # check is refused like any other mismatch, and says so.
        if not commanders or not map_played:
            self.deviations[game] = ["no evidence sent with the result — "
                                     "update the client"]
            self.voided_games.add(game)
            return self.public_state(account)
        if (off := self.check_against_plan(game, map_played, commanders)):
            self.deviations[game] = off
            self.voided_games.add(game)
            return self.public_state(account)
        # Played correctly after a replay: the game is live again.
        self.voided_games.discard(game)
        self.deviations.pop(game, None)

        # The winner by Steam ID when the client sent one. The log's side
        # numbers are per game and Forts swaps them, so "side 1 won" cannot be
        # turned into a drafted side without knowing who side 1 was.
        if winner_steam and (mapped := self.side_of_steam(winner_steam)):
            winner = mapped
        try:
            side = Side(winner)
        except ValueError as e:
            raise AuthError(f"{winner!r} is not a side") from e
        if not 1 <= game <= self.draft.best_of:
            raise AuthError(f"game {game} is not in a Bo{self.draft.best_of}")
        if game != self.draft.revealed_through():
            # Out of order would open a later game while an earlier one is still
            # unplayed, which is exactly the reveal this is protecting.
            raise AuthError(
                f"game {self.draft.revealed_through()} is the one being played")
        self.draft.note_result(game, side)
        return self.public_state(account)

    def set_lobby(self, account: Account, lobby_id: int,
                  password: str | None = None) -> dict:
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
        # Against the *assigned* host, not the stored field: with nobody having
        # taken hosting over the field is empty, and comparing it let the guest
        # register the lobby.
        host = self.assigned_host()
        if host is not None and host != seat.side.value:
            raise AuthError(f"side {host} is hosting this series")
        self.lobby_id = int(lobby_id)
        # Starts the guest's half of the handoff clock.
        if self.lobby_at is None:
            self.lobby_at = self._now()
        self.lobby_host = seat.side.value
        self.lobby_host_steam = account.steam_id
        if password:
            self.lobby_password = password.strip()[:32]
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
        self._note_done()
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
        moved = self.draft.tick()
        self._note_done()
        return moved

    def _note_done(self) -> None:
        """Start the handoff clock the moment the draft is finished.

        Stamped from both the move path and the timeout path: a draft whose last
        step was decided by the clock is just as finished, and would otherwise
        have got no deadline at all.
        """
        if self.draft.done and self.done_at is None:
            self.done_at = self._now()


class DraftService:
    def __init__(self, now=time.time) -> None:
        self._now = now
        self.sessions: dict[str, DraftSession] = {}

    def create(self, host: Account, map_pool: list[str],
               commander_pool: list[str], best_of: int = 3,
               commander_bans_per_side: int = 1,
               step_seconds: float | None = 30.0,
               series_id: str | None = None,
               team_size: int = 1) -> DraftSession:
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

        if team_size not in (1, 2):
            raise AuthError("a side is one or two players")
        s = DraftSession(id=secrets.token_hex(6),
                         join_code=secrets.token_hex(3).upper(),
                         draft=draft, series_id=series_id,
                         created_at=self._now(),
                         original_map_pool=list(map_pool),
                         team_size=team_size)
        s.seats[host.id] = Seat(Side.A, host.id, _name(host))
        if host.steam_id:
            s._steam_ids[host.id] = host.steam_id
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
            raise AuthError(
                f"this draft already has all {2 * s.team_size} players")
        side = s.free_side()
        if side is None:                      # `full()` said otherwise: impossible
            raise AuthError("no seat left in this draft")
        s.seats[account.id] = Seat(side, account.id, _name(account))
        if account.steam_id:
            s._steam_ids[account.id] = account.steam_id
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
                             team_size=row.get("team_size") or 1,
                             created_at=row["created_at"],
                             original_map_pool=list(row["map_pool"]),
                             lobby_id=row.get("lobby_id"),
                             lobby_host=row.get("lobby_host"),
                             lobby_host_steam=row.get("lobby_host_steam"),
                             cancelled_by=row.get("cancelled_by"),
                             lobby_password=row.get("lobby_password"),
                             aborted_side=row.get("aborted_side"),
                             aborted_reason=row.get("aborted_reason"),
                             voided=bool(row.get("voided")),
                             concluded=bool(row.get("concluded")),
                             done_at=row.get("done_at"),
                             lobby_at=row.get("lobby_at"),
                             guest_ready_at=row.get("guest_ready_at"),
                             extra_seconds=row.get("extra_seconds") or 0.0,
                             deviations=dict(row.get("deviations") or {}),
                             voided_games=set(row.get("voided_games") or []))
            for seat in row["seats"]:
                s.seats[seat["account_id"]] = Seat(
                    Side(seat["side"]), seat["account_id"], seat["display"])
                # Restored with the seat. Without it every check that rests on a
                # drafted Steam ID answered "nothing wrong" to anything after a
                # redeploy — the roster check, the wrong-commander verdict and
                # the guest's join link, all three at once and all three
                # silently.
                if seat.get("steam_id"):
                    s._steam_ids[seat["account_id"]] = seat["steam_id"]
            for c in row["choices"]:
                try:
                    draft.apply(c["value"], Side(c["side"]) if c["side"] else None)
                except (ValueError, KeyError):
                    break
            # Reported games, replayed through note_result so the burned
            # commanders and the reveal come out the same as before the restart.
            for g, side in (row.get("results") or {}).items():
                try:
                    draft.note_result(int(g), Side(side))
                except (ValueError, KeyError):
                    continue
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
