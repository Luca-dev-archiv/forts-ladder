"""Tests for the adapter that feeds brackets-viewer.js.

The viewer is somebody else's library and is not tested here. What is tested is
the translation into its format, because that is where a wrong field name or a
wrong enum value produces a bracket that is silently blank or shows a match
nobody can play.

The status numbers are the load-bearing part: 0 locked, 1 waiting, 2 ready,
4 completed. A viewer handed the wrong one draws the wrong thing without
complaining.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.tournament import Participant, Tournament  # noqa: E402
from server.brackets import (  # noqa: E402
    COMPLETED, LOCKED, READY, WAITING, viewer_data,
)


def four() -> Tournament:
    return Tournament("Cup", [Participant(n, r) for n, r in
                              (("A", 2000), ("B", 1500),
                               ("C", 1200), ("D", 900))])


def by_id(data: dict) -> dict:
    return {m["id"]: m for m in data["matches"]}


def test_the_shape_has_everything_the_viewer_reads():
    d = viewer_data(four(), "t1")
    for key in ("participants", "stages", "matches", "matchGames"):
        assert key in d, key
    assert len(d["participants"]) == 4
    assert len(d["round"]) == 2, "four entrants is two rounds"
    assert len(d["matches"]) == 3, "two semis and a final"
    assert d["stages"][0]["type"] == "single_elimination"
    # Every match has to point at a round that exists, or the viewer drops it.
    rounds = {r["id"] for r in d["round"]}
    assert all(m["round_id"] in rounds for m in d["matches"])


def test_a_playable_match_is_ready_and_an_undecided_final_is_locked():
    d = by_id(viewer_data(four(), "t1"))
    assert d["R1M1"]["status"] == READY
    assert d["R1M2"]["status"] == READY
    assert d["R2M1"]["status"] == LOCKED, \
        "a final with neither side known must not look playable"
    assert d["R2M1"]["opponent1"] is None
    assert d["R2M1"]["opponent2"] is None


def test_one_side_known_is_waiting_not_ready():
    """Drawn differently from a match that cannot start at all, which is the
    whole reason the distinction exists."""
    t = four()
    t.report("R1M1", "A", (3, 0))
    d = by_id(viewer_data(t, "t1"))
    assert d["R2M1"]["status"] == WAITING
    assert d["R2M1"]["opponent1"] is not None
    assert d["R2M1"]["opponent2"] is None


def test_a_result_shows_as_win_and_loss_with_the_score():
    t = four()
    t.report("R1M1", "A", (3, 1))
    m = by_id(viewer_data(t, "t1"))["R1M1"]
    assert m["status"] == COMPLETED
    sides = {o["result"]: o["score"] for o in (m["opponent1"], m["opponent2"])}
    assert sides == {"win": 3, "loss": 1}, sides


def test_participants_are_numbered_by_seat_not_by_seed():
    """A rename or a re-seed must not move a result onto a different player, so
    the id is the registration order and nothing else."""
    t = four()
    d = viewer_data(t, "t1")
    assert [p["name"] for p in d["participants"]] == ["A", "B", "C", "D"]
    assert [p["id"] for p in d["participants"]] == [0, 1, 2, 3]

    t.rename(0, "Alpha")
    d = viewer_data(t, "t1")
    assert d["participants"][0] == {"id": 0, "tournament_id": "t1",
                                   "name": "Alpha"}


def test_a_bye_is_a_win_and_never_a_forfeit():
    """Nobody gave anything up — the seed had no opponent. Marking it as a
    forfeit would read as a walkover in the bracket."""
    t = Tournament("Cup", [Participant(n) for n in ("A", "B", "C")])
    d = by_id(viewer_data(t, "t1"))
    bye = next(m for m in d.values()
               if m["status"] == COMPLETED
               and (m["opponent1"] is None or m["opponent2"] is None))
    present = bye["opponent1"] or bye["opponent2"]
    assert present["result"] == "win"
    assert "forfeit" not in present


def test_the_padded_size_is_what_the_byes_come_from():
    t = Tournament("Cup", [Participant(n) for n in ("A", "B", "C")])
    assert viewer_data(t, "t1")["stages"][0]["settings"]["size"] == 4


def test_a_finished_tournament_has_no_playable_match_left():
    t = four()
    t.report("R1M1", "A", (3, 0))
    t.report("R1M2", "B", (3, 0))
    t.report("R2M1", "A", (3, 0))
    d = viewer_data(t, "t1")
    assert all(m["status"] == COMPLETED for m in d["matches"])
    assert t.champion is not None and t.champion.name == "A"


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e or 'assertion failed'}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
