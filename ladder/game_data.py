"""Read the ranked map rotation for every season from the game files.

The pools live in `data/db/constants.lua`, including seasons that have not
started yet — the table ships with the patch rather than coming from a
server. So the next season's pool is known before it begins.

The comments after each map name list every season it appeared in, which
gives the full rotation history for free.

    python -m ladder.game_data
    python -m ladder.game_data --json pools.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

FORTS_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Forts"
CONSTANTS = os.path.join(FORTS_DIR, "data", "db", "constants.lua")


def extract_pools(path: str) -> tuple[dict[int, list[str]], dict[str, list[int]]]:
    text = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"^RankedMaps\s*=\s*\{", text, re.M)
    if not m:
        sys.exit("RankedMaps table not found — did a patch change the format?")
    body = text[m.end():]

    pools: dict[int, list[str]] = {}
    history: dict[str, list[int]] = {}
    for block in re.finditer(r"\[(\d+)\]\s*=\s*\{(.*?)\n\t\}", body, re.S):
        season = int(block.group(1))
        maps: list[str] = []
        for line in block.group(2).splitlines():
            line = line.strip()
            # Commented-out maps are out of the pool but still carry their
            # history, so they are parsed for the rotation.
            commented = line.startswith("--")
            mm = re.match(r'(?:--)?\s*"([^"]+)"\s*,?\s*(?:--\s*(.*))?$', line)
            if not mm:
                continue
            name, hist = mm.group(1), mm.group(2)
            if not commented:
                maps.append(name)
            if hist:
                seasons = [int(x) for x in re.findall(r"\d+", hist)]
                prev = history.setdefault(name, [])
                for s in seasons:
                    if s not in prev:
                        prev.append(s)
        if maps:
            pools[season] = maps
    for v in history.values():
        v.sort()
    return pools, history


def current_season() -> int | None:
    """Which season did the game last report in the log?"""
    root = os.path.join(FORTS_DIR, "users")
    if not os.path.isdir(root):
        return None
    best = None
    for d in os.listdir(root):
        p = os.path.join(root, d, "log.txt")
        if not os.path.exists(p):
            continue
        text = open(p, "rb").read().decode("utf-16-le", errors="replace")
        for m in re.finditer(r"(?:Setting current leaderboard season to|"
                             r"detected new season)\s+(\d+)", text):
            s = int(m.group(1))
            best = s if best is None else max(best, s)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the result to this file as JSON")
    args = ap.parse_args()

    pools, history = extract_pools(CONSTANTS)
    live = current_season()

    print(f"Seasons in the game files: {min(pools)}-{max(pools)}")
    if live:
        print(f"Latest season seen in the log: {live}")
    print()
    for season in sorted(pools):
        if season < max(pools) - 5:
            continue
        tag = ""
        if live and season == live:
            tag = "   <== running"
        elif live and season == live + 1:
            tag = "   <== up next"
        print(f"Season {season}{tag}")
        for name in pools[season]:
            print(f"   - {name}")
        print()

    print("Rotation frequency (seasons in the pool, across all entries):")
    for name, seasons in sorted(history.items(), key=lambda kv: -len(kv[1])):
        print(f"   {name:24s} {len(seasons):3d}x   latest: {seasons[-3:]}")

    if args.json:
        json.dump({"pools": {str(k): v for k, v in pools.items()},
                   "history": history,
                   "current_season_from_log": live},
                  open(args.json, "w"), indent=2)
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
