"""SQLite storage. One connection, serialised by a lock: the load is a few faces a minute.

sightings  one face in one frame (embedding, crop, what ImmichEye called it)
visits     a camera's sightings of one person in one arrival
identities a known (Immich-named) person or an unknown visitor; owns visits
labels     ImmichEye label -> known identity (several labels after a merge)
rings      doorbell presses, and the visit each belongs to
merges     identities merged into another, kept on the one they went into

Ids of identities, visits and sightings are never reused (AUTOINCREMENT): Home Assistant
entities are keyed by identity id, and face images are cached by sighting id, so a deleted
person's id must never come back as someone else.
"""
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    first_seen REAL, last_seen REAL,
    events INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('known', 'unknown')),
    name TEXT NOT NULL,
    named INTEGER NOT NULL DEFAULT 0,      -- given a name by a person, not "Visitor 12"
    notes TEXT NOT NULL DEFAULT '',
    ignored INTEGER NOT NULL DEFAULT 0,    -- the household: still matched, counted apart, own HA event
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    label TEXT PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    camera TEXT,
    frame TEXT
);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    touched_at REAL NOT NULL,              -- wall clock of the last face, for closing
    closed INTEGER NOT NULL DEFAULT 0,
    how TEXT,                              -- named | matched | new | manual | none
    match_score REAL,
    announced_id INTEGER,                  -- identity last announced while open (arrival / identified)
    merge_id INTEGER REFERENCES merges(id) ON DELETE SET NULL,  -- came to this identity by that merge
    caption TEXT                           -- JSON: the vision model's description (caption.py)
);
CREATE INDEX IF NOT EXISTS visits_identity ON visits(identity_id, start_ts);
CREATE INDEX IF NOT EXISTS visits_start ON visits(start_ts);
CREATE INDEX IF NOT EXISTS visits_open ON visits(closed);
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    camera TEXT NOT NULL,
    x1 INTEGER, y1 INTEGER, x2 INTEGER, y2 INTEGER,
    px INTEGER NOT NULL,
    det REAL NOT NULL,
    quality REAL NOT NULL,
    usable INTEGER NOT NULL,
    label TEXT,                            -- ImmichEye's name for it, NULL if unknown
    score REAL,
    top TEXT,
    embedding BLOB,
    crop TEXT,
    folded_from INTEGER                    -- a stray visit this face was folded in from (now gone)
);
CREATE INDEX IF NOT EXISTS sightings_visit ON sightings(visit_id);
CREATE INDEX IF NOT EXISTS sightings_ts ON sightings(ts);
CREATE TABLE IF NOT EXISTS dismissed (
    a INTEGER NOT NULL, b INTEGER NOT NULL,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS rings (
    id INTEGER PRIMARY KEY,
    camera TEXT NOT NULL,
    ts REAL NOT NULL,
    visit_id INTEGER REFERENCES visits(id) ON DELETE CASCADE   -- NULL until a visit claims it
);
CREATE INDEX IF NOT EXISTS rings_camera ON rings(camera, ts);
CREATE TABLE IF NOT EXISTS merges (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    identity_id INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,   -- merged into
    name TEXT NOT NULL,                    -- the identity merged in, as it was called
    visits INTEGER NOT NULL                -- its visits, now this identity's
);
CREATE INDEX IF NOT EXISTS merges_identity ON merges(identity_id);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

NO_REUSE = ("identities", "visits", "sightings")
ADDED = (("visits", "announced_id", "INTEGER"), ("sightings", "folded_from", "INTEGER"),
         ("visits", "merge_id", "INTEGER REFERENCES merges(id) ON DELETE SET NULL"), ("visits", "caption", "TEXT"))   # columns added since a table was first created


def _table_sql(name: str) -> str:
    start = SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {name} (")
    return SCHEMA[start:SCHEMA.index(");", start) + 2]


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._no_id_reuse()
        self._add_columns()

    def _add_columns(self) -> None:
        for table, col, decl in ADDED:
            if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _no_id_reuse(self) -> None:
        """Databases from before AUTOINCREMENT: rebuild those tables with it, keeping every
        row and id (SQLite's documented table-rebuild procedure). Runs once."""
        old = [t for t in NO_REUSE
               if "AUTOINCREMENT" not in self.conn.execute(
                   "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone()[0].upper()]
        if not old:
            return
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for t in old:
                create = _table_sql(t).replace(f"CREATE TABLE IF NOT EXISTS {t} (", f"CREATE TABLE {t}_new (", 1)
                cols = ", ".join(r[1] for r in self.conn.execute(f"PRAGMA table_info({t})"))
                self.conn.execute(create)
                self.conn.execute(f"INSERT INTO {t}_new ({cols}) SELECT {cols} FROM {t}")
                self.conn.execute(f"DROP TABLE {t}")
                self.conn.execute(f"ALTER TABLE {t}_new RENAME TO {t}")
            if self.conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("foreign keys broken by the id migration")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)   # indexes dropped with the old tables

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k = ?", (key,))
        return r["v"] if r else default

    def put(self, key: str, value: str) -> None:
        self.x("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    class _Tx:
        def __init__(self, db: "DB"):
            self.db = db

        def __enter__(self):
            self.db.lock.acquire()
            try:
                self.db.conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                self.db.lock.release()   # e.g. "database is locked": the next caller must not hang
                raise
            return self.db

        def __exit__(self, exc_type, *_):
            conn = self.db.conn
            try:
                if exc_type:
                    if conn.in_transaction:   # some errors (disk full) have rolled it back already
                        conn.execute("ROLLBACK")
                else:
                    try:
                        conn.execute("COMMIT")
                    except BaseException:
                        if conn.in_transaction:   # a failed COMMIT leaves it open: the next BEGIN would fail
                            conn.execute("ROLLBACK")
                        raise
            finally:
                self.db.lock.release()

    def tx(self) -> "_Tx":
        return DB._Tx(self)
