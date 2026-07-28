"""Tests for slot allocation.

The expensive mistake here is a quiet one: eight players entered, MaxPlayers
set to eight — and on tournament night the caster cannot get into the lobby,
because spectators count too. So these tests mostly check the arithmetic at
the boundary.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder import modes  # noqa: E402
from ladder.slots import (  # noqa: E402
    MAX_CLIENTS, Applicant, Role, observer_capacity, plan_slots,
)

BOTH = {"A", "B"}


def players(n: int, plays_host: bool = True) -> list[Applicant]:
    out = [Applicant(f"Player{i}", Role.PLAYER) for i in range(1, n + 1)]
    if plays_host and out:
        out[0] = Applicant("Player1", Role.HOST, plays=True)
    return out


def test_the_hard_limit_is_nine_clients():
    """Hard in the binary: an array of nine. Not a setting."""
    assert MAX_CLIENTS == 9


def test_observers_take_a_player_slot():
    """The core: spectators count, so MaxPlayers has to cover them."""
    plan = plan_slots(modes.RANKED_1V1, [
        *players(2),
        Applicant("Caster", Role.CASTER, approved_by=BOTH),
    ])
    assert len(plan.players) == 2
    assert len(plan.observers) == 1
    assert plan.max_players == 3, "MaxPlayers does not cover the spectator"


def test_a_4v4_leaves_exactly_one_observer_slot():
    mode = modes.RANKED_2V2.with_(key="t4v4", label="4v4", team_size=4)
    assert observer_capacity(mode) == 1
    plan = plan_slots(mode, [
        *players(8),
        Applicant("Caster", Role.CASTER, approved_by=BOTH),
        Applicant("CoCaster", Role.CO_CASTER, approved_by=BOTH),
    ])
    assert len(plan.players) == 8
    assert len(plan.observers) == 1
    assert plan.max_players == 9
    assert any("lobby full" in d.reason for d in plan.rejected)


def test_a_dedicated_host_uses_up_the_last_slot_in_a_4v4():
    """A host who does not play IS the spectator — and that is the last."""
    mode = modes.RANKED_2V2.with_(key="t4v4", label="4v4", team_size=4)
    assert observer_capacity(mode, dedicated_host=True) == 0


def test_a_1v1_has_room_for_a_whole_broadcast_crew():
    plan = plan_slots(modes.RANKED_1V1, [
        *players(2),
        Applicant("Admin", Role.ADMIN),
        Applicant("Caster", Role.CASTER, approved_by=BOTH),
        Applicant("CoCaster", Role.CO_CASTER, approved_by=BOTH),
    ])
    assert len(plan.admitted) == 5
    assert plan.free == 4


def test_admins_need_no_consent():
    """Rule set: "UFER admins are always allowed to observe"."""
    plan = plan_slots(modes.RANKED_1V1,
                      [*players(2), Applicant("Admin", Role.ADMIN)])
    assert len(plan.observers) == 1
    assert "no consent needed" in plan.decisions[-1].reason


def test_a_caster_without_both_sides_agreement_stays_out():
    plan = plan_slots(modes.RANKED_1V1, [
        *players(2),
        Applicant("Caster", Role.CASTER, approved_by={"A"}),
    ])
    assert plan.observers == []
    assert "side B" in plan.rejected[0].reason


def test_consent_can_be_switched_off_for_open_lobbies():
    plan = plan_slots(modes.RANKED_1V1, [
        *players(2), Applicant("Caster", Role.CASTER),
    ], require_consent=False)
    assert len(plan.observers) == 1


def test_surplus_players_are_rejected_with_a_reason():
    plan = plan_slots(modes.RANKED_1V1, players(4))
    assert len(plan.players) == 2
    assert len(plan.rejected) == 2
    assert "needs 2 players" in plan.rejected[0].reason


def test_players_outrank_observers_when_slots_are_scarce():
    """A spectator must never take a player's slot."""
    mode = modes.RANKED_2V2.with_(key="t4v4", label="4v4", team_size=4)
    applicants = [Applicant("Caster", Role.CASTER, approved_by=BOTH)] + players(8)
    plan = plan_slots(mode, applicants)
    assert len(plan.players) == 8, "a player was pushed out"


def test_max_players_is_not_padded_beyond_what_is_used():
    """Setting it larger than needed lets strangers in, password or not."""
    plan = plan_slots(modes.RANKED_1V1, players(2))
    assert plan.max_players == 2


def test_lobby_settings_carry_the_mandatory_rules():
    plan = plan_slots(modes.RANKED_1V1, players(2))
    s = plan.lobby_settings("[LEAGUE] #1", "secret")
    assert s["ArtificialHostLag"] is True      # mandatory rule no. 7
    assert s["TeamsUnlocked"] is False
    assert s["FortsUnlocked"] is False
    assert s["MaxPlayers"] == 2


def test_team_modes_get_coop_on_elimination():
    """Brawl rule 8: "Coop on death enabled" — meaningless in a 1v1."""
    assert plan_slots(modes.RANKED_2V2, players(4)) \
        .lobby_settings("x", "y")["CoopOnElimination"] is True
    assert plan_slots(modes.RANKED_1V1, players(2)) \
        .lobby_settings("x", "y")["CoopOnElimination"] is False


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
