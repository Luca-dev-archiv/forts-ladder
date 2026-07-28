"""Link ladder display names to Steam IDs.

The ranking sheet only knows display names; the recorder only knows Steam
IDs and the Steam persona at the time of the match. Bridging the two is what
makes automatic tracking useful at all.

Three things make this messy, and all three are handled explicitly:

- **Steam names change.** The Steam ID is the identity, the name only an
  alias with an observation date, so `observed` accumulates aliases instead
  of overwriting.
- **Ladder names are often not Steam names.** Many entries are Discord
  handles.
- **Guessing is dangerous.** Short names sit inside longer ones (`Rin` and
  `Rinaldo` can be two different people), and merging them would mix up two
  players' careers. So only exact matches link automatically; anything else
  is a suggestion for a human to confirm.

Every link carries its provenance so a wrong one can be found later rather
than merely suspected.

    python -m ladder.identity setup   # who owns this machine
    python -m ladder.identity scan    # read recorded matches
    python -m ladder.identity match   # link exact hits, suggest the rest
    python -m ladder.identity link "Name" 7656...
    python -m ladder.identity status
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .paths import active_user_dir

REPO = Path(__file__).resolve().parent.parent
IDENTITY_FILE = REPO / "data" / "identity.json"
SEED_FILE = REPO / "data" / "seed" / "ufer.json"
RECORDED_DIRS = (REPO / "data" / "recorded", REPO / "matches" / "recorded")

# Below this length no similarity suggestion is ever made: at three
# characters any distance is meaningless.
MIN_FUZZY_LEN = 5
FUZZY_THRESHOLD = 0.86


def normalize(name: str) -> str:
    """Normalise for comparison without destroying information.

    NFKC, casefold, whitespace — deliberately *not* stripping diacritics or
    transliterating non-ASCII. Cyrillic names appear verbatim in the ranking
    and belong to distinct players; transliterating would merge two of them.
    """
    return unicodedata.normalize("NFKC", str(name)).strip().casefold()


@dataclass
class Link:
    ufer_name: str
    steam_id: str
    method: str                  # "exact" | "manual" | "fuzzy-confirmed"
    confirmed: bool
    evidence: str = ""
    updated: str = ""

    def to_dict(self) -> dict:
        return {"ufer_name": self.ufer_name, "steam_id": self.steam_id,
                "method": self.method, "confirmed": self.confirmed,
                "evidence": self.evidence, "updated": self.updated}


@dataclass
class Observed:
    steam_id: str
    names: list[str] = field(default_factory=list)
    matches: int = 0
    last_seen: str = ""

    def note(self, name: str, when: str) -> None:
        if name and name not in self.names:
            self.names.append(name)
        self.matches += 1
        if when > self.last_seen:
            self.last_seen = when

    def to_dict(self) -> dict:
        return {"steam_id": self.steam_id, "names": self.names,
                "matches": self.matches, "last_seen": self.last_seen}


class Registry:
    def __init__(self) -> None:
        self.links: list[Link] = []
        self.observed: dict[str, Observed] = {}
        self.suggestions: list[dict] = []
        # First-run dialog state for this machine, so the recorder does not
        # ask again on every start.
        self.local: dict = {}

    # ----------------------------------------------------------- Persistence
    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        # Resolved at call time on purpose. As a default argument the
        # constant freezes at import, so a test that redirects IDENTITY_FILE
        # still writes to the real file — which is exactly what happened.
        path = path or IDENTITY_FILE
        r = cls()
        if not path.exists():
            return r
        raw = json.loads(path.read_text(encoding="utf-8"))
        r.links = [Link(**l) for l in raw.get("links", [])]
        for o in raw.get("observed", []):
            r.observed[o["steam_id"]] = Observed(**o)
        r.suggestions = raw.get("suggestions", [])
        r.local = raw.get("local", {})
        return r

    def save(self, path: Path | None = None) -> None:
        path = path or IDENTITY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "note": ("The Steam ID is the identity, the name only an alias. "
                     "Only exact matches are linked automatically."),
            "links": [l.to_dict() for l in self.links],
            "observed": [o.to_dict() for o in sorted(
                self.observed.values(), key=lambda o: -o.matches)],
            "suggestions": self.suggestions,
            "local": self.local,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    # --------------------------------------------------------------- Lookups
    def steam_ids_for(self, ufer_name: str) -> list[str]:
        """Several are allowed — alt accounts are common in small scenes."""
        key = normalize(ufer_name)
        return [l.steam_id for l in self.links
                if normalize(l.ufer_name) == key and l.confirmed]

    def ufer_name_for(self, steam_id: str) -> str | None:
        for l in self.links:
            if l.steam_id == str(steam_id) and l.confirmed:
                return l.ufer_name
        return None

    def conflicts(self) -> list[str]:
        """One Steam ID with two ladder names is an error, not an alt."""
        by_id: dict[str, set[str]] = {}
        for l in self.links:
            if l.confirmed:
                by_id.setdefault(l.steam_id, set()).add(l.ufer_name)
        return [f"SteamID {sid} is linked to {sorted(names)}"
                for sid, names in by_id.items() if len(names) > 1]

    # --------------------------------------------------------------- Editing
    def add_link(self, ufer_name: str, steam_id: str, method: str,
                 evidence: str = "", confirmed: bool = True) -> bool:
        steam_id = str(steam_id)
        for l in self.links:
            if l.steam_id == steam_id and normalize(l.ufer_name) == normalize(ufer_name):
                # Never weaken an existing confirmation from an automatic run.
                l.confirmed = l.confirmed or confirmed
                return False
        self.links.append(Link(
            ufer_name=ufer_name, steam_id=steam_id, method=method,
            confirmed=confirmed, evidence=evidence,
            updated=time.strftime("%Y-%m-%d")))
        return True

    def remove_link(self, steam_id: str) -> int:
        before = len(self.links)
        self.links = [l for l in self.links if l.steam_id != str(steam_id)]
        return before - len(self.links)


def scan_recorded(reg: Registry, dirs=None) -> int:
    """Collect every (name, Steam ID) pair from the recorded matches."""
    dirs = dirs if dirs is not None else RECORDED_DIRS
    seen_files = 0
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                d_ = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            seen_files += 1
            when = d_.get("played_at", "")
            for pl in d_.get("players", []):
                sid = pl.get("steam_id")
                if not sid:
                    continue
                obs = reg.observed.setdefault(sid, Observed(steam_id=sid))
                obs.note(pl.get("name", ""), when)
    return seen_files


def load_ufer_names(seed: Path = SEED_FILE) -> list[str]:
    if not seed.exists():
        return []
    raw = json.loads(seed.read_text(encoding="utf-8"))
    return [p["name"] for p in raw.get("players", [])]


RE_LOGIN = re.compile(r"Logged into Steam as (.+?) \((\d{17})\)")


def read_persona(user_dir: Path) -> str | None:
    """Steam persona name from this account's log."""
    log = user_dir / "log.txt"
    if not log.exists():
        return None
    try:
        text = log.read_bytes()[:200_000].decode("utf-16-le", errors="replace")
    except OSError:
        return None
    hits = RE_LOGIN.findall(text)
    return hits[-1][0] if hits else None


def detect_local() -> tuple[str, str | None] | None:
    """(SteamID64, persona) of the most recently active account.

    The ID comes from the directory name and survives a cleared log; the
    persona only appears if the log still holds a login line.
    """
    d = active_user_dir()
    if d is None:
        return None
    return d.name, read_persona(d)


class ImpersonationRefused(Exception):
    """The claimed name already belongs to a different Steam ID."""


def self_declare(reg: Registry, steam_id: str, ufer_name: str,
                 force: bool = False) -> Link:
    """Record a self-declared link, guarding against claiming other names.

    A self-declaration is a *claim*, not proof — on your own machine anyone
    can say they are the top-ranked player. That is fine for labelling your
    own matches, but a server accepting results must not treat
    `method="self-declared"` as identity; it needs one confirmation, from an
    admin or via Discord login.

    What is prevented regardless: claiming a name that demonstrably belongs
    to another Steam ID. That is the case where a mix-up gets expensive.
    """
    steam_id = str(steam_id)
    owner_ids = reg.steam_ids_for(ufer_name)
    if owner_ids and steam_id not in owner_ids and not force:
        raise ImpersonationRefused(
            f"{ufer_name!r} is already linked to {', '.join(owner_ids)}. "
            "If this is your alt account, an admin can record it — "
            "deliberately not from here.")
    existing = reg.ufer_name_for(steam_id)
    if existing and normalize(existing) != normalize(ufer_name):
        reg.remove_link(steam_id)
    reg.add_link(ufer_name, steam_id, "self-declared",
                 evidence="first-run dialog on this machine")
    return [l for l in reg.links if l.steam_id == steam_id][-1]


def match_names(reg: Registry, ufer_names: list[str]) -> tuple[int, list[dict]]:
    """Link exact hits, only suggest similar ones.

    Returns (number of new links, suggestions).
    """
    by_norm: dict[str, str] = {}
    ambiguous: set[str] = set()
    for n in ufer_names:
        k = normalize(n)
        if k in by_norm and by_norm[k] != n:
            # Two ranking entries differing only in case: "exact" is no
            # longer unambiguous.
            ambiguous.add(k)
        by_norm[k] = n

    linked = 0
    suggestions: list[dict] = []

    for sid, obs in reg.observed.items():
        if reg.ufer_name_for(sid):
            continue                              # already confirmed
        hit = None
        for alias in obs.names:
            k = normalize(alias)
            if k in ambiguous:
                suggestions.append({
                    "steam_id": sid, "steam_name": alias,
                    "ufer_name": by_norm[k], "score": 1.0,
                    "reason": "several ranking entries differ only in case "
                              "— decide by hand"})
                continue
            if k in by_norm:
                hit = (by_norm[k], alias)
                break
        if hit:
            if reg.add_link(hit[0], sid, "exact",
                            evidence=f"Steam-Name {hit[1]!r} in "
                                     f"{obs.matches} Match(es)"):
                linked += 1
            continue

        # No exact hit: fall back to similarity, but only as a suggestion
        # and only for names long enough to be meaningful.
        for alias in obs.names:
            k = normalize(alias)
            if len(k) < MIN_FUZZY_LEN:
                continue
            near = difflib.get_close_matches(k, [x for x in by_norm
                                                 if len(x) >= MIN_FUZZY_LEN],
                                             n=3, cutoff=FUZZY_THRESHOLD)
            for cand in near:
                suggestions.append({
                    "steam_id": sid, "steam_name": alias,
                    "ufer_name": by_norm[cand],
                    "score": round(difflib.SequenceMatcher(
                        None, k, cand).ratio(), 3),
                    "reason": "similarity — MUST be confirmed (short names "
                              "sit inside longer ones)"})

    # Store suggestions deduplicated and in a stable order.
    key = lambda s: (s["steam_id"], s["ufer_name"])          # noqa: E731
    merged = {key(s): s for s in reg.suggestions}
    merged.update({key(s): s for s in suggestions})
    reg.suggestions = sorted(merged.values(),
                             key=lambda s: (-s["score"], s["ufer_name"]))
    return linked, reg.suggestions


def ensure_local_identity(force: bool = False,
                          interactive: bool | None = None) -> str | None:
    """Ask once who owns this machine. Returns the ladder name.

    Called by the recorder before it starts following the log. Three rules:

    - **Ask once.** A "no" is remembered too, or the tool nags on every start
      and gets switched off.
    - **Never block.** Running as a service there is no terminal, so the
      dialog is skipped with a hint rather than waiting for input that will
      never come.
    - **Record without it.** Match files contain Steam IDs anyway; the ladder
      name is a convenience, not a precondition.
    """
    reg = Registry.load()
    local = detect_local()
    if local is None:
        print("No Forts accounts found -- skipping the first-run dialog.")
        return None
    steam_id, persona = local

    known = reg.ufer_name_for(steam_id)
    if known and not force:
        return known
    if reg.local.get("skip") and reg.local.get("steam_id") == steam_id and not force:
        return None

    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    if not interactive:
        print(f"Account {steam_id} ({persona or 'name unknown'}) is not "
              "linked to a ladder name.")
        print("No terminal for the prompt — do it later with:")
        print("    python -m ladder.identity setup")
        return None

    names = load_ufer_names()
    print("\n" + "=" * 62)
    print("  Who owns this machine?")
    print("=" * 62)
    print(f"  Steam-Account : {steam_id}")
    print(f"  Steam name    : {persona or '(not found in the log)'}")
    if not names:
        print("\n  No ladder name list available — only the Steam ID is "
              "stored.")
    print("\n  The ranking only knows display names, the game only Steam IDs.")
    print("  This one answer connects them. It stays on your machine until")
    print("  you report a result.\n")

    by_norm = {normalize(n): n for n in names}
    exact = by_norm.get(normalize(persona or ""))

    # Letters, not digits. Which options exist depends on whether the Steam
    # name happens to be in the ranking, so numbering would shift meaning
    # between machines — during the first dry run that turned "other name"
    # into "not on the ranking".
    options: list[tuple[str, str, object]] = []
    if exact:
        options.append(("y", f'yes, my ladder name is also "{exact}"', exact))
    options.append(("n", "enter a different ladder name", None))
    options.append(("x", "I am not on the ranking", "SKIP"))
    options.append(("l", "ask me later", "LATER"))
    for key, label, _ in options:
        print(f"    [{key}] {label}")

    default = "y" if exact else "l"
    try:
        choice = input(f"\n  Choice [{default}]: ").strip().lower() or default
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled — later: python -m ladder.identity setup")
        return None

    valid = {k for k, _, _ in options}
    if choice not in valid:
        print(f"  \"{choice}\" is not one of the options "
              f"({', '.join(sorted(valid))}) — nothing saved.")
        return None
    picked = next(v for k, _, v in options if k == choice)
    if picked == "LATER":
        print("  Fine. Later with:  python -m ladder.identity setup")
        return None
    if picked == "SKIP":
        reg.local = {"steam_id": steam_id, "skip": True,
                     "asked": time.strftime("%Y-%m-%d")}
        reg.save()
        print("  Noted. You will not be asked again; matches are still "
              "recorded.")
        return None

    chosen = picked
    if chosen is None:
        try:
            chosen = input("  Ladder name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not chosen:
            return None
        if names and normalize(chosen) not in by_norm:
            near = difflib.get_close_matches(normalize(chosen), list(by_norm),
                                             n=5, cutoff=0.7)
            print(f"\n  \"{chosen}\" is not on the ranking.")
            if near:
                print("  Similar entries:")
                for n in near:
                    print(f"      {by_norm[n]}")
            try:
                if input("  Save it anyway? [y/N] ").strip().lower() \
                        not in ("y", "yes"):
                    print("  Nothing saved.")
                    return None
            except (EOFError, KeyboardInterrupt):
                return None
        elif names:
            chosen = by_norm[normalize(chosen)]

    try:
        self_declare(reg, steam_id, chosen)
    except ImpersonationRefused as e:
        print(f"\n  Not saved: {e}")
        return None
    reg.local = {"steam_id": steam_id, "ufer_name": chosen,
                 "asked": time.strftime("%Y-%m-%d")}
    reg.save()
    print(f"\n  Saved: {chosen} <-> {steam_id}")
    print("  Change it with:  python -m ladder.identity setup\n")
    return chosen


def cmd_setup(args) -> int:
    ensure_local_identity(force=True)
    return 0


def cmd_scan(args) -> int:
    reg = Registry.load()
    n = scan_recorded(reg)
    reg.save()
    print(f"{n} Matchdatei(en) gelesen, {len(reg.observed)} SteamID(s) bekannt")
    for o in sorted(reg.observed.values(), key=lambda o: -o.matches)[:15]:
        name = reg.ufer_name_for(o.steam_id)
        tag = f"  -> UFER: {name}" if name else ""
        print(f"   {o.steam_id}  {', '.join(o.names):<28} {o.matches:>3} Match(es){tag}")
    return 0


def cmd_match(args) -> int:
    reg = Registry.load()
    names = load_ufer_names()
    if not names:
        print(f"No seed data at {SEED_FILE}.")
        print("Run first:  python -m ladder.ufer_import <ranking.xlsx>")
        return 1
    scan_recorded(reg)
    linked, suggestions = match_names(reg, names)
    reg.save()
    print(f"{len(names)} ladder names, {len(reg.observed)} observed Steam IDs")
    print(f"{linked} new exact link(s)")
    if suggestions:
        print(f"\n{len(suggestions)} suggestion(s) -- NOT applied "
              f"automatically:")
        for s in suggestions[:20]:
            print(f"   {s['score']:.2f}  {s['steam_name']!r} "
                  f"=? {s['ufer_name']!r}  ({s['steam_id']})")
            print(f"         {s['reason']}")
        print("\nConfirm with:  python -m ladder.identity link "
              "\"<ladder name>\" <SteamID>")
    for c in reg.conflicts():
        print(f"CONFLICT: {c}")
    return 0


def cmd_link(args) -> int:
    reg = Registry.load()
    if not args.steam_id.isdigit() or len(args.steam_id) != 17:
        print(f"'{args.steam_id}' does not look like a SteamID64 "
              "(17 digits).")
        return 1
    existing = reg.ufer_name_for(args.steam_id)
    if existing and normalize(existing) != normalize(args.ufer_name):
        print(f"This Steam ID is already linked to {existing!r}.")
        print("Unlink first:  python -m ladder.identity unlink "
              f"{args.steam_id}")
        return 1
    created = reg.add_link(args.ufer_name, args.steam_id, "manual",
                           evidence=args.note or "confirmed by hand")
    reg.suggestions = [s for s in reg.suggestions
                       if s["steam_id"] != args.steam_id]
    reg.save()
    others = reg.steam_ids_for(args.ufer_name)
    print(("linked" if created else "already present")
          + f": {args.ufer_name!r} <-> {args.steam_id}")
    if len(others) > 1:
        print(f"Note: {args.ufer_name!r} now has {len(others)} Steam IDs "
              f"({', '.join(others)}) — an alt account? If not, unlink one.")
    return 0


def cmd_unlink(args) -> int:
    reg = Registry.load()
    n = reg.remove_link(args.steam_id)
    reg.save()
    print(f"{n} link(s) removed")
    return 0


def cmd_who(args) -> int:
    reg = Registry.load()
    sid = args.steam_id
    name = reg.ufer_name_for(sid)
    obs = reg.observed.get(sid)
    print(f"SteamID {sid}")
    print(f"  Ladder name: {name or '(not linked)'}")
    if obs:
        print(f"  Steam names: {', '.join(obs.names)}")
        print(f"  Matches    : {obs.matches}, last {obs.last_seen or '?'}")
    else:
        print("  not seen in any recorded match")
    return 0


def cmd_status(args) -> int:
    reg = Registry.load()
    names = load_ufer_names()
    scan_recorded(reg)
    confirmed = {l.steam_id for l in reg.links if l.confirmed}
    mapped_names = {normalize(l.ufer_name) for l in reg.links if l.confirmed}
    unmapped_ids = [o for sid, o in reg.observed.items() if sid not in confirmed]

    print(f"ladder names          : {len(names)}")
    print(f"  of those, linked    : {len(mapped_names)}")
    print(f"observed Steam IDs    : {len(reg.observed)}")
    print(f"  of those, unlinked  : {len(unmapped_ids)}")
    print(f"open suggestions      : {len(reg.suggestions)}")
    for c in reg.conflicts():
        print(f"CONFLICT: {c}")
    if unmapped_ids:
        print("\nUnlinked players (by match count):")
        for o in sorted(unmapped_ids, key=lambda o: -o.matches)[:15]:
            print(f"   {o.steam_id}  {', '.join(o.names):<28} "
                  f"{o.matches:>3} match(es)")
        print("\nThese are usually players who are not on the ranking — "
              "not an error.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="first-run dialog: who owns this machine")
    sub.add_parser("scan", help="Matchdateien auswerten")
    sub.add_parser("match", help="link exact hits, suggest the rest")
    sub.add_parser("status", help="show coverage")
    p = sub.add_parser("link", help="confirm a link by hand")
    p.add_argument("ufer_name")
    p.add_argument("steam_id")
    p.add_argument("--note", help="reason, for traceability")
    p = sub.add_parser("unlink", help="remove a link")
    p.add_argument("steam_id")
    p = sub.add_parser("who", help="wer steckt hinter einer SteamID")
    p.add_argument("steam_id")
    args = ap.parse_args()

    return {"setup": cmd_setup, "scan": cmd_scan, "match": cmd_match,
            "status": cmd_status,
            "link": cmd_link, "unlink": cmd_unlink,
            "who": cmd_who}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
