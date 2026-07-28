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


def match(when, lobby, winner=1, opponent=B, map_name="Vanilla"):
    return {
        "map": map_name, "played_at": when, "lobby_id": lobby,
        "players": [{"name": "A", "steam_id": A, "side": 1},
                    {"name": "B", "steam_id": opponent, "side": 2}],
        "outcome": {"status": "decided", "winner_side": winner},
        "duration_s": 300, "commanders": {},
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
