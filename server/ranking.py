"""Serve the ranking so every client sees the same one.

Until now the ranking view read a file on the player's own machine, which meant
it showed whatever that person happened to have generated. A ladder is only a
ladder if everyone reads the same table, so it is served.

**Behind a login, deliberately.** The seed is the community spreadsheet: a few
hundred real display names and ratings that belong to whoever maintains it. On
an open endpoint that is a scrapeable copy of someone else's list. Requiring a
session means it seeds ratings for people who signed up, which is a different
thing and a defensible one.

Steam IDs are stripped. The client needs them to recognise *itself* and gets
that from its own log; nothing about the ranking requires handing out the Steam
ID of everyone on it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ladder.ratings import recompute, tier_of

#: Alongside the database rather than in the application directory: it is data,
#: it changes when a new season is imported, and it must survive a redeploy that
#: replaces the code.
SEED_PATH = Path(os.environ.get(
    "LADDER_SEED", "/var/lib/forts-ladder/seed/ufer.json"))


class Ranking:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SEED_PATH
        self.players: list[dict] = []
        self.source: str | None = None
        self.reload()

    def reload(self) -> int:
        """Read the seed. A missing or broken file leaves an empty ranking
        rather than raising: the rest of the server has nothing to do with it."""
        self.players = []
        self.source = None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        self.source = raw.get("source")
        for p in raw.get("players", []):
            if not p.get("name") or p.get("rating") is None:
                continue
            rating = float(p["rating"])
            self.players.append({
                "name": p["name"],
                "ufer_rating": round(rating, 1),
                "ufer_title": p.get("title") or tier_of(rating).title,
                "ufer_rank": p.get("rank"),
                # No open rating yet: results are reported by clients, and until
                # one arrives a number here would be invented.
                "open_rating": None,
                "open_title": None,
                "open_games": 0,
                "open_provisional": False,
            })
        self.players.sort(key=lambda r: -(r["ufer_rating"] or 0))
        for i, r in enumerate(self.players, start=1):
            r["rank"] = i
        return len(self.players)

    def apply_open(self, events: list[dict]) -> int:
        """Fill the open column from series reported to this server.

        The seed is the starting point, not a second ranking: a player begins
        at their spreadsheet rating and moves from there, so the first reported
        game does not reset anyone to 1000.

        Players who exist only through reported results are appended — someone
        who was never on the spreadsheet still has to appear once they have
        played.
        """
        seed = {p["name"]: float(p["ufer_rating"]) for p in self.players
                if p.get("ufer_rating") is not None}
        computed = recompute(events, seed=seed)
        by_name = {p["name"]: p for p in self.players}

        for name, st in computed.items():
            row = by_name.get(name)
            if row is None:
                row = {"name": name, "ufer_rating": None, "ufer_title": None,
                       "ufer_rank": None}
                self.players.append(row)
                by_name[name] = row
            row["open_rating"] = round(st.rating, 1)
            row["open_title"] = st.title
            row["open_games"] = st.games
            # Under ten games the number moves a lot per series, so it is shown
            # as what it is rather than as a standing.
            row["open_provisional"] = st.games < 10

        # Ranked by the open rating where there is one, since that is the live
        # column, and by the seed for everyone who has not played yet.
        self.players.sort(key=lambda r: -(r.get("open_rating")
                                          or r.get("ufer_rating") or 0))
        for i, r in enumerate(self.players, start=1):
            r["rank"] = i
        self.open_events = len(events)
        return len(computed)

    def payload(self) -> dict:
        return {
            "source": self.source,
            "count": len(self.players),
            "players": self.players,
            "open_events": getattr(self, "open_events", 0),
            "note": ("Seeded from the community spreadsheet. The open column "
                     "is computed from the series reported to this server."),
        }
