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
            # Who is *meant* to host, which is a decision and not a race.
            "lobby_host": self.assigned_host(),
            "lobby_host_steam": self.lobby_host_steam or self._host_steam(),
            # Only the two of them. A spectator gets in through the host, and a
            # password handed to anyone who asks for the state protects nothing.
            "lobby_password": self.lobby_password if seat else None,
            "seats": self._seat_names(seat),
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
            "plan": self._plan_for(seat),
            # How far the series has got, so a client can say "game 2 of 3" and
            # explain why later games are still blank.
            "revealed_through": d.revealed_through(),
            "games_played": sorted(d.played_games()),
            "wins": self.wins(),
            "series_over": self.series_over(),
            "voided": self.voided,
            "voided_games": sorted(self.voided_games),
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
        """Who is in which seat — with the opponent anonymous while picking.

        Knowing who you are against changes how you ban, and a queue match is
        supposed to be decided by the board rather than by the name. Once the
        picking is over it is pointless to hide: you are about to play them.

        Not applied to a draft somebody hosted with a join code — they invited a
        specific person and already know who it is.
        """
        names = {s.side.value: s.display for s in self.seats.values()}
        if self.draft.done or self.series_id is None or seat is None:
            return names
        return {side: (name if side == seat.side.value
                       else "Opponent")
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

    def check_roster(self, account: Account,
                     steam_ids: list[str]) -> list[str]:
        """Who is actually in the lobby against who drafted.

        The draft binds two accounts, each with a proven Steam ID. A game played
        by somebody else is not that match — whether it is a friend at the
        keyboard, a second account, or simply the wrong lobby, the result cannot
        be rated as if the drafted pair had played it.

        Returns the ids that do not belong. Empty means the roster is the one
        that drafted.
        """
        expected = {s for s in self._steam_ids.values() if s}
        if not expected:
            # Nothing to compare against: accounts without a linked Steam ID
            # cannot be checked, and refusing on that basis would punish the
            # wrong thing.
            return []
        return sorted(set(steam_ids) - expected)

    def abort(self, side: str, reason: str) -> None:
        """End the series on a fact rather than on an agreement."""
        if self.aborted:
            return
        self.aborted_side = side
        self.aborted_reason = reason[:200]

    def note_game(self, account: Account, game: int, winner: str,
                  steam_ids: list[str] | None = None) -> dict:
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

        # Who actually played. Checked before the result is counted, because a
        # game played by the wrong people must not become a rating change.
        if steam_ids:
            strangers = self.check_roster(account, steam_ids)
            if strangers:
                # Named against the side that reported it: the client reports its
                # own log, so the unexpected id came from that machine's lobby.
                self.abort(seat.side.value,
                           f"{len(strangers)} player(s) in the lobby did not "
                           "draft this series")
                return self.public_state(account)
        if game in self.draft.played_games():
            return self.public_state(account)
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
            raise AuthError("this draft already has two players")
        s.seats[account.id] = Seat(Side.B, account.id, _name(account))
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
                             voided_games=set(row.get("voided_games") or []))
            for seat in row["seats"]:
                s.seats[seat["account_id"]] = Seat(
                    Side(seat["side"]), seat["account_id"], seat["display"])
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
