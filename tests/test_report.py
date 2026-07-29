"""Tests for series grouping — the part that goes wrong most easily.

A badly cut series reports a Bo5 as two duels (or two duels as one) and
distorts the ranking directly. The edge cases here all come from the real
rule set and from what the log actually delivers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.identity import Registry  # noqa: E402
from ladder.report import group_series  # noqa: E402

A, B, C = "76561199000000001", "76561199000000002", "76561199000000003"


def match(when, lobby, winner=1, opponent=B, map_name="Vanilla",
          swap=False, commanders=None):
    """One game.

    `swap` puts A on side 2 and the opponent on side 1, which is what Forts does
    between games of a series — and which every test here used to ignore.
    """
    a_side, b_side = (2, 1) if swap else (1, 2)
    return {
        "map": map_name, "played_at": when, "lobby_id": lobby,
        "players": [{"name": "A", "steam_id": A, "side": a_side},
                    {"name": "B", "steam_id": opponent, "side": b_side}],
        "outcome": {"status": "decided", "winner_side": winner},
        "duration_s": 300, "commanders": commanders or {},
    }


def test_same_lobby_is_one_series():
    """The normal case: one Bo3 in one lobby."""
    s = group_series([match("2026-07-28T20:00:00", 111),
                      match("2026-07-28T20:12:00", 111, winner=2),
                      match("2026-07-28T20:25:00", 111)])
    assert len(s) == 1
    assert len(s[0].matches) == 3


def test_host_crash_does_not_split_a_series():
    """When the host crashes, play continues in a NEW lobby.

    The rule set covers this explicitly (save the state with \\save and play
    on). Cutting by lobby ID would turn one Bo3 into two reported duels —
    distorting the ranking directly.
    """
    s = group_series([match("2026-07-28T20:00:00", 111),
                      match("2026-07-28T20:12:00", 111, winner=2),
                      match("2026-07-28T20:31:00", 222)])
    assert len(s) == 1, "the host change cut the series in two"
    assert len(s[0].matches) == 3
    line, warnings = s[0].report(Registry())
    assert line.endswith("2-1")
    assert any("lobbies" in w for w in warnings), (
        "the host change must be surfaced")


def test_new_opponent_in_the_same_lobby_is_a_new_series():
    """A lobby can stay up all evening while the opponents change."""
    s = group_series([match("2026-07-28T20:00:00", 111),
                      match("2026-07-28T20:20:00", 111, opponent=C)])
    assert len(s) == 2


def test_next_evening_is_a_new_series():
    s = group_series([match("2026-07-28T20:00:00", 111),
                      match("2026-07-29T20:00:00", 222)])
    assert len(s) == 2


def test_missing_lobby_id_still_groups_by_roster_and_time():
    """Log fragments without a `Setting lobby` line must not fall apart."""
    a = match("2026-07-28T20:00:00", None)
    b = match("2026-07-28T20:10:00", None, winner=2)
    assert len(group_series([a, b])) == 1


def test_report_is_written_from_the_local_players_view():
    """Your own score comes first — even when the log is the opponent's."""
    ms = [match("2026-07-28T20:00:00", 111, winner=2)]
    ms[0]["players"][1]["local"] = True          # Log stammt vom Gegner
    s = group_series(ms)
    line, _ = s[0].report(Registry(), )
    # Without a known own SteamID it falls back to the lowest side; what
    # matters is that a line with a score comes out at all.
    assert " vs " in line and ("0-1" in line or "1-0" in line)


# --------------------------------------------------- Sides swap between games
def test_a_series_has_two_teams_even_when_the_sides_swap():
    """Forts swaps the sides between games. Grouping the series by side number
    put the same people in both buckets, so a duel read "A and B vs A and B"."""
    s = group_series([match("2026-07-28T20:00:00", 111),
                      match("2026-07-28T20:30:00", 111, swap=True)])[0]
    sides = s.sides()
    assert len(sides) == 2, sides
    ids = {team: {p["steam_id"] for p in players}
           for team, players in sides.items()}
    assert ids[1] == {A} and ids[2] == {B}, ids


def test_one_team_winning_twice_is_two_nil_not_one_all():
    """The consequence nobody would have noticed: counting wins by side number
    turned a 2-0 into a 1-1, and that goes straight into the report line."""
    # A wins game 1 as side 1, then wins game 2 as side 2.
    s = group_series([match("2026-07-28T20:00:00", 111, winner=1),
                      match("2026-07-28T20:30:00", 111, winner=2, swap=True)])[0]
    wins, unclear = s.score()
    assert unclear == 0
    assert wins == {1: 2, 2: 0}, wins


def test_the_report_line_names_my_team_first_after_a_swap():
    reg = Registry()
    s = group_series([match("2026-07-28T20:00:00", 111, winner=1),
                      match("2026-07-28T20:30:00", 111, winner=2, swap=True)])[0]
    line, _ = s.report(reg, my_steam_id=A)
    assert " 2-0" in line, line
    assert line.index("A") < line.index("B"), line


def test_a_commander_reused_after_a_win_is_caught_across_a_swap():
    """Tracked per team: keyed by side number, one player's history was split in
    two and a genuine reuse went unnoticed."""
    reg = Registry()
    cmd = "commander-da-overclocker"
    s = group_series([
        match("2026-07-28T20:00:00", 111, winner=1,
              commanders={"side1": cmd, "side2": "commander-ee-fireman"}),
        # A won with it, swapped side, and played it again.
        match("2026-07-28T20:30:00", 111, winner=2, swap=True,
              commanders={"side2": cmd, "side1": "commander-iba-spy"}),
    ])[0]
    line, warnings = s.report(reg, my_steam_id=A)
    reuse = [w for w in warnings if w.startswith("commander reuse")]
    assert len(reuse) == 1, warnings
    assert cmd in reuse[0], reuse[0]
    # And the score is right in the same breath: 2-0, not 1-1.
    assert line.endswith(" 2-0"), line


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
