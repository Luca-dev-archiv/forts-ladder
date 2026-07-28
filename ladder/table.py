"""Put both ratings side by side and export them for the UI.

The **league column** is not recomputed. It is taken as-is from the seed
(`data/seed/ufer.json`), because that number belongs to the sheet's
maintainer; we display it and do not touch it.

The **open column** starts from the same value and evolves from our own
recordings. That is the only one we calculate.

Anyone who never played in the open ladder has no value there — and does not
get an invented one.

    python -m ladder.table
    python -m ladder.table --json data/ratings.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .eligibility import Eligibility, consent_filter
from .identity import Registry, load_ufer_names
from .open_ladder import recompute as open_recompute
from .ratings import tier_of
from .report import group_series, load_matches

REPO = Path(__file__).resolve().parent.parent
SEED_FILE = REPO / "data" / "seed" / "ufer.json"
OUT_FILE = REPO / "data" / "ratings.json"


def load_seed() -> dict[str, dict]:
    if not SEED_FILE.exists():
        return {}
    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return {p["name"]: p for p in raw.get("players", [])}


def series_to_events(reg: Registry,
                     elig: Eligibility | None = None) -> list[dict]:
    """Translate recorded series into rating events.

    Anything ambiguous is skipped: series without two sides, without a
    decided game, or with players whose name is not linked. One series fewer
    beats one wrongly attributed — a misplaced win enters the history and is
    never noticed again.

    `elig` applies the same two rules as the report path (ladder-created
    lobby, everyone opted in). Skipped series are listed with their reason
    rather than dropped quietly, so an empty column is explained instead of
    looking broken.
    """
    events: list[dict] = []
    skipped: list[str] = []

    for s in group_series(load_matches()):
        if elig is not None:
            verdict = elig.check_series(s.matches)
            if not verdict:
                skipped.append(f"{s.played_at[:10]}: {'; '.join(verdict.reasons)}")
                continue
        sides = s.sides()
        if len(sides) != 2:
            skipped.append(f"{s.played_at[:10]}: {len(sides)} sides")
            continue
        wins, unclear = s.score()
        if sum(wins.values()) == 0:
            skipped.append(f"{s.played_at[:10]}: no decided game")
            continue

        a_side, b_side = sorted(sides)
        names = {}
        missing = False
        for side in (a_side, b_side):
            resolved = []
            for p in sides[side]:
                n = reg.ufer_name_for(p["steam_id"])
                if n is None:
                    missing = True
                    break
                resolved.append(n)
            names[side] = resolved
        if missing:
            skipped.append(f"{s.played_at[:10]}: player without a linked name")
            continue

        games = wins[a_side] + wins[b_side]
        ev = {"date": s.played_at[:10], "event": "open",
              "event_id": f"{s.played_at}-{a_side}-{b_side}",
              "games": games, "score_a": wins[a_side]}
        if len(names[a_side]) == 1 and len(names[b_side]) == 1:
            ev |= {"kind": "1v1", "a": names[a_side][0], "b": names[b_side][0]}
        else:
            ev |= {"kind": "team", "team_a": names[a_side],
                   "team_b": names[b_side], "coop": False}
        events.append(ev)

    return events, skipped        # type: ignore[return-value]


def build() -> dict:
    reg = Registry.load()
    seed = load_seed()
    elig = Eligibility.load()
    events, skipped = series_to_events(reg, elig)   # type: ignore[misc]

    start = {name: p["rating"] for name, p in seed.items()}
    # Gated twice on purpose. The series check above produces the readable
    # reason; this one is the backstop for events reaching the rating from
    # anywhere else, so consent cannot be bypassed by adding a new source.
    open_res = open_recompute(events, seed=start,
                              allow=consent_filter(elig, reg))

    rows = []
    for name, p in seed.items():
        o = open_res.players.get(name)
        rows.append({
            "name": name,
            "ufer_rating": p["rating"],
            "ufer_title": p.get("title") or tier_of(p["rating"]).title,
            "ufer_rank": p.get("rank"),
            # No value without games played: a number with no matches behind
            # it would be a claim, not a rating.
            "open_rating": round(o.rating, 1) if o and o.rated_games else None,
            "open_title": tier_of(o.rating).title if o and o.rated_games else None,
            "open_games": o.rated_games if o else 0,
            "open_provisional": bool(o and o.rated_games < 10),
            "steam_ids": reg.steam_ids_for(name),
        })
    # Players who only appear in the open ladder, not yet on the sheet.
    for name, o in open_res.players.items():
        if name in seed:
            continue
        rows.append({
            "name": name, "ufer_rating": None, "ufer_title": None,
            "ufer_rank": None, "open_rating": round(o.rating, 1),
            "open_title": tier_of(o.rating).title, "open_games": o.rated_games,
            "open_provisional": o.rated_games < 10,
            "steam_ids": reg.steam_ids_for(name),
        })

    rows.sort(key=lambda r: -(r["ufer_rating"] or r["open_rating"] or 0))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    return {"players": rows, "events_used": len(events),
            "skipped": skipped, "notes": open_res.notes}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", nargs="?", const=str(OUT_FILE),
                    help="write the table as JSON (for the UI)")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    data = build()
    rows = data["players"]

    print(f"{'#':>4}  {'Player':<30} {'UFER':>8}  {'Open':>8}  Games")
    for r in rows[:args.top]:
        # One decimal like the source sheet: 2099 instead of 2099.4 reads as
        # a different number to anyone comparing the two.
        u = f"{r['ufer_rating']:.1f}" if r["ufer_rating"] else "-"
        o = (f"{r['open_rating']:.1f}" + ("*" if r["open_provisional"] else "")
             if r["open_rating"] else "-")
        print(f"{r['rank']:>4}  {r['name']:<30} {u:>8}  {o:>8}  {r['open_games']:>7}")
    print(f"\n{len(rows)} players, {data['events_used']} series counted in "
          f"the open ladder")
    if data["skipped"]:
        print(f"{len(data['skipped'])} series skipped:")
        for s in data["skipped"][:5]:
            print(f"   {s}")
    print("\n* = starting phase (fewer than 10 rated games)")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
