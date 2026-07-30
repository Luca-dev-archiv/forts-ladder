"""Reported series, and the standings that come out of them.

This is the part that was missing: everything else in the project could
record, draft, pair and display, but a finished series went nowhere. The
shared ranking was the imported spreadsheet and nothing else, so winning a
match on this ladder changed no number anybody else could see.

Three decisions, all of them deliberate:

**Events are stored, ratings are not.** A rating is recomputed from the whole
event list every time it is asked for (`ladder.ratings.recompute`). That is
what makes withdrawal of consent work retroactively — drop the events, run it
again — and it means anybody with the same file arrives at the same numbers.

**A report is accepted only from someone who played in it.** Not from an
admin panel, not from an unauthenticated POST. The reporter's own Steam ID has
to be one of the participants, which is checked against their linked account
rather than against anything in the request body.

**Both sides must have agreed to be tracked, and the lobby must be one the
ladder set up.** This is the condition the project was cleared under, and it
is enforced here rather than promised: an unsanctioned lobby, or one player
who never opted in, means the series is stored as *seen* but rated for
nobody.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from .auth import Account, AuthError


@dataclass
class Reported:
    """One series, as the players' clients reported it."""
    id: str
    lobby_id: int | None
    #: SteamID64 -> side, exactly as the game log had it.
    sides: dict[str, int]
    games: int
    #: Games won by the lower side number, which is how `ladder.ratings`
    #: expects a series to be expressed.
    score_low: int
    played_at: str
    reported_by: str
    #: False when it was accepted but must not affect anybody's rating.
    rated: bool = True
    reasons: list[str] = field(default_factory=list)
    replays: list[str] = field(default_factory=list)
    #: Taken back by a reviewer: who, when and why.
    #:
    #: Not a deletion. The row stays and stays visible, because "this did not
    #: count" is a decision somebody made and the person it was made about is
    #: entitled to see it. `events()` skips it, and the standings are recomputed
    #: from what is left — the same mechanism that makes withdrawing consent
    #: retroactive.
    annulled_by: str | None = None
    annulled_at: float | None = None
    annul_note: str = ""
    #: A player asked for a human to look at this one.
    #:
    #: The reason a series is stored even when it cannot be rated: "it did not
    #: count" is sometimes the software being wrong, and the person it happened
    #: to is the only one who knows. Without somewhere to say so they would have
    #: to find an admin on Discord and describe a match from memory.
    flagged: bool = False
    flag_note: str = ""
    created_at: float = field(default_factory=time.time)

    def low_side(self) -> int:
        return min(self.sides.values())

    def names_on(self, side: int) -> list[str]:
        return sorted(sid for sid, s in self.sides.items() if s == side)


class ResultService:
    """Accepts reports, refuses the ones that must not count, ranks the rest."""

    def __init__(self, auth, store, ranking) -> None:
        self._auth = auth
        self._store = store
        self._ranking = ranking

    # ------------------------------------------------------------- Accepting
    def report(self, account: Account, *, lobby_id: int | None,
               sides: dict[str, int], games: int, score_low: int,
               played_at: str, replays: list[str] | None = None,
               draft_id: str | None = None) -> Reported:
        """Take one finished series from a client that played in it.

        `draft_id` is what makes two reports of one series *one* series. Both
        clients report — that is deliberate, so a series survives one of them
        being closed — and the id used to be random, so the two arrivals became
        two rows and the rating moved twice. It could not be derived from what
        the clients send either: only the host's log has a lobby id, and the two
        logs disagree about the kickoff second. The draft id is the one thing the
        server itself handed to both of them.
        """
        account.require("report_own_match")
        if account.steam_id is None:
            raise AuthError("link your Steam account before reporting")
        if len(set(sides.values())) != 2:
            raise AuthError("a series needs exactly two sides")
        if games <= 0 or not 0 <= score_low <= games:
            raise AuthError(f"{score_low} of {games} is not a possible score")
        if account.steam_id not in sides:
            # The one check that cannot be relaxed: otherwise anyone could
            # report a result between two other people.
            raise AuthError("you are not in this series")

        reasons = self._why_not(lobby_id, sides)
        # Derived, not drawn: same series, same row.
        #
        # The draft id when there is one — the server handed it to both clients,
        # so it is the one thing they cannot disagree about. Failing that, a hash
        # of what identifies a series anyway: the lobby, the kickoff and who
        # played. Either way two reports of one series collide on the primary
        # key, where a random id made them two rows and two rating changes.
        rid = f"d-{draft_id}" if draft_id else "s-" + hashlib.sha256(
            "|".join([str(lobby_id), played_at,
                      *sorted(f"{k}:{v}" for k, v in sides.items())])
            .encode()).hexdigest()[:16]
        r = Reported(id=rid, lobby_id=lobby_id,
                     sides={str(k): int(v) for k, v in sides.items()},
                     games=games, score_low=score_low, played_at=played_at,
                     reported_by=account.id, rated=not reasons,
                     reasons=reasons, replays=list(replays or []))
        self._store.save_result(r)
        return r

    def flag(self, account: Account, result_id: str, note: str) -> "Reported":
        """Ask for a human to look at a reported series.

        Only somebody who played in it: this is a report about their own match,
        not a way to file complaints about other people.
        """
        rows = {r.id: r for r in self._store.load_results()}
        r = rows.get(result_id)
        if r is None:
            raise AuthError("unknown series")
        if account.steam_id not in r.sides:
            raise AuthError("you are not in this series")
        r.flagged = True
        r.flag_note = (note or "").strip()[:500]
        self._store.update_result_flag(r)
        return r

    def annul(self, actor: Account, result_id: str, note: str) -> "Reported":
        """Take a series' rating back.

        The one thing a reviewer can do that a player cannot, and the reason a
        player's flag is worth anything: without this, "please look at this" ends
        in somebody agreeing with you and being unable to act.

        A note is required. A rating that vanished for no recorded reason is
        indistinguishable from a bug, and the two players will ask.
        """
        actor.require("review_results")
        note = (note or "").strip()
        if not note:
            raise AuthError("say why — a rating taken back without a reason "
                            "cannot be told apart from a bug")
        rows = {x.id: x for x in self._store.load_results()}
        row = rows.get(result_id)
        if row is None:
            raise AuthError("unknown series")
        if row.annulled_by is not None:
            return row
        row.annulled_by = actor.id
        row.annulled_at = time.time()
        row.annul_note = note[:500]
        row.rated = False
        row.reasons = list(row.reasons) + [f"annulled: {row.annul_note}"]
        self._store.update_result_review(row)
        return row

    def reinstate(self, actor: Account, result_id: str) -> "Reported":
        """Undo an annulment, for when the reviewer was the one who was wrong."""
        actor.require("review_results")
        rows = {x.id: x for x in self._store.load_results()}
        row = rows.get(result_id)
        if row is None:
            raise AuthError("unknown series")
        row.annulled_by = None
        row.annulled_at = None
        row.annul_note = ""
        # Rateable again only if nothing *else* was standing in the way.
        row.reasons = [x for x in row.reasons if not x.startswith("annulled:")]
        row.rated = not row.reasons
        self._store.update_result_review(row)
        return row

    def one(self, result_id: str) -> "Reported | None":
        return {x.id: x for x in self._store.load_results()}.get(result_id)

    def flagged(self) -> list["Reported"]:
        """Everything waiting for a human, newest first."""
        return sorted((r for r in self._store.load_results() if r.flagged),
                      key=lambda r: -r.created_at)

    def _why_not(self, lobby_id: int | None, sides: dict[str, int]) -> list[str]:
        """Everything standing between this series and the ladder.

        Returned as a list rather than a bool so a client can say *which* of
        the two conditions is missing — "not sanctioned" and "your opponent
        never opted in" call for completely different next steps.
        """
        reasons: list[str] = []
        if lobby_id is None:
            reasons.append("no lobby id — the ladder cannot tell which game "
                           "this was")
        elif not self._store.is_sanctioned(int(lobby_id)):
            reasons.append(f"lobby {lobby_id} was not set up by this ladder")

        trackable = self._auth.trackable_ids()
        missing = sorted(s for s in sides if s not in trackable)
        if missing:
            # Named by count, not by id: telling one player which Steam IDs the
            # server holds would turn this into a lookup service.
            reasons.append(f"{len(missing)} of {len(sides)} players have not "
                           "agreed to be tracked")
        return reasons

    # -------------------------------------------------------------- Standings
    def events(self) -> list[dict]:
        """Stored reports as rating events, ladder names resolved.

        A player with no linked, consenting account is skipped entirely — which
        is what makes withdrawing consent retroactive: their events stop being
        produced here, and the next recompute has never heard of them.
        """
        by_steam = {a.steam_id: a for a in self._auth.accounts.values()
                    if a.trackable and a.steam_id}
        out: list[dict] = []
        for r in self._store.load_results():
            if not r.rated:
                continue
            low = r.low_side()
            high = next(s for s in set(r.sides.values()) if s != low)
            a = [by_steam.get(s) for s in r.names_on(low)]
            b = [by_steam.get(s) for s in r.names_on(high)]
            if any(x is None for x in a) or any(x is None for x in b):
                continue
            names_a = [x.ufer_name or x.discord_name or x.id for x in a]  # type: ignore[union-attr]
            names_b = [x.ufer_name or x.discord_name or x.id for x in b]  # type: ignore[union-attr]
            # The stored value is a full timestamp, because lobby plus kickoff
            # is what makes two series in one lobby two series. The rating
            # orders by day, so it gets the day.
            date = r.played_at[:10]
            if len(names_a) == 1 and len(names_b) == 1:
                out.append({"kind": "1v1", "date": date,
                            "a": names_a[0], "b": names_b[0],
                            "games": r.games, "score_a": r.score_low,
                            # Ties the games of one series together for the
                            # entry-rating rule, and orders same-day series.
                            "event_id": r.id, "event": r.played_at})
            else:
                out.append({"kind": "team", "date": date,
                            "team_a": names_a, "team_b": names_b,
                            "games": r.games, "score_a": r.score_low,
                            "event_id": r.id, "event": r.played_at})
        return out

    def refresh_ranking(self) -> int:
        """Recompute the open column. Cheap enough to do on every read.

        A few hundred events over a few hundred players is microseconds, and
        caching it would mean holding a rating that a consent withdrawal has
        already invalidated.
        """
        self._ranking.reload()
        return self._ranking.apply_open(self.events())
