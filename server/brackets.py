"""Our bracket, in the shape brackets-viewer.js draws.

Drawing a bracket properly is more work than it looks: connector lines between
rounds, byes that skip a round, a column of boxes spread so the pairings line
up. The first attempt here was rounds as columns of cards, which is not a
bracket — it made it impossible to see who could meet whom, which is the only
thing a bracket is for.

So the drawing is done by `brackets-viewer.js` (MIT, vendored under
`server/static/`), and this module is the adapter. What it does **not** do is
move the tournament logic over: `ladder/tournament.py` stays the source of
truth for seeding, byes and results, because it knows this league's rules and
is covered by tests. Only the presentation is borrowed.

The target format is the viewer's `ViewerData`: four plural arrays — `stages`,
`matches`, `matchGames`, `participants`. The singular table names of
`brackets-manager` storage look right and are not; handed those, the viewer says
"the `data.stages` array is either empty or undefined". Groups and rounds are
not passed at all — they are derived from each match's `group_id` and
`round_id`.

Its numbers matter more than they look: `status` is an enum where 0 is locked,
2 is ready and 4 is completed, and a viewer given the wrong one draws a match
nobody can play.
"""

from __future__ import annotations

from ladder.tournament import Tournament

#: brackets-model Status. Named rather than inlined because a bare 2 in the
#: middle of a dict is unreadable and easy to get wrong.
LOCKED = 0
WAITING = 1
READY = 2
COMPLETED = 4

#: One stage, one group: single elimination has nothing else in it.
STAGE_ID = 0
GROUP_ID = 0


def viewer_data(t: Tournament, tid: str) -> dict:
    """The whole bracket as brackets-viewer wants it.

    Participants are numbered by seat, not by seed, so a rename or a re-seed
    cannot silently move a result onto a different player.
    """
    seat_of = {p.name: i for i, p in enumerate(t.participants)}

    participants = [{"id": i, "tournament_id": tid, "name": p.name}
                    for i, p in enumerate(t.participants)]

    rounds = [{"id": r, "stage_id": STAGE_ID, "group_id": GROUP_ID,
               "number": r + 1} for r in range(len(t.rounds))]

    matches = []
    for r, in_round in enumerate(t.rounds):
        for n, m in enumerate(in_round):
            matches.append({
                "id": m.id,
                "stage_id": STAGE_ID,
                "group_id": GROUP_ID,
                "round_id": r,
                "number": n + 1,
                "child_count": 0,
                "status": _status(m),
                "opponent1": _side(m, m.a, seat_of),
                "opponent2": _side(m, m.b, seat_of),
            })

    return {
        "participants": participants,
        "stages": [{
            "id": STAGE_ID, "tournament_id": tid, "name": t.name,
            "type": "single_elimination", "number": 1,
            # `size` is the padded bracket size, which is what byes come from.
            "settings": {"size": len(t.rounds[0]) * 2 if t.rounds else 0,
                         "seedOrdering": ["natural"],
                         "matchesChildCount": 0},
        }],
        "matches": matches,
        # No sub-games: a Bo3 is one match with a score here, not three rows.
        "matchGames": [],
        # Not part of ViewerData — the viewer derives rounds from the matches —
        # but kept so `GET /tournaments/{tid}/viewer` is also useful to anything
        # reading brackets-manager storage.
        "round": rounds,
        "group": [{"id": GROUP_ID, "stage_id": STAGE_ID, "number": 1}],
    }


def _status(m) -> int:
    if m.winner is not None:
        return COMPLETED
    if m.a is not None and m.b is not None:
        return READY
    if m.a is not None or m.b is not None:
        # One side known, still waiting for the other. Drawn differently from a
        # match that cannot start at all, which is the point of the distinction.
        return WAITING
    return LOCKED


def _side(m, who, seat_of: dict[str, int]) -> dict | None:
    """One opponent slot, or null for "to be determined".

    A bye is deliberately *not* marked as a forfeit: nobody gave anything up,
    the seed simply had no opponent, and calling it a forfeit would read as a
    walkover in the bracket.
    """
    if who is None:
        return None
    out: dict = {"id": seat_of.get(who.name)}
    if m.winner is not None:
        out["result"] = "win" if m.winner is who else "loss"
    if m.score is not None and m.a is not None and m.b is not None:
        out["score"] = m.score[0] if who is m.a else m.score[1]
    return out
