"""Tests for modes, the bracket and the draft time limit.

The bracket is where a bug costs the most: it surfaces mid-event, in front
of an audience, and cannot be repaired cleanly at that point. So the focus
is on entrant counts that do not divide evenly — 6, 12, 13 — rather than the
comfortable powers of two.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder import modes  # noqa: E402
from ladder.draft import Draft, Side  # noqa: E402
from ladder.tournament import Participant, Tournament, seed_order  # noqa: E402

CMDS = [f"commander-x-{n}" for n in
        ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")]
MAPS = ["Abyss", "Pillars", "Desert Ruins", "Split", "Spirals"]


def field(n: int) -> list[Participant]:
    """n entrants with descending ratings — P1 is the strongest."""
    return [Participant(f"P{i}", 2100 - i * 25) for i in range(1, n + 1)]


def play_out(t: Tournament, upsets: set[str] | None = None) -> None:
    """Play the tournament out; in `upsets` the weaker side wins."""
    upsets = upsets or set()
    guard = 0
    while not t.finished:
        guard += 1
        assert guard < 100, "tournament never finishes"
        m = t.playable()[0]
        winner = m.b if m.id in upsets else m.a
        t.report(m.id, winner.name, (3, 1))


# ------------------------------------------------------------------ Modes
def test_modes_map_to_the_right_rating_table():
    assert modes.RANKED_1V1.rating_mode == "1v1"
    assert modes.RANKED_2V2.rating_mode == "tdm"
    assert modes.COOP_2V2.rating_mode == "coop"
    assert modes.RANKED_3V3.players_per_match == 6


def test_unranked_modes_are_marked_and_skip_the_draft():
    assert not modes.UNRANKED_1V1.rated
    assert not modes.UNRANKED_1V1.draft_enabled
    assert all(m.rated for m in modes.rated_modes())


def test_team_modes_use_the_fpl_pool():
    assert modes.RANKED_1V1.needs_map_pool == "duel"
    assert modes.RANKED_2V2.needs_map_pool == "fpl"


def test_a_mode_can_be_varied_without_touching_the_original():
    special = modes.RANKED_1V1.with_(best_of=7, draft_seconds=None)
    assert special.best_of == 7 and modes.RANKED_1V1.best_of == 3


def test_unknown_mode_key_fails_loudly():
    try:
        modes.get("no-such-key")
    except KeyError as e:
        assert "Known" in str(e)
    else:
        raise AssertionError("an unknown mode was accepted")


# ---------------------------------------------------------------- Seeding
def test_seeding_keeps_the_favourites_apart():
    """Seeds 1 and 2 must not meet before the final."""
    assert seed_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]
    # 1 and 2 sit in different halves.
    order = seed_order(16)
    assert (order.index(1) < 8) != (order.index(2) < 8)


def test_byes_go_to_the_top_seeds():
    """At 6 entrants two byes are needed — they must not fall at random."""
    t = Tournament("Cup", field(6))
    byes = [m.winner.name for m in t.rounds[0] if m.bye]
    assert sorted(byes) == ["P1", "P2"], byes


# ------------------------------------------------------------------ Flow
def test_a_full_bracket_produces_exactly_one_champion():
    for n in (2, 3, 5, 6, 8, 12, 13, 16):
        t = Tournament(f"Cup{n}", field(n))
        play_out(t)
        assert t.finished
        assert t.champion is not None
        assert t.champion.name == "P1", f"{n} entrants: {t.champion.name}"


def test_an_upset_moves_the_right_player_forward():
    t = Tournament("Cup", field(8))
    first = t.playable()[0]
    underdog = first.b.name
    t.report(first.id, underdog, (3, 0))
    later = [m for r in t.rounds[1:] for m in r]
    assert any(m.a and m.a.name == underdog or m.b and m.b.name == underdog
               for m in later)


def test_reporting_an_unrelated_winner_is_refused():
    t = Tournament("Cup", field(8))
    m = t.playable()[0]
    try:
        t.report(m.id, "Outsider", (3, 0))
    except ValueError as e:
        assert "does not play" in str(e)
    else:
        raise AssertionError("a winner who is not in the match was accepted")


def test_a_score_that_cannot_decide_the_series_is_refused():
    """A Bo5 needs three wins — 2:1 decides nothing."""
    t = Tournament("Cup", field(8))
    m = t.playable()[0]
    try:
        t.report(m.id, m.a.name, (2, 1))
    except ValueError as e:
        assert "Bo5" in str(e)
    else:
        raise AssertionError("an impossible score was accepted")


def test_a_match_cannot_be_reported_twice():
    t = Tournament("Cup", field(8))
    m = t.playable()[0]
    t.report(m.id, m.a.name, (3, 0))
    try:
        t.report(m.id, m.b.name, (3, 0))
    except ValueError as e:
        assert "already decided" in str(e)
    else:
        raise AssertionError("a result was overwritten")


def test_a_match_without_both_participants_is_not_playable():
    t = Tournament("Cup", field(8))
    assert all(m.a and m.b for m in t.playable())
    assert len(t.playable()) == 4


def test_two_participants_are_enough_and_one_is_not():
    Tournament("Duel", field(2))
    try:
        Tournament("Alone", field(1))
    except ValueError:
        pass
    else:
        raise AssertionError("a tournament with one entrant was created")


# ------------------------------------------------------------ Time limit
def test_the_draft_resolves_itself_when_nobody_reacts():
    """Without it every draft stalls the moment someone walks away."""
    clock = [1000.0]
    d = Draft(map_pool=list(MAPS), commander_pool=list(CMDS), best_of=3,
              step_seconds=30)
    d._now = lambda: clock[0]
    d.seconds_left()
    clock[0] += 400
    messages = d.tick()
    assert d.done, "the draft stayed open past its deadline"
    # More notifications than steps: the blind commander steps affect BOTH
    # sides and therefore report twice.
    assert len(messages) > len(d.steps)
    assert all(g["map"] for g in d.plan())
    assert all(g["commander_a"] and g["commander_b"] for g in d.plan())


def test_the_timer_catches_up_instead_of_granting_fresh_time():
    """Each following step starts at the expired deadline, not at zero.

    Otherwise a draft left sitting for an hour would need another hour to
    catch up, and nobody could finish it.
    """
    clock = [0.0]
    d = Draft(map_pool=list(MAPS), commander_pool=list(CMDS), best_of=3,
              step_seconds=10)
    d._now = lambda: clock[0]
    d.seconds_left()
    clock[0] = 35            # three deadlines have passed
    assert len(d.tick()) >= 3


def test_a_choice_made_in_time_survives_the_timeout():
    """Whoever locked in does not get something drawn for them later."""
    clock = [0.0]
    d = Draft(map_pool=list(MAPS), commander_pool=list(CMDS), best_of=1,
              step_seconds=20)
    d._now = lambda: clock[0]
    while d.current and d.current.side is not None:
        d.apply(d.legal_options()[0])
    d.apply(CMDS[2], Side.A)            # A is on time
    clock[0] += 100                     # B misses the deadline
    d.tick()
    assert d.plan()[0]["commander_a"] == CMDS[2]
    assert d.plan()[0]["commander_b"] is not None


def test_without_a_time_limit_nothing_resolves_itself():
    d = Draft(map_pool=list(MAPS), commander_pool=list(CMDS), best_of=3,
              step_seconds=None)
    assert d.seconds_left() is None
    assert d.tick(now=10_000) == []
    assert not d.done


# ------------------------------------------------------- Host's own settings
def test_seeding_by_the_listed_order_ignores_the_ratings():
    """A host often knows the bracket they want. Then the list is the seeding
    and the numbers are noise."""
    people = [Participant("D", 900), Participant("A", 2000),
              Participant("C", 1200), Participant("B", 1500)]
    by_rating = Tournament("R", list(people))
    as_listed = Tournament("L", list(people), seeding="listed")

    assert [p.name for p in by_rating._seeded()] == ["A", "B", "C", "D"]
    assert [p.name for p in as_listed._seeded()] == ["D", "A", "C", "B"]
    # Seed 1 plays seed 8/last, so the first match differs between the two.
    assert by_rating.rounds[0][0].a.name == "A"
    assert as_listed.rounds[0][0].a.name == "D"


def test_a_random_draw_is_the_same_draw_every_time():
    """Stored as entrants and rebuilt on load, so a draw that reshuffled would
    not be the same tournament after a restart."""
    people = [Participant(n) for n in "ABCDEFGH"]
    first = Tournament("Cup", list(people), seeding="random")
    again = Tournament("Cup", list(people), seeding="random")
    assert [p.name for p in first._seeded()] == [p.name for p in again._seeded()]
    # And it is actually a draw rather than the listed order.
    plain = Tournament("Cup", list(people), seeding="listed")
    assert [p.name for p in first._seeded()] != [p.name for p in plain._seeded()]


def test_a_different_draw_for_a_different_tournament():
    people = [Participant(n) for n in "ABCDEFGH"]
    a = Tournament("Spring", list(people), seeding="random")
    b = Tournament("Autumn", list(people), seeding="random")
    assert [p.name for p in a._seeded()] != [p.name for p in b._seeded()]


def test_the_host_can_override_the_modes_series_length():
    t = Tournament("Cup", [Participant("A"), Participant("B")], best_of=3)
    assert t.mode.best_of == 5 and t.series_length() == 3
    try:
        t.report("R1M1", "A", (3, 0))
    except ValueError:
        raise AssertionError("a 3-0 was refused for a Bo3")
    t2 = Tournament("Cup", [Participant("A"), Participant("B")], best_of=7)
    try:
        t2.report("R1M1", "A", (3, 0))
    except ValueError as e:
        assert "Bo7" in str(e), str(e)
    else:
        raise AssertionError("a 3-0 decided a Bo7")


# ------------------------------------------------------------------ Renaming
def test_an_entrant_can_be_renamed_before_anything_is_reported():
    """A typo is the commonest thing to fix, and it used to mean building the
    bracket again."""
    t = Tournament("Cup", [Participant("Alcie", 1500), Participant("Bob", 1200)])
    t.rename(0, "Alice")
    assert [p.name for p in t.participants] == ["Alice", "Bob"]
    assert t.match("R1M1").a.name == "Alice"


def test_renaming_stops_once_a_result_exists():
    """The stored results refer to these names: changing one afterwards would
    quietly detach a result from the player who earned it."""
    t = Tournament("Cup", [Participant("A", 1500), Participant("B", 1200)])
    t.report("R1M1", "A", (3, 0))
    try:
        t.rename(0, "Something else")
    except ValueError as e:
        assert "result" in str(e), str(e)
    else:
        raise AssertionError("an entrant was renamed after a reported result")


def test_renaming_to_a_name_already_in_the_bracket_is_refused():
    t = Tournament("Cup", [Participant("A"), Participant("B")])
    try:
        t.rename(0, "B")
    except ValueError as e:
        assert "already" in str(e), str(e)
    else:
        raise AssertionError("two entrants ended up with the same name")


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
