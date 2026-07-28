"""Tests for the name mapping — above all for what it does NOT do.

The most expensive fault in this module is not a missing link but a wrong
one: it merges two players' careers into one ranking entry, and that often
only surfaces months later. So these tests mostly pin down the limits of
automatic matching.

No test writes to `data/identity.json` — they all work on in-memory
registry objects.
"""

import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder.identity import (  # noqa: E402
    FUZZY_THRESHOLD, MIN_FUZZY_LEN, ImpersonationRefused, Observed, Registry,
    ensure_local_identity, match_names, normalize, self_declare,
)

SEED = Path(__file__).resolve().parent.parent / "data" / "seed" / "ufer.json"


def _registry_with(observed: dict[str, list[str]]) -> Registry:
    reg = Registry()
    for sid, names in observed.items():
        o = Observed(steam_id=sid)
        for n in names:
            o.note(n, "2026-07-01")
        reg.observed[sid] = o
    return reg


def test_normalize_keeps_scripts_distinct():
    """No transliteration: "Игрок" appears exactly like that."""
    assert normalize("  TopSeed ") == "topseed"
    assert normalize("Игрок") == "игрок"
    assert normalize("Игрок") != normalize("Igrok")
    # NFKC folds compatibility characters without changing meaning.
    assert normalize("ﬁle") == normalize("file")


def test_exact_match_links_automatically():
    reg = _registry_with({"76561199000000001": ["TopSeed"]})
    linked, suggestions = match_names(reg, ["TopSeed", "ThirdSeed"])
    assert linked == 1
    assert reg.ufer_name_for("76561199000000001") == "TopSeed"
    assert suggestions == []


def test_case_difference_still_counts_as_exact():
    reg = _registry_with({"76561199000000002": ["TOPSEED"]})
    linked, _ = match_names(reg, ["TopSeed"])
    assert linked == 1


def test_short_names_are_never_fuzzy_matched():
    """The Rin/Rinaldo case: two real, different players.

    "Rin" appears as an opponent name in the rule set, "SecondSeed" at rank
    5 of the ranking. A similarity rule that allows short names merges pairs
    like these — which is why it only applies from MIN_FUZZY_LEN characters.
    """
    reg = _registry_with({"76561199000000003": ["Rin"]})
    linked, suggestions = match_names(reg, ["SecondSeed", "Rinaldo"])
    assert linked == 0
    assert suggestions == [], f"a short name was suggested: {suggestions}"
    assert MIN_FUZZY_LEN >= 5


def test_similar_names_are_suggested_but_not_linked():
    reg = _registry_with({"76561199000000004": ["FourthSeedd"]})
    linked, suggestions = match_names(reg, ["FourthSeed"])
    assert linked == 0, "similarity must never link automatically"
    assert len(suggestions) == 1
    assert suggestions[0]["ufer_name"] == "FourthSeed"
    assert suggestions[0]["score"] >= FUZZY_THRESHOLD
    assert reg.ufer_name_for("76561199000000004") is None


def test_one_steam_id_with_two_ufer_names_is_a_conflict():
    reg = Registry()
    reg.add_link("FifthSeed", "76561199000000005", "manual")
    reg.add_link("SixthSeed", "76561199000000005", "manual")
    assert reg.conflicts(), "two names on one SteamID must be flagged"


def test_two_steam_ids_for_one_name_are_allowed():
    """Alt accounts are normal in a small scene, not an error."""
    reg = Registry()
    reg.add_link("SeventhSeed", "76561199000000006", "manual")
    reg.add_link("SeventhSeed", "76561199000000007", "manual")
    assert not reg.conflicts()
    assert len(reg.steam_ids_for("SeventhSeed")) == 2


def test_persona_rename_keeps_the_link_and_adds_an_alias():
    """The Steam ID is the identity; the name is only an alias."""
    reg = _registry_with({"76561199000000008": ["TopSeed"]})
    match_names(reg, ["TopSeed"])
    reg.observed["76561199000000008"].note("TopSeed_v2", "2026-08-01")
    match_names(reg, ["TopSeed"])
    assert reg.ufer_name_for("76561199000000008") == "TopSeed"
    assert "TopSeed_v2" in reg.observed["76561199000000008"].names


def test_automatic_run_never_downgrades_a_manual_confirmation():
    reg = _registry_with({"76561199000000009": ["TopSeed"]})
    reg.add_link("TopSeed", "76561199000000009", "manual", confirmed=True)
    match_names(reg, ["TopSeed"])
    link = [l for l in reg.links if l.steam_id == "76561199000000009"][0]
    assert link.confirmed
    assert link.method == "manual"


def test_no_two_real_ufer_names_collide_at_the_configured_threshold():
    """Regression guard for the parameters, checked against 220 real names.

    At threshold 0.86 and minimum length 5, no ranking name matches another.
    Lower the values and this test fails — which is exactly what it is for,
    before two players get merged.
    """
    if not SEED.exists():
        print("      (skipped: no data/seed/ufer.json)")
        return
    import difflib
    names = [p["name"] for p in
             json.loads(SEED.read_text(encoding="utf-8"))["players"]]
    norm = [unicodedata.normalize("NFKC", n).strip().casefold() for n in names]
    long = [n for n in norm if len(n) >= MIN_FUZZY_LEN]
    collisions = []
    for i, a in enumerate(long):
        for b in long[i + 1:]:
            if difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY_THRESHOLD:
                collisions.append((a, b))
    assert not collisions, f"colliding names: {collisions[:5]}"


def test_self_declaration_cannot_claim_someone_elses_name():
    """The case where a mix-up gets expensive.

    On your own machine you may claim to be whoever you like — but not take
    over a name that demonstrably belongs to another Steam ID. That is a
    league admin's call, not a dialog's.
    """
    reg = Registry()
    reg.add_link("SecondSeed", "76561199000000010", "manual")
    try:
        self_declare(reg, "76561199000000011", "SecondSeed")
    except ImpersonationRefused:
        pass
    else:
        raise AssertionError("someone else's name was taken over")
    # With force it works — the route for an admin who knows about alts.
    self_declare(reg, "76561199000000011", "SecondSeed", force=True)
    assert len(reg.steam_ids_for("SecondSeed")) == 2


def test_self_declaration_replaces_ones_own_earlier_choice():
    reg = Registry()
    self_declare(reg, "76561199000000012", "Mistyped")
    self_declare(reg, "76561199000000013", "Unrelated")   # a different account
    self_declare(reg, "76561199000000012", "Corrected")
    assert reg.ufer_name_for("76561199000000012") == "Corrected"
    assert reg.steam_ids_for("Mistyped") == []


def test_self_declaration_is_marked_as_a_claim_not_a_proof():
    """A server must not treat `self-declared` as proof of identity."""
    reg = Registry()
    link = self_declare(reg, "76561199000000014", "EighthSeed")
    assert link.method == "self-declared"


def test_dialog_never_blocks_without_a_terminal():
    """Started as a service, the recorder must not wait for input."""
    assert ensure_local_identity(interactive=False) in (None, )


def test_registry_path_is_resolved_at_call_time():
    """Regression: the default was frozen at import time.

    Because of that, a test run redirecting IDENTITY_FILE still wrote the
    real file — which is exactly what happened while building this.
    """
    import tempfile
    from ladder import identity as mod
    tmp = Path(tempfile.mkdtemp()) / "identity.json"
    original = mod.IDENTITY_FILE
    try:
        mod.IDENTITY_FILE = tmp
        reg = Registry()
        reg.add_link("Testspieler", "76561199000000015", "manual")
        reg.save()
        assert tmp.exists(), "save ignored the redirected constant"
        assert Registry.load().ufer_name_for("76561199000000015") == "Testspieler"
    finally:
        mod.IDENTITY_FILE = original


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
