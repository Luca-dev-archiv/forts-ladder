"""Tests for the log parser — the foundation everything else stands on.

There were none, and two bugs lived in here unnoticed for the whole project:
every recorded match came out with no commanders at all, and every replay
filename was truncated at its first space. The second one silently caused three
further failures, which is what makes it worth a test of its own.

The line *order* below is taken from a real log, because the order is the bug:
the commander lines arrive after the game reports itself over. The ids and
names in it are fabricated — the Steam IDs deliberately contain a run of zeros
no real account has, which is also how CI tells a fixture from a leak. Never
paste a real log in here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.recorder import Parser  # noqa: E402

#: One complete multiplayer game, in the order Forts writes it. The
#: interesting part is lines 6-9: `mDone detected` comes before the commanders
#: and before the replay name.
GAME = [
    "Logged into Steam as local_player (76561199000000001)",
    "  Game mode: Team Death Match",
    "Loading map maps/Up & Down/Up & Down.fwe",
    "OnMultiStart host 0, players 2",
    "  1: local_player, Id 3, Team 1, alive, join at 0, (Steam), "
    "SteamID 76561199000000001, x, Local 1, ping 0.102",
    "  2: opponent_one, Id 1, Team 2, alive, join at 0, (Steam), "
    "SteamID 76561199000000002, x, Local 0, ping 0.0",
    "  13:02 opponent_one has been defeated!",
    "World::Execute mDone detected",
    "  Team1 commander: commander-da-overclocker",
    "  Team2 commander: commander-cf-buster",
    "Replay saved as replays/v1.38.2_Up & Down_20260728_181126.fwr",
]


def parse(lines: list[str]):
    p = Parser(fallback_time=0.0)
    for line in lines:
        p.feed(line)
    p.flush()
    return p.done


def test_the_commanders_are_recorded():
    """They are logged after `mDone detected`, so a parser that accepted only
    the replay line from then on lost them for every match ever recorded."""
    (m,) = parse(GAME)
    assert m.commanders == {1: "commander-da-overclocker",
                            2: "commander-cf-buster"}, m.commanders


def test_a_replay_filename_keeps_its_spaces():
    """"v1.38.2_Up & Down_20260728_181126.fwr" — stopping at the first space
    made the file unfindable on disk, and it is the only wall clock in the
    log."""
    (m,) = parse(GAME)
    assert m.replay == "replays/v1.38.2_Up & Down_20260728_181126.fwr"


def test_the_time_of_play_comes_from_the_replay_name():
    """Without it every match parsed after the fact is dated "now" — three
    games in one evening all landed on the same minute."""
    (m,) = parse(GAME)
    assert m.to_dict()["played_at"].startswith("2026-07-28T18:11:26")


def test_two_maps_starting_with_the_same_word_stay_two_matches():
    """The real failure the truncation caused: "Vanilla 2v2" and
    "Vanilla 4v4 long" both keyed as "replay:v1.38.2_Vanilla" and the second
    was discarded as a duplicate."""
    second = [
        "  Game mode: Team Death Match",
        "Loading map maps/Vanilla 2v2/Vanilla 2v2.fwe",
        "  1: local_player, Id 3, Team 1, alive, join at 0, (Steam), "
        "SteamID 76561199000000001, x, Local 1, ping 0.1",
        "  2: opponent_one, Id 1, Team 2, alive, join at 0, (Steam), "
        "SteamID 76561199000000002, x, Local 0, ping 0.0",
        "  13:49 opponent_one has been defeated!",
        "World::Execute mDone detected",
        "  Team1 commander: commander-da-overclocker",
        "  Team2 commander: commander-da-speed-demon",
        "Replay saved as replays/v1.38.2_Vanilla 2v2_20260728_182905.fwr",
        "  Game mode: Team Death Match",
        "Loading map 3585084905\\Vanilla 4v4 long.fwe",
        "  1: local_player, Id 3, Team 1, alive, join at 0, (Steam), "
        "SteamID 76561199000000001, x, Local 1, ping 0.1",
        "  2: opponent_one, Id 1, Team 2, alive, join at 0, (Steam), "
        "SteamID 76561199000000002, x, Local 0, ping 0.0",
        "  13:36 opponent_one has been defeated!",
        "World::Execute mDone detected",
        "  Team1 commander: commander-da-overclocker",
        "  Team2 commander: commander-da-overclocker",
        "Replay saved as replays/v1.38.2_Vanilla 4v4 long_20260728_184522.fwr",
    ]
    done = parse(second)
    assert len(done) == 2, [m.map for m in done]
    keys = {m.to_dict()["match_key"] for m in done}
    assert len(keys) == 2, keys


def test_the_next_match_cannot_attach_itself_to_a_closed_one():
    """Closing a match still has to stop *new* content: roster lines from the
    following game must start their own match, not extend this one."""
    lines = GAME + [
        "  Game mode: Team Death Match",
        "Loading map maps/Bowl/Bowl.fwe",
        "  1: local_player, Id 3, Team 1, alive, join at 0, (Steam), "
        "SteamID 76561199000000001, x, Local 1, ping 0.1",
        "  2: someone_else, Id 4, Team 2, alive, join at 0, (Steam), "
        "SteamID 76561199000000003, x, Local 0, ping 0.1",
        "  9:00 someone_else has been defeated!",
        "World::Execute mDone detected",
        "Replay saved as replays/v1.38.2_Bowl_20260728_190000.fwr",
    ]
    done = parse(lines)
    assert len(done) == 2
    first, second = done
    assert first.map == "Up & Down"
    assert "someone_else" not in {p["name"] for p in first.players.values()}
    assert second.map == "Bowl"


def test_the_loser_is_named_and_the_winner_derived():
    """The log writes no winner line — it names each loser individually."""
    (m,) = parse(GAME)
    d = m.to_dict()
    assert d["outcome"]["winner_side"] == 1
    assert d["outcome"]["loser_sides"] == [2]


def test_a_lobby_id_is_picked_up_for_the_match_it_belongs_to():
    lines = ["Setting lobby 109775241234567890 game server 1"] + GAME
    (m,) = parse(lines)
    assert m.lobby_id == 109775241234567890


def test_a_game_with_no_multiplayer_lobby_has_none():
    """Skirmish against the AI: no lobby line, so nothing to sanction — and
    the rating path has to be able to tell the difference."""
    (m,) = parse(GAME)
    assert m.lobby_id is None


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
