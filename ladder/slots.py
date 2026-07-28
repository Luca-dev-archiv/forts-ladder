"""Lobby slots: who plays, who spectates, who stays out.

Forts allows **nine clients, and spectators count towards that limit** —
both verified in game: a ninth client connects, a tenth is refused. A
community mod that raises the host screen's cap also stops at nine, which is
independent confirmation that the number is the game's, not the menu's.

Consequences: a 4v4 with a dedicated host leaves *zero* commentator slots,
so the host should be the caster. A 1v1 leaves seven.

Who may enter follows the league rules: admins are always allowed to
observe, everyone else needs both sides to agree. Every refusal carries its
reason so nobody has to guess why they are out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .modes import Mode

#: Hard limit in the game itself, not a setting anyone can raise.
MAX_CLIENTS = 9


class Role(IntEnum):
    """Roles in the order they receive a slot; lower value wins.

    The host holds the lobby together, without players there is no match,
    without an admin no arbitration — a second caster is nice but optional.
    """
    HOST = 0
    PLAYER = 1
    ADMIN = 2
    CASTER = 3
    CO_CASTER = 4
    GUEST = 5

    @property
    def label(self) -> str:
        return {
            Role.HOST: "host", Role.PLAYER: "player", Role.ADMIN: "admin",
            Role.CASTER: "caster", Role.CO_CASTER: "co-caster",
            Role.GUEST: "guest",
        }[self]

    @property
    def is_observer(self) -> bool:
        return self >= Role.ADMIN


@dataclass
class Applicant:
    """Someone who wants into the lobby."""
    name: str
    role: Role
    steam_id: str | None = None
    #: Consent from both sides; irrelevant for admins.
    approved_by: set[str] = field(default_factory=set)
    #: A host only takes a fort if they actually play.
    plays: bool = False

    @property
    def occupies_player_slot(self) -> bool:
        """Does this client need a fort, or only a slot?

        The role alone is not enough: a host who does not play occupies a
        client slot but no player slot. Confusing the two throws the eighth
        player out of a 4v4 — which is what happened on the first run.
        """
        if self.role.is_observer:
            return False
        if self.role is Role.HOST:
            return self.plays
        return True


@dataclass
class Decision:
    applicant: Applicant
    admitted: bool
    reason: str


@dataclass
class SlotPlan:
    mode: Mode
    decisions: list[Decision]
    #: Value for `MaxPlayers` — players *and* spectators.
    max_players: int
    dedicated_host: bool

    @property
    def admitted(self) -> list[Applicant]:
        return [d.applicant for d in self.decisions if d.admitted]

    @property
    def rejected(self) -> list[Decision]:
        return [d for d in self.decisions if not d.admitted]

    @property
    def players(self) -> list[Applicant]:
        return [a for a in self.admitted if a.occupies_player_slot]

    @property
    def observers(self) -> list[Applicant]:
        return [a for a in self.admitted if not a.occupies_player_slot]

    @property
    def free(self) -> int:
        return MAX_CLIENTS - len(self.admitted)

    def lobby_settings(self, server_name: str, password: str) -> dict:
        """Values for `users/<steamid>/multiplayer.lua`.

        `MaxPlayers` is the total client count, not the player count. This is
        where tournaments go wrong: eight players entered, and the caster
        cannot get in.
        """
        return {
            "MaxPlayers": self.max_players,
            "ArtificialHostLag": True,      # required: no host latency edge
            "CoopOnElimination": self.mode.team_size > 1 and not self.mode.coop,
            "TeamsUnlocked": False,
            "FortsUnlocked": False,
            "ServerName": server_name,
            "Password": password,
        }

    def summary(self) -> str:
        lines = [f"{self.mode.label}: {len(self.players)} players, "
                 f"{len(self.observers)} spectators, {self.free} slot(s) free",
                 f"MaxPlayers = {self.max_players} of at most {MAX_CLIENTS}"]
        if self.dedicated_host:
            lines.append("host does not play (takes no fort)")
        for d in self.decisions:
            mark = "+" if d.admitted else "-"
            lines.append(f"  {mark} {d.applicant.name:<20} "
                         f"{d.applicant.role.label:<16} {d.reason}")
        return "\n".join(lines)


def plan_slots(mode: Mode, applicants: list[Applicant],
               require_consent: bool = True) -> SlotPlan:
    """Assign slots, refusing with a reason instead of silently trimming.

    `require_consent` models the rule that spectators need both sides to
    agree, admins excepted. It can be switched off for open lobbies.
    """
    needed_players = mode.players_per_match
    sides = {"A", "B"}

    # By role, then by signup order — a stable order keeps the assignment
    # explainable.
    ordered = sorted(enumerate(applicants), key=lambda p: (p[1].role, p[0]))

    decisions: list[Decision] = []
    taken = 0
    players_taken = 0

    for _, a in ordered:
        if a.role.is_observer and require_consent and a.role is not Role.ADMIN:
            missing = sides - a.approved_by
            if missing:
                decisions.append(Decision(a, False,
                    "consent missing from side " + ", ".join(sorted(missing))))
                continue

        if a.occupies_player_slot and players_taken >= needed_players:
            decisions.append(Decision(a, False,
                f"{mode.label} needs {needed_players} players, "
                f"and those are taken"))
            continue

        if taken >= MAX_CLIENTS:
            decisions.append(Decision(a, False,
                f"lobby full — Forts allows at most {MAX_CLIENTS} clients, "
                "spectators included"))
            continue

        taken += 1
        if a.occupies_player_slot:
            players_taken += 1
        note = ("plays" if a.occupies_player_slot
                else "hosts, does not play" if a.role is Role.HOST
                else "admin, no consent needed" if a.role is Role.ADMIN
                else "both sides agreed")
        decisions.append(Decision(a, True, note))

    host = next((a for a in applicants if a.role is Role.HOST), None)
    dedicated = host is not None and not host.plays

    # Only occupied slots are announced: padding `MaxPlayers` opens the lobby
    # to strangers, password or not.
    return SlotPlan(mode=mode, decisions=decisions, max_players=max(taken, 2),
                    dedicated_host=dedicated)


def observer_capacity(mode: Mode, dedicated_host: bool = False) -> int:
    """How many spectators fit alongside the players?

    4v4 with a playing host leaves one slot; with a dedicated host the host
    *is* the spectator and none remain.
    """
    used = mode.players_per_match + (1 if dedicated_host else 0)
    return max(0, MAX_CLIENTS - used)
