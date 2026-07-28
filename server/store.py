"""Persistence on SQLite.

The league has a few hundred players; even ten thousand matches a year is a
few megabytes. A database that sits next to the code as a file can be backed
up by copying it — which is exactly what you want to be able to do at three
in the morning after a tournament.

Tournaments are stored as **entrants and results**, not as an object tree.
On load the bracket is rebuilt from the entrants and the stored results are
applied to it. That keeps the builder the only place that knows about
seeding and byes, and makes it impossible to load a state the builder would
never produce. It requires the build to be deterministic, which a test
checks.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

from ladder.modes import BY_KEY, Mode
from ladder.tournament import Participant, Tournament

from .auth import Account, AuthService, Grant, Role, Session

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    discord_id    TEXT UNIQUE,
    discord_name  TEXT,
    -- UNIQUE because two accounts sharing a SteamID would score the same
    -- matches twice. A write error beats a silent double count.
    steam_id      TEXT UNIQUE,
    ufer_name     TEXT UNIQUE,
    role          INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    -- Defaults to 0: a restart must never turn into consent nobody gave.
    tracking_consent INTEGER NOT NULL DEFAULT 0,
    consent_since    TEXT
);

CREATE TABLE IF NOT EXISTS account_grants (
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    grant_key   TEXT NOT NULL,
    granted_by  TEXT,
    granted_at  REAL NOT NULL,
    PRIMARY KEY (account_id, grant_key)
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);

-- Which lobbies were set up for a ladder match. The allowlist: a result only
-- counts if its lobby is in here, so the ranked queue cannot slip in.
CREATE TABLE IF NOT EXISTS sanctioned_lobbies (
    lobby_id    INTEGER PRIMARY KEY,
    series_id   TEXT,
    created_by  TEXT REFERENCES accounts(id),
    created_at  REAL NOT NULL
);

-- Drafts are stored the same way tournaments are: the setup plus the moves,
-- not a serialised object. Replaying the moves through the engine means a
-- restored draft can only ever be a state the engine would produce itself, and
-- the rules stay in one place.
CREATE TABLE IF NOT EXISTS drafts (
    id            TEXT PRIMARY KEY,
    join_code     TEXT NOT NULL,
    map_pool      TEXT NOT NULL,          -- JSON list
    commander_pool TEXT NOT NULL,         -- JSON list
    best_of       INTEGER NOT NULL,
    bans_per_side INTEGER NOT NULL,
    step_seconds  REAL,
    -- Kept so the neutral strike is reproducible: without it a restored draft
    -- would strike a different map and the board would change under the
    -- players.
    strike_seed   INTEGER,
    series_id     TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_seats (
    draft_id   TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    side       TEXT NOT NULL,
    account_id TEXT NOT NULL,
    display    TEXT NOT NULL,
    PRIMARY KEY (draft_id, side)
);

CREATE TABLE IF NOT EXISTS draft_choices (
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,            -- replay order; the rules depend on it
    side     TEXT NOT NULL,
    action   TEXT NOT NULL,
    value    TEXT NOT NULL,
    game     INTEGER,
    PRIMARY KEY (draft_id, seq)
);

CREATE TABLE IF NOT EXISTS tournaments (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    mode_key    TEXT NOT NULL,
    created_by  TEXT REFERENCES accounts(id),
    created_at  REAL NOT NULL,
    finished    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tournament_participants (
    tournament_id TEXT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    seat          INTEGER NOT NULL,      -- order of registration
    name          TEXT NOT NULL,
    rating        REAL NOT NULL,
    members       TEXT NOT NULL,         -- JSON list (teams)
    PRIMARY KEY (tournament_id, seat)
);

CREATE TABLE IF NOT EXISTS tournament_results (
    tournament_id TEXT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    match_id      TEXT NOT NULL,
    winner        TEXT NOT NULL,
    score_a       INTEGER,
    score_b       INTEGER,
    match_keys    TEXT NOT NULL DEFAULT '[]',
    reported_at   REAL NOT NULL,
    -- Order matters: results are replayed in this sequence on load, or a
    -- winner ends up in a round that does not exist yet.
    seq           INTEGER NOT NULL,
    PRIMARY KEY (tournament_id, match_id)
);

-- Reported series: the events the shared ranking is computed from.
--
-- Ratings are deliberately NOT stored. They are recomputed from these rows on
-- demand, which is what makes withdrawing consent retroactive and lets anyone
-- with the same rows arrive at the same numbers.
--
-- `rated` is a fact about the series, not a permission: a report that arrives
-- from an unsanctioned lobby is kept, marked, and left out of the maths. Silently
-- dropping it would make "my game did not count" impossible to explain.
CREATE TABLE IF NOT EXISTS results (
    id          TEXT PRIMARY KEY,
    lobby_id    INTEGER,
    sides       TEXT NOT NULL,          -- {"765...": 1, "765...": 2}
    games       INTEGER NOT NULL,
    score_low   INTEGER NOT NULL,       -- games won by the lower side number
    played_at   TEXT NOT NULL,
    reported_by TEXT REFERENCES accounts(id),
    rated       INTEGER NOT NULL DEFAULT 1,
    reasons     TEXT NOT NULL DEFAULT '[]',
    replays     TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL
);

-- One series reported twice is one series. Two clients in the same match both
-- report it, on purpose — whichever is running wins — so the pair of lobby and
-- kickoff time is what makes the second one a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS results_once
    ON results (lobby_id, played_at);
"""


class Store:
    def __init__(self, path: str | Path = "data/ladder.sqlite") -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    #: Columns added after the first release. `CREATE TABLE IF NOT EXISTS`
    #: does nothing to a table that already exists, so a database from before
    #: the change would keep the old shape and every write would fail with
    #: "no such column".
    _ADDED_COLUMNS = {
        "accounts": [
            ("tracking_consent", "INTEGER NOT NULL DEFAULT 0"),
            ("consent_since", "TEXT"),
        ],
        "drafts": [
            # The handoff and the walk-away. Both have to survive a restart:
            # the lobby id is what recorded games are matched against, and a
            # cancelled draft that came back alive would put a dead board in
            # front of whoever logs in next.
            ("lobby_id", "INTEGER"),
            ("lobby_host", "TEXT"),
            ("cancelled_by", "TEXT"),
        ],
    }

    def _migrate(self) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            have = {r["name"] for r in
                    self.db.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue                      # table not created yet
            for name, decl in columns:
                if name not in have:
                    self.db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.db.close()

    # ----------------------------------------------------------- Accounts
    def save_account(self, a: Account, granted_by: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO accounts
                   (id, discord_id, discord_name, steam_id, ufer_name, role,
                    created_at, tracking_consent, consent_since)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   discord_id=excluded.discord_id,
                   discord_name=excluded.discord_name,
                   steam_id=excluded.steam_id,
                   ufer_name=excluded.ufer_name,
                   role=excluded.role,
                   tracking_consent=excluded.tracking_consent,
                   consent_since=excluded.consent_since""",
            (a.id, a.discord_id, a.discord_name, a.steam_id, a.ufer_name,
             int(a.role), a.created_at,
             int(a.tracking_consent), a.consent_since))
        # Reconcile grants: revoked ones have to actually disappear.
        self.db.execute("DELETE FROM account_grants WHERE account_id = ?", (a.id,))
        self.db.executemany(
            "INSERT INTO account_grants VALUES (?, ?, ?, ?)",
            [(a.id, g.value, granted_by, time.time()) for g in a.grants])
        self.db.commit()

    def load_accounts(self) -> dict[str, Account]:
        out: dict[str, Account] = {}
        for row in self.db.execute("SELECT * FROM accounts"):
            out[row["id"]] = Account(
                id=row["id"], discord_id=row["discord_id"],
                discord_name=row["discord_name"], steam_id=row["steam_id"],
                ufer_name=row["ufer_name"], role=Role(row["role"]),
                created_at=row["created_at"],
                tracking_consent=bool(row["tracking_consent"]),
                consent_since=row["consent_since"])
        for row in self.db.execute("SELECT * FROM account_grants"):
            acc = out.get(row["account_id"])
            if acc is None:
                continue
            try:
                acc.grants.add(Grant(row["grant_key"]))
            except ValueError:
                # A grant the code no longer knows is skipped rather than
                # blocking startup.
                continue
        return out

    # ----------------------------------------------------------- Sessions
    def save_session(self, s: Session) -> None:
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)",
                        (s.token, s.account_id, s.created_at, s.expires_at))
        self.db.commit()

    def delete_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self.db.commit()

    def load_sessions(self, now: float | None = None) -> dict[str, Session]:
        now = now if now is not None else time.time()
        # Sweep expired sessions on load, or the table grows forever.
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        self.db.commit()
        return {r["token"]: Session(r["token"], r["account_id"],
                                    r["created_at"], r["expires_at"])
                for r in self.db.execute("SELECT * FROM sessions")}

    def restore_auth(self, auth: AuthService) -> AuthService:
        auth.accounts = self.load_accounts()
        auth.sessions = self.load_sessions()
        return auth

    # --------------------------------------------------- Sanctioned lobbies
    def sanction_lobby(self, lobby_id: int, series_id: str | None = None,
                       created_by: str | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO sanctioned_lobbies VALUES (?, ?, ?, ?)",
            (int(lobby_id), series_id, created_by, time.time()))
        self.db.commit()

    def sanctioned_lobbies(self) -> list[int]:
        return [r["lobby_id"] for r in self.db.execute(
            "SELECT lobby_id FROM sanctioned_lobbies ORDER BY created_at DESC")]

    def is_sanctioned(self, lobby_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sanctioned_lobbies WHERE lobby_id = ?",
            (int(lobby_id),)).fetchone() is not None

    # ------------------------------------------------------------ Results
    def next_result_id(self) -> str:
        return secrets.token_hex(8)

    def save_result(self, r) -> None:
        """Write one reported series. Reporting it twice changes nothing.

        Both clients in a match report, so the second arrival is expected. It
        keeps the first version rather than overwriting: they should agree, and
        if they do not, the earlier one is the one that was already rated.
        """
        self.db.execute(
            """INSERT OR IGNORE INTO results
                   (id, lobby_id, sides, games, score_low, played_at,
                    reported_by, rated, reasons, replays, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.id, r.lobby_id, json.dumps(r.sides), r.games, r.score_low,
             r.played_at, r.reported_by, int(r.rated), json.dumps(r.reasons),
             json.dumps(r.replays), r.created_at))
        self.db.commit()

    def load_results(self) -> list:
        from .results import Reported
        return [Reported(
            id=r["id"], lobby_id=r["lobby_id"],
            sides={k: int(v) for k, v in json.loads(r["sides"]).items()},
            games=r["games"], score_low=r["score_low"],
            played_at=r["played_at"], reported_by=r["reported_by"],
            rated=bool(r["rated"]), reasons=json.loads(r["reasons"]),
            replays=json.loads(r["replays"]), created_at=r["created_at"])
            for r in self.db.execute(
                "SELECT * FROM results ORDER BY played_at, created_at")]

    # ------------------------------------------------------------- Drafts
    def save_draft(self, session) -> None:
        """Write setup, seats and every move so far.

        Called after each move rather than at the end: a draft that only
        survived once finished would not survive the thing persistence is for.
        """
        d = session.draft
        self.db.execute(
            """INSERT INTO drafts (id, join_code, map_pool, commander_pool,
                    best_of, bans_per_side, step_seconds, strike_seed,
                    series_id, created_at, lobby_id, lobby_host, cancelled_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   join_code=excluded.join_code,
                   lobby_id=excluded.lobby_id,
                   lobby_host=excluded.lobby_host,
                   cancelled_by=excluded.cancelled_by""",
            (session.id, session.join_code,
             json.dumps(session.original_map_pool or d.map_pool),
             json.dumps(d.commander_pool), d.best_of,
             d.commander_bans_per_side, d.step_seconds, d.strike_seed,
             session.series_id, session.created_at,
             session.lobby_id, session.lobby_host, session.cancelled_by))

        self.db.execute("DELETE FROM draft_seats WHERE draft_id = ?", (session.id,))
        self.db.executemany(
            "INSERT INTO draft_seats VALUES (?, ?, ?, ?)",
            [(session.id, s.side.value, s.account_id, s.display)
             for s in session.seats.values()])

        # Rewritten wholesale: the move list only grows, and comparing is more
        # code than replacing a handful of rows.
        self.db.execute("DELETE FROM draft_choices WHERE draft_id = ?", (session.id,))
        self.db.executemany(
            "INSERT INTO draft_choices VALUES (?, ?, ?, ?, ?, ?)",
            [(session.id, i, c.side.value if c.side else "", c.action.value,
              c.value, c.game) for i, c in enumerate(d.choices)])
        self.db.commit()

    def load_drafts(self, max_age_s: float = 24 * 3600) -> list[dict]:
        """Rows for drafts worth restoring.

        Anything older than a day is left behind: a draft nobody finished
        yesterday is not something to resume, and restoring it would put a stale
        board in front of whoever logs in next.
        """
        cutoff = time.time() - max_age_s
        out = []
        for r in self.db.execute(
                "SELECT * FROM drafts WHERE created_at > ? ORDER BY created_at",
                (cutoff,)):
            out.append({
                "id": r["id"], "join_code": r["join_code"],
                "map_pool": json.loads(r["map_pool"]),
                "commander_pool": json.loads(r["commander_pool"]),
                "best_of": r["best_of"], "bans_per_side": r["bans_per_side"],
                "step_seconds": r["step_seconds"],
                "strike_seed": r["strike_seed"],
                "series_id": r["series_id"], "created_at": r["created_at"],
                "lobby_id": r["lobby_id"], "lobby_host": r["lobby_host"],
                "cancelled_by": r["cancelled_by"],
                "seats": [dict(s) for s in self.db.execute(
                    "SELECT side, account_id, display FROM draft_seats "
                    "WHERE draft_id = ?", (r["id"],))],
                "choices": [dict(c) for c in self.db.execute(
                    "SELECT side, action, value, game FROM draft_choices "
                    "WHERE draft_id = ? ORDER BY seq", (r["id"],))],
            })
        return out

    def delete_draft(self, draft_id: str) -> None:
        self.db.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        self.db.commit()

    # -------------------------------------------------------- Tournaments
    def create_tournament(self, tid: str, t: Tournament,
                          created_by: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO tournaments (id, name, mode_key, created_by, "
            "created_at, finished) VALUES (?, ?, ?, ?, ?, 0)",
            (tid, t.name, t.mode.key, created_by, time.time()))
        self.db.executemany(
            "INSERT INTO tournament_participants VALUES (?, ?, ?, ?, ?)",
            [(tid, i, p.name, p.rating, json.dumps(p.members))
             for i, p in enumerate(t.participants)])
        self.db.commit()

    def record_result(self, tid: str, match_id: str, winner: str,
                      score: tuple[int, int] | None,
                      match_keys: list[str] | None = None) -> None:
        seq = self.db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM tournament_results "
            "WHERE tournament_id = ?", (tid,)).fetchone()["n"]
        self.db.execute(
            "INSERT OR REPLACE INTO tournament_results "
            "(tournament_id, match_id, winner, score_a, score_b, match_keys, "
            " reported_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, match_id, winner,
             score[0] if score else None, score[1] if score else None,
             json.dumps(match_keys or []), time.time(), seq))
        self.db.commit()

    def load_tournament(self, tid: str) -> Tournament:
        row = self.db.execute("SELECT * FROM tournaments WHERE id = ?",
                              (tid,)).fetchone()
        if row is None:
            raise KeyError(f"no tournament {tid!r}")
        mode: Mode = BY_KEY.get(row["mode_key"]) or BY_KEY["tournament_1v1"]

        participants = [
            Participant(r["name"], r["rating"], json.loads(r["members"]))
            for r in self.db.execute(
                "SELECT * FROM tournament_participants WHERE tournament_id = ? "
                "ORDER BY seat", (tid,))]

        t = Tournament(row["name"], participants, mode=mode)

        # Apply results in report order; the bracket rebuilds itself.
        for r in self.db.execute(
                "SELECT * FROM tournament_results WHERE tournament_id = ? "
                "ORDER BY seq", (tid,)):
            score = ((r["score_a"], r["score_b"])
                     if r["score_a"] is not None else None)
            t.report(r["match_id"], r["winner"], score,
                     json.loads(r["match_keys"]))
        return t

    def list_tournaments(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT t.*, COUNT(p.seat) AS participants FROM tournaments t "
            "LEFT JOIN tournament_participants p ON p.tournament_id = t.id "
            "GROUP BY t.id ORDER BY t.created_at DESC")]

    def mark_finished(self, tid: str) -> None:
        self.db.execute("UPDATE tournaments SET finished = 1 WHERE id = ?",
                        (tid,))
        self.db.commit()
