"""Does this match count for the ladder?

Two things were promised publicly about this, so both have to hold in code
rather than in intent:

  1. **Only matches the ladder set up are counted.** An allowlist, not
     "everything except ranked". A blocklist would need a reliable ranked
     marker in the log, and a gap in that detection would silently *include*
     ranked games. An allowlist fails the other way: something we did not
     create is silently ignored, which is the direction the risk should run.
  2. **A result only enters the ladder if every participant opted in.**
     Playing someone who never registered does not rate them and does not
     put their name into anything that leaves this machine.

Withdrawal has to work the same way round: removing consent has to remove
past results too. That is why the rating is always recomputed from the event
list instead of carried forward as a running total — `recompute` simply stops
seeing those events.

Recording is deliberately *not* gated. Reading your own log file is not
collecting someone else's data; publishing a line with their name in it is.
So the gate sits at report, rating and publish, and the recorder keeps
working as before.

    python -m ladder.eligibility status
    python -m ladder.eligibility opt-in <SteamID64>
    python -m ladder.eligibility withdraw <SteamID64>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONSENT_FILE = REPO / "data" / "consent.json"


@dataclass
class Verdict:
    """Why a series does or does not count. Never just a bool."""
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


#: How long an "the next lobby is a ladder lobby" declaration stays valid.
#: Long enough to pick a map and host, short enough that a forgotten arm does
#: not silently sanction whatever gets played an hour later.
ARM_TTL_S = 15 * 60.0


@dataclass
class Participation:
    steam_id: str
    since: str
    source: str = "client"          # client | server | admin


@dataclass
class ArmedIntent:
    """A declaration that the lobby about to be hosted belongs to the ladder.

    Needed because the lobby id does not exist until Steam creates it — it
    only shows up in the log after hosting. So intent is recorded first and
    matched against the id when it appears.
    """
    until: float
    series_id: str | None = None
    source: str = "client"

    def valid(self, now: float) -> bool:
        return now < self.until


class Eligibility:
    """Consent roster plus the lobbies the ladder created."""

    def __init__(self) -> None:
        self.consent: dict[str, Participation] = {}
        #: lobby id -> how it got sanctioned. Provenance is kept rather than a
        #: bare set: "the client said so" and "the server said so" are very
        #: different claims if a result is ever disputed.
        self.sanctioned: dict[int, str] = {}
        self.armed: ArmedIntent | None = None
        #: True once the roster came from the server. Local-only rosters know
        #: about this machine and nothing else, so an unknown Steam ID means
        #: "cannot confirm" rather than "refused" — and the reason says which.
        self.authoritative = False

    # ------------------------------------------------------------ Persistence
    @classmethod
    def load(cls, path: Path | None = None) -> "Eligibility":
        # Resolved at call time, not defaulted in the signature: a module
        # constant frozen at import made an earlier test write the real file.
        path = path or CONSENT_FILE
        e = cls()
        if not path.exists():
            return e
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A broken file must not silently widen the gate. Empty roster
            # means nothing counts, which is the safe direction.
            return e
        for d in raw.get("consent", []):
            if d.get("steam_id"):
                e.consent[d["steam_id"]] = Participation(
                    d["steam_id"], d.get("since", ""), d.get("source", "client"))
        for lid, src in (raw.get("sanctioned_lobbies") or {}).items():
            e.sanctioned[int(lid)] = src or "client"
        armed = raw.get("armed")
        if armed:
            # Persisted so the declaration survives the client restarting
            # between setting up the lobby and actually hosting.
            e.armed = ArmedIntent(float(armed.get("until", 0)),
                                  armed.get("series_id"),
                                  armed.get("source", "client"))
        e.authoritative = bool(raw.get("authoritative", False))
        return e

    def save(self, path: Path | None = None) -> None:
        path = path or CONSENT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "note": ("Who agreed to be tracked, and which lobbies this ladder "
                     "created. Both gate whether a result counts."),
            "authoritative": self.authoritative,
            "consent": [
                {"steam_id": p.steam_id, "since": p.since, "source": p.source}
                for p in sorted(self.consent.values(), key=lambda p: p.steam_id)],
            "sanctioned_lobbies": {str(k): v
                                   for k, v in sorted(self.sanctioned.items())},
            "armed": None if self.armed is None else {
                "until": self.armed.until,
                "series_id": self.armed.series_id,
                "source": self.armed.source,
            },
        }, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- Consent
    def opt_in(self, steam_id: str, source: str = "client") -> bool:
        """Returns False if it was already recorded."""
        if steam_id in self.consent:
            return False
        self.consent[steam_id] = Participation(
            steam_id, time.strftime("%Y-%m-%d"), source)
        return True

    def withdraw(self, steam_id: str) -> bool:
        """Take someone back out. Past results stop counting on the next
        recompute — that is the point, not a side effect."""
        return self.consent.pop(steam_id, None) is not None

    def is_registered(self, steam_id: str) -> bool:
        return steam_id in self.consent

    def unregistered(self, steam_ids) -> list[str]:
        return sorted(s for s in steam_ids if s and s not in self.consent)

    # -------------------------------------------------------------- Allowlist
    def sanction(self, lobby_id: int, source: str = "client") -> None:
        """Mark a lobby as one the ladder created. Only these count."""
        # A server statement outranks a local one and must not be downgraded
        # by a later local call.
        if self.sanctioned.get(int(lobby_id)) == "server" and source != "server":
            return
        self.sanctioned[int(lobby_id)] = source

    def unsanction(self, lobby_id: int) -> None:
        self.sanctioned.pop(int(lobby_id), None)

    def is_sanctioned(self, lobby_id: int | None) -> bool:
        return lobby_id is not None and int(lobby_id) in self.sanctioned

    # ------------------------------------------------------- Arming a lobby
    def arm(self, series_id: str | None = None, ttl_s: float = ARM_TTL_S,
            now: float | None = None, source: str = "client") -> ArmedIntent:
        """Declare that the lobby about to be hosted belongs to the ladder.

        Called when the client sets a lobby up. The id itself arrives later,
        from the log, which is what `observe_lobby` is for.
        """
        now = now if now is not None else time.time()
        self.armed = ArmedIntent(now + ttl_s, series_id, source)
        return self.armed

    def disarm(self) -> None:
        self.armed = None

    def observe_lobby(self, lobby_id: int, now: float | None = None) -> bool:
        """A lobby id showed up in the log. Sanction it if we were armed.

        Returns whether it was sanctioned. Consumed on use: one declaration
        covers one lobby, so an arm cannot quietly collect a whole evening.
        An expired arm is dropped rather than honoured — the whole point of
        the deadline.
        """
        now = now if now is not None else time.time()
        if self.armed is None:
            return False
        if not self.armed.valid(now):
            self.armed = None
            return False
        self.sanction(lobby_id, source=self.armed.source)
        self.armed = None
        return True

    # ------------------------------------------------------------ Server sync
    def sync_from_server(self, payload: dict) -> None:
        """Apply the roster and lobby list the server hands out.

        This is what makes the guest's client agree with the host's: the
        server knows the lobby was set up for a ladder match, the guest's
        machine never saw it being armed. Server statements replace local
        guesses, and the roster becomes authoritative — so an unknown Steam ID
        now genuinely means "did not opt in".
        """
        self.consent = {
            sid: Participation(sid, "", "server")
            for sid in payload.get("steam_ids", []) if sid}
        for lid in payload.get("sanctioned_lobbies", []):
            self.sanction(int(lid), source="server")
        self.authoritative = True

    # ----------------------------------------------------------------- Checks
    def check_match(self, match: dict) -> Verdict:
        reasons: list[str] = []
        lobby = match.get("lobby_id")
        if not self.is_sanctioned(lobby):
            reasons.append(
                "lobby was not set up by the ladder"
                + (f" (lobby {lobby})" if lobby else " (no lobby id in the log)"))
        ids = [p.get("steam_id") for p in match.get("players", [])]
        reasons += self._consent_reasons(ids)
        return Verdict(not reasons, reasons)

    def check_series(self, matches: list[dict]) -> Verdict:
        """A series counts only if every game in it counts.

        Not per game on purpose: half a Bo5 is not a result, and a host crash
        moving play into a new lobby must not turn into a partial series. The
        continuation lobby has to be sanctioned too.
        """
        reasons: list[str] = []
        lobbies = [m.get("lobby_id") for m in matches]
        unsanctioned = [l for l in lobbies if not self.is_sanctioned(l)]
        if unsanctioned:
            named = sorted({str(l) for l in unsanctioned if l})
            reasons.append(
                f"{len(unsanctioned)} of {len(matches)} game(s) were not "
                "played in a lobby the ladder set up"
                + (f": {', '.join(named)}" if named else
                   " (no lobby id in the log)"))
        ids = {p.get("steam_id") for m in matches
               for p in m.get("players", [])}
        reasons += self._consent_reasons(ids)
        return Verdict(not reasons, reasons)

    def _consent_reasons(self, steam_ids) -> list[str]:
        missing = self.unregistered(steam_ids)
        if not missing:
            return []
        if self.authoritative:
            return [f"{len(missing)} participant(s) have not opted in: "
                    f"{', '.join(missing)}"]
        return [f"consent unknown for {len(missing)} participant(s): "
                f"{', '.join(missing)} — refresh the roster from the server"]


def consent_filter(elig: Eligibility, reg) -> "callable":
    """Bridge for `open_ladder.recompute(allow=...)`.

    The rating works with ladder names, consent with Steam IDs. A name counts
    only if at least one Steam ID linked to it opted in — "at least one"
    because alt accounts are normal and a player is one person, not one
    account.

    An unlinked name is refused. Someone whose Steam ID we do not know cannot
    have opted in, so counting them would be guessing about consent.
    """
    def allow(name: str) -> bool:
        ids = reg.steam_ids_for(name)
        return any(elig.is_registered(sid) for sid in ids)
    return allow


# --------------------------------------------------------------------- CLI
def cmd_status(args) -> int:
    e = Eligibility.load()
    print(f"roster           : {len(e.consent)} opted in"
          + ("" if e.authoritative else "  (local only, not authoritative)"))
    print(f"sanctioned lobbies: {len(e.sanctioned)}")
    if e.armed is not None:
        left = e.armed.until - time.time()
        print(f"armed            : "
              + (f"{int(left)}s left" if left > 0 else "EXPIRED")
              + (f", series {e.armed.series_id}" if e.armed.series_id else ""))
    for p in sorted(e.consent.values(), key=lambda p: p.since):
        print(f"   {p.steam_id}  since {p.since or '?'}  ({p.source})")
    if not e.consent:
        print("\nNothing counts yet. Opt in with:")
        print("   python -m ladder.eligibility opt-in <SteamID64>")
    return 0


def cmd_opt_in(args) -> int:
    e = Eligibility.load()
    if not args.steam_id.isdigit() or len(args.steam_id) != 17:
        print(f"{args.steam_id!r} does not look like a SteamID64 (17 digits).")
        return 1
    created = e.opt_in(args.steam_id, source=args.source)
    e.save()
    print(("opted in: " if created else "already recorded: ") + args.steam_id)
    return 0


def cmd_withdraw(args) -> int:
    e = Eligibility.load()
    gone = e.withdraw(args.steam_id)
    e.save()
    print(f"withdrawn: {args.steam_id}" if gone
          else f"{args.steam_id} was not on the roster")
    if gone:
        print("Past results stop counting on the next recompute.")
    return 0


def cmd_sanction(args) -> int:
    e = Eligibility.load()
    e.sanction(args.lobby_id)
    e.save()
    print(f"sanctioned lobby {args.lobby_id}")
    return 0


def cmd_arm(args) -> int:
    e = Eligibility.load()
    intent = e.arm(series_id=args.series, ttl_s=args.ttl)
    e.save()
    mins = int(args.ttl // 60)
    print(f"armed for {mins} min"
          + (f", series {args.series}" if args.series else ""))
    print("Host the lobby now. The recorder has to be running — the lobby id\n"
          "only exists once Steam creates it, and the log is where it appears:")
    print("   python -m ladder.recorder --watch")
    return 0


def cmd_sync(args) -> int:
    """Pull roster and lobby list from the server.

    Without this a guest's client cannot know the host set the lobby up, and
    reports "consent unknown" for everyone.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(args.url.rstrip("/") + "/sync",
                                    timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as exc:                       # pragma: no cover
        print(f"could not reach {args.url}: {exc}")
        return 1
    e = Eligibility.load()
    e.sync_from_server(payload)
    e.save()
    print(f"{len(e.consent)} opted in, {len(e.sanctioned)} sanctioned "
          f"lobbies — roster is now authoritative")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="who is on the roster, what is sanctioned")
    p = sub.add_parser("opt-in", help="record consent to be tracked")
    p.add_argument("steam_id")
    p.add_argument("--source", default="client",
                   choices=["client", "server", "admin"])
    p = sub.add_parser("withdraw", help="remove consent again")
    p.add_argument("steam_id")
    p = sub.add_parser("sanction", help="mark a lobby as ladder-created")
    p.add_argument("lobby_id", type=int)
    p = sub.add_parser("arm", help="the next lobby you host counts")
    p.add_argument("--series", help="series id this lobby belongs to")
    p.add_argument("--ttl", type=float, default=ARM_TTL_S,
                   help=f"seconds the declaration stays valid "
                        f"(default {int(ARM_TTL_S)})")
    p = sub.add_parser("sync", help="pull roster and lobbies from the server")
    p.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()
    return {"status": cmd_status, "opt-in": cmd_opt_in,
            "withdraw": cmd_withdraw, "sanction": cmd_sanction,
            "arm": cmd_arm, "sync": cmd_sync}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
