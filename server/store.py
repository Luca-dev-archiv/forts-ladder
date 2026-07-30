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

import hashlib
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
    -- A player asked for a human to look. Stored on the row rather than as a
    -- separate table: it is a property of that series, and one note is enough.
    flagged     INTEGER NOT NULL DEFAULT 0,
    flag_note   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

-- No unique index on (lobby_id, played_at) any more; see `_migrate`, which
-- drops the one older databases still have. Identity is the draft id, which is
-- in the primary key.
"""


def _stored_json(raw, fallback):
    """Read a JSON column without letting a damaged one take the page down.

    Every one of these was written by this program, so a value that will not
    parse means the row is damaged — a half-finished write, a bad migration,
    somebody editing the database by hand. Crashing is the wrong answer twice
    over: the whole tournament becomes unreachable because one row is wrong, and
    `json.JSONDecodeError` is a `ValueError`, so the parser's own text lands
    wherever a rule refusal would have been shown.
    """
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


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
            ("steam_name", "TEXT"),
            ("ufer_claim", "TEXT"),
        ],
        "results": [
            ("flagged", "INTEGER NOT NULL DEFAULT 0"),
            ("flag_note", "TEXT NOT NULL DEFAULT ''"),
            # A rating taken back by a reviewer. Recorded rather than applied
            # silently: the two players are entitled to know that somebody
            # decided this, who, and why.
            ("annulled_by", "TEXT"),
            ("annulled_at", "REAL"),
            ("annul_note", "TEXT NOT NULL DEFAULT ''"),
            # The directory this series' replays live in, drawn by the server on
            # first upload. Stored rather than derived so that nothing a caller
            # sends is ever part of a filesystem path.
            ("replay_key", "TEXT"),
        ],
        "tournaments": [
            # A tournament that is still being built. Entrants can be added,
            # dropped and reordered while this is 1; starting it fixes them,
            # because from then on the pairings and every stored result rest on
            # those names.
            ("planning", "INTEGER NOT NULL DEFAULT 0"),
            # How the host wanted it seeded and how long a series is. Without
            # these a restored tournament re-seeds by rating and reverts to the
            # mode's series length, quietly changing the event.
            ("seeding", "TEXT NOT NULL DEFAULT 'rating'"),
            ("best_of", "INTEGER"),
        ],
        "drafts": [
            # The handoff and the walk-away. Both have to survive a restart:
            # the lobby id is what recorded games are matched against, and a
            # cancelled draft that came back alive would put a dead board in
            # front of whoever logs in next.
            ("lobby_id", "INTEGER"),
            ("lobby_host", "TEXT"),
            ("lobby_host_steam", "TEXT"),
            ("lobby_password", "TEXT"),
            ("aborted_side", "TEXT"),
            ("aborted_reason", "TEXT"),
            # Agreements, not derived state: a void both players settled on
            # must not come back to life after a restart.
            ("voided", "INTEGER NOT NULL DEFAULT 0"),
            ("voided_games", "TEXT NOT NULL DEFAULT '[]'"),
            ("results", "TEXT NOT NULL DEFAULT '{}'"),
            ("cancelled_by", "TEXT"),
            # A closed-out series. Without this a restart reopens every decided
            # series and locks both players out of the queue again.
            ("concluded", "INTEGER NOT NULL DEFAULT 0"),
            # The handoff clock. Wall-clock stamps on purpose: a redeploy in the
            # middle of a handoff must not silently reset or expire it.
            ("done_at", "REAL"),
            ("lobby_at", "REAL"),
            ("guest_ready_at", "REAL"),
            ("extra_seconds", "REAL NOT NULL DEFAULT 0"),
            # Why a game was thrown out. A redeploy mid-series would otherwise
            # lose the reason and quietly let the wrong game count.
            ("deviations", "TEXT NOT NULL DEFAULT '{}'"),
            # People per side. A restored 2v2 that came back as a duel would
            # think it was full with half its players missing.
            ("team_size", "INTEGER NOT NULL DEFAULT 1"),
        ],
    }

    def _migrate(self) -> None:
        # A series used to be identified by (lobby_id, played_at) through a
        # unique index. It was the wrong pair twice over. Only a *host's* log
        # carries a lobby id, so a guest's report had NULL there — and NULLs are
        # distinct in a SQLite unique index, so the two reports of one series
        # never met. Meanwhile two genuinely different series in the same lobby
        # at the same recorded second silently discarded the second one.
        #
        # The identity is the draft id now, which the server itself handed to
        # both clients and which is the primary key. The old index can only lose
        # data from here on.
        self.db.execute("DROP INDEX IF EXISTS results_once")
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
                    created_at, tracking_consent, consent_since,
                    steam_name, ufer_claim)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   discord_id=excluded.discord_id,
                   discord_name=excluded.discord_name,
                   steam_id=excluded.steam_id,
                   ufer_name=excluded.ufer_name,
                   role=excluded.role,
                   tracking_consent=excluded.tracking_consent,
                   consent_since=excluded.consent_since,
                   steam_name=excluded.steam_name,
                   ufer_claim=excluded.ufer_claim""",
            (a.id, a.discord_id, a.discord_name, a.steam_id, a.ufer_name,
             int(a.role), a.created_at,
             int(a.tracking_consent), a.consent_since,
             a.steam_name, a.ufer_claim))
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
                consent_since=row["consent_since"],
                steam_name=row["steam_name"],
                ufer_claim=row["ufer_claim"])
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

    def sanctioned_for(self, steam_id: str, account_id: str) -> list[int]:
        """The sanctioned lobbies *this* account has anything to do with.

        Not the whole list. Handing every client every lobby the ladder ever set
        up would turn this into a directory of who played where, which is nobody
        else's business — and the client only needs to know about its own games.

        Two routes in: a lobby they hosted, and a lobby that appears in a series
        they were reported in.
        """
        out = {r["lobby_id"] for r in self.db.execute(
            "SELECT lobby_id FROM sanctioned_lobbies WHERE created_by = ?",
            (account_id,))}
        for r in self.db.execute(
                "SELECT lobby_id, sides FROM results WHERE lobby_id IS NOT NULL"):
            try:
                sides = json.loads(r["sides"])
            except ValueError:
                continue
            if steam_id in sides and self.is_sanctioned(r["lobby_id"]):
                out.add(r["lobby_id"])
        return sorted(out)

    def is_sanctioned(self, lobby_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sanctioned_lobbies WHERE lobby_id = ?",
            (int(lobby_id),)).fetchone() is not None

    # ------------------------------------------------------------ Results
    def next_result_id(self) -> str:
        return secrets.token_hex(8)

    def save_result(self, r) -> None:
        """Write one reported series. Reporting it twice changes nothing.

        Both clients in a match report, so the second arrival is expected, and
        the first version is the one kept — they should agree.

        With one exception. A stored row that could *not* be rated is replaced by
        one that can: "the earlier one is the one that was already rated" is only
        a reason while the earlier one was rated. Before this, a guest reporting
        first — with no lobby id, because only a host's log has one — wrote an
        unrated row that the host's good report could never displace.
        """
        if r.rated:
            existing = {x.id: x for x in self.load_results()}.get(r.id)
            if existing is not None and not existing.rated:
                self.db.execute("DELETE FROM results WHERE id = ?", (r.id,))
        self.db.execute(
            """INSERT OR IGNORE INTO results
                   (id, lobby_id, sides, games, score_low, played_at,
                    reported_by, rated, reasons, replays, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.id, r.lobby_id, json.dumps(r.sides), r.games, r.score_low,
             r.played_at, r.reported_by, int(r.rated), json.dumps(r.reasons),
             json.dumps(r.replays), r.created_at))
        self.db.commit()

    def update_result_flag(self, r) -> None:
        self.db.execute(
            "UPDATE results SET flagged = ?, flag_note = ? WHERE id = ?",
            (int(r.flagged), r.flag_note, r.id))
        self.db.commit()

    def update_result_review(self, r) -> None:
        """Write a reviewer's decision. `rated` moves with it, which is what
        makes the standings follow — they are recomputed from the rows that still
        count, exactly as they are when somebody withdraws consent."""
        self.db.execute(
            "UPDATE results SET annulled_by = ?, annulled_at = ?, "
            "annul_note = ?, rated = ?, reasons = ? WHERE id = ?",
            (r.annulled_by, r.annulled_at, r.annul_note, int(r.rated),
             json.dumps(r.reasons), r.id))
        self.db.commit()

    # ------------------------------------------------------------- Replays
    #: How long an uploaded replay is kept.
    #:
    #: Long enough for a dispute to be looked at, short enough that the server is
    #: not quietly accumulating everybody's match history. The clearance from the
    #: game's community was for tracking *results*; recordings are a different
    #: thing and are not kept as if they were the same.
    REPLAY_KEEP_S = 7 * 24 * 3600
    #: One replay, and one series' worth. A .fwr of a long duel is tens of
    #: kilobytes, so these are generous by an order of magnitude and still stop
    #: an upload endpoint being free storage.
    REPLAY_MAX_BYTES = 4 * 1024 * 1024
    REPLAY_MAX_PER_SERIES = 8

    def replay_key(self, result_id: str, create: bool = False) -> str:
        """The name of the directory this series' replays live in.

        Never the series id. A stored token drawn by the server on first upload,
        or a hex digest of the id for a read of something that was never
        uploaded — so the path component is always something this process
        generated, and nothing a caller typed can appear in it. Filtering the id
        down to safe characters also stopped traversal, but it left two ids that
        differed only in punctuation sharing one directory.
        """
        row = self.db.execute(
            "SELECT replay_key FROM results WHERE id = ?",
            (result_id,)).fetchone()
        if row is not None and row["replay_key"]:
            return row["replay_key"]
        if row is not None and create:
            key = secrets.token_hex(8)
            self.db.execute("UPDATE results SET replay_key = ? WHERE id = ?",
                            (key, result_id))
            self.db.commit()
            return key
        # Nothing stored: a stable bucket that still cannot escape, since a
        # sha256 digest is hexadecimal and nothing else.
        return "x" + hashlib.sha256(result_id.encode()).hexdigest()[:16]

    def replay_dir(self, result_id: str, create: bool = False) -> "Path":
        """Where a series' replays live."""
        from pathlib import Path as _P
        return _P(self.path).parent / "replays" / self.replay_key(
            result_id, create)

    def replay_path(self, result_id: str, name: str) -> "Path | None":
        """One stored replay, or nothing.

        The path comes out of `iterdir()` rather than being built by joining the
        requested name onto a directory. Checking a name against a listing and
        then concatenating it anyway is the same answer reached by a route
        nothing can verify; this way a name that is not a file in there cannot
        become a path at all.
        """
        try:
            for p in self.replay_dir(result_id).iterdir():
                if p.is_file() and p.name == name:
                    return p
        except OSError:
            return None
        return None

    def save_replay(self, result_id: str, index: int, data: bytes) -> str:
        """Store one replay under a name this server chose."""
        d = self.replay_dir(result_id, create=True)
        d.mkdir(parents=True, exist_ok=True)
        name = f"game{int(index):02d}.fwr"
        (d / name).write_bytes(data)
        return name

    def replays_for(self, result_id: str) -> list[str]:
        d = self.replay_dir(result_id)
        try:
            return sorted(p.name for p in d.iterdir() if p.is_file())
        except OSError:
            return []

    def prune_replays(self, now: float | None = None) -> int:
        """Delete anything past its keep-by date.

        Called from the pages that list replays, so the retention is enforced by
        somebody looking rather than by a timer that can be dead without anybody
        noticing.
        """
        import shutil
        import time as _t
        from pathlib import Path as _P
        root = _P(self.path).parent / "replays"
        cutoff = (now or _t.time()) - self.REPLAY_KEEP_S
        gone = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return 0
        for d in entries:
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    gone += 1
            except OSError:
                continue
        return gone

    def load_results(self) -> list:
        from .results import Reported
        return [Reported(
            id=r["id"], lobby_id=r["lobby_id"],
            sides={k: int(v) for k, v in _stored_json(r["sides"], {}).items()},
            games=r["games"], score_low=r["score_low"],
            played_at=r["played_at"], reported_by=r["reported_by"],
            rated=bool(r["rated"]), reasons=_stored_json(r["reasons"], []),
            replays=_stored_json(r["replays"], []), created_at=r["created_at"],
            flagged=bool(r["flagged"]), flag_note=r["flag_note"] or "",
            replay_key=r["replay_key"],
            annulled_by=r["annulled_by"], annulled_at=r["annulled_at"],
            annul_note=r["annul_note"] or "")
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
                    series_id, created_at, lobby_id, lobby_host, cancelled_by,
                    lobby_host_steam, voided, voided_games, results,
                    lobby_password, aborted_side, aborted_reason,
                    concluded, done_at, lobby_at, guest_ready_at,
                    extra_seconds, deviations, team_size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   join_code=excluded.join_code,
                   lobby_id=excluded.lobby_id,
                   lobby_host=excluded.lobby_host,
                   cancelled_by=excluded.cancelled_by,
                   lobby_host_steam=excluded.lobby_host_steam,
                   voided=excluded.voided,
                   voided_games=excluded.voided_games,
                   results=excluded.results,
                   lobby_password=excluded.lobby_password,
                   aborted_side=excluded.aborted_side,
                   aborted_reason=excluded.aborted_reason,
                   concluded=excluded.concluded,
                   done_at=excluded.done_at,
                   lobby_at=excluded.lobby_at,
                   guest_ready_at=excluded.guest_ready_at,
                   extra_seconds=excluded.extra_seconds,
                   deviations=excluded.deviations,
                   team_size=excluded.team_size""",
            (session.id, session.join_code,
             json.dumps(session.original_map_pool or d.map_pool),
             json.dumps(d.commander_pool), d.best_of,
             d.commander_bans_per_side, d.step_seconds, d.strike_seed,
             session.series_id, session.created_at,
             session.lobby_id, session.lobby_host, session.cancelled_by,
             session.lobby_host_steam, int(session.voided),
             json.dumps(sorted(session.voided_games)),
             json.dumps({str(g): s.value
                         for g, s in session.draft._results.items()}),
             session.lobby_password, session.aborted_side,
             session.aborted_reason, int(session.concluded),
             session.done_at, session.lobby_at, session.guest_ready_at,
             session.extra_seconds,
             json.dumps({str(g): v
                         for g, v in session.deviations.items()}),
             session.team_size))

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
                "map_pool": _stored_json(r["map_pool"], []),
                "commander_pool": _stored_json(r["commander_pool"], []),
                "best_of": r["best_of"], "bans_per_side": r["bans_per_side"],
                "step_seconds": r["step_seconds"],
                "strike_seed": r["strike_seed"],
                "series_id": r["series_id"], "created_at": r["created_at"],
                "lobby_id": r["lobby_id"], "lobby_host": r["lobby_host"],
                "lobby_host_steam": r["lobby_host_steam"],
                "lobby_password": r["lobby_password"],
                "aborted_side": r["aborted_side"],
                "aborted_reason": r["aborted_reason"],
                "voided": bool(r["voided"]),
                "concluded": bool(r["concluded"]),
                "done_at": r["done_at"],
                "lobby_at": r["lobby_at"],
                "guest_ready_at": r["guest_ready_at"],
                "extra_seconds": r["extra_seconds"] or 0.0,
                "deviations": {int(g): list(v) for g, v in
                               _stored_json(r["deviations"], {}).items()},
                "team_size": r["team_size"] or 1,
                "voided_games": _stored_json(r["voided_games"], []),
                "results": _stored_json(r["results"], {}),
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
                          created_by: str | None = None,
                          planning: bool = False) -> None:
        self.db.execute(
            "INSERT INTO tournaments (id, name, mode_key, created_by, "
            "created_at, finished, seeding, best_of, planning) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (tid, t.name, t.mode.key, created_by, time.time(),
             t.seeding, t.best_of, int(planning)))
        self.db.executemany(
            "INSERT INTO tournament_participants VALUES (?, ?, ?, ?, ?)",
            [(tid, i, p.name, p.rating, json.dumps(p.members))
             for i, p in enumerate(t.participants)])
        self.db.commit()

    def rename_participant(self, tid: str, seat: int, name: str) -> None:
        self.db.execute(
            "UPDATE tournament_participants SET name = ?, members = ? "
            "WHERE tournament_id = ? AND seat = ?",
            (name, json.dumps([name]), tid, seat))
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
            Participant(r["name"], r["rating"],
                        _stored_json(r["members"], [r["name"]]))
            for r in self.db.execute(
                "SELECT * FROM tournament_participants WHERE tournament_id = ? "
                "ORDER BY seat", (tid,))]

        t = Tournament(row["name"], participants, mode=mode,
                       seeding=row["seeding"] or "rating",
                       best_of=row["best_of"],
                       # Or a plan with one entrant could be written but never
                       # read back.
                       planning=bool(row["planning"]))

        # Apply results in report order; the bracket rebuilds itself.
        for r in self.db.execute(
                "SELECT * FROM tournament_results WHERE tournament_id = ? "
                "ORDER BY seq", (tid,)):
            score = ((r["score_a"], r["score_b"])
                     if r["score_a"] is not None else None)
            t.report(r["match_id"], r["winner"], score,
                     _stored_json(r["match_keys"], []))
        return t

    def is_planning(self, tid: str) -> bool:
        row = self.db.execute(
            "SELECT planning FROM tournaments WHERE id = ?", (tid,)).fetchone()
        return bool(row and row["planning"])

    def set_planning(self, tid: str, value: bool) -> None:
        self.db.execute("UPDATE tournaments SET planning = ? WHERE id = ?",
                        (int(value), tid))
        self.db.commit()

    def replace_participants(self, tid: str, people: list) -> None:
        """Swap the entrant list wholesale.

        Only meaningful while planning: the seats are what the bracket is built
        from, so rewriting them after a result exists would move a result onto a
        different player.
        """
        self.db.execute(
            "DELETE FROM tournament_participants WHERE tournament_id = ?", (tid,))
        self.db.executemany(
            "INSERT INTO tournament_participants VALUES (?, ?, ?, ?, ?)",
            [(tid, i, p.name, p.rating, json.dumps(p.members))
             for i, p in enumerate(people)])
        self.db.commit()

    def set_tournament_format(self, tid: str, name: str, mode_key: str,
                              seeding: str, best_of: int | None) -> None:
        self.db.execute(
            "UPDATE tournaments SET name = ?, mode_key = ?, seeding = ?, "
            "best_of = ? WHERE id = ?",
            (name, mode_key, seeding, best_of, tid))
        self.db.commit()

    def list_tournaments(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT t.*, COUNT(p.seat) AS participants FROM tournaments t "
            "LEFT JOIN tournament_participants p ON p.tournament_id = t.id "
            "GROUP BY t.id ORDER BY t.created_at DESC")]

    def mark_finished(self, tid: str) -> None:
        self.db.execute("UPDATE tournaments SET finished = 1 WHERE id = ?",
                        (tid,))
        self.db.commit()
