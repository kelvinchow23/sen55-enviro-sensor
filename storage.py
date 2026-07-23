"""SQLite persistence for sensor readings.

Design notes
------------
* One table, `readings`, with an AUTOINCREMENT `id` that doubles as a monotonic
  cursor for "give me everything since I last asked" queries.
* Timestamps are stored as ISO-8601 UTC strings ("...Z"). Lexical order equals
  chronological order, so range queries on `ts` work with plain string compares.
* WAL mode is enabled so the API can read while the sampler writes without the
  two blocking each other.
* Each public function opens its own short-lived connection. sqlite3 connections
  are not safe to share across threads, and the sampler thread and the API
  request handlers are different threads -- a per-call connection sidesteps the
  whole thread-affinity problem. At this scale (one write every few seconds)
  connection-open cost is negligible.
"""

import sqlite3
from contextlib import contextmanager

import config

# Column order used consistently for INSERT and for row->dict mapping.
_FIELDS = (
    "pm1_0", "pm2_5", "pm4_0", "pm10",
    "rh", "temp_c", "voc", "nox",
)


@contextmanager
def _connect():
    """Yield a connection with sane pragmas, committing on clean exit."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        # WAL lets readers and the single writer proceed concurrently.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create the schema and indexes if they don't exist. Idempotent."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,
                pm1_0      REAL,
                pm2_5      REAL,
                pm4_0      REAL,
                pm10       REAL,
                rh         REAL,
                temp_c     REAL,
                voc        REAL,
                nox        REAL,
                status_ok  INTEGER NOT NULL,
                status_raw INTEGER
            )
            """
        )
        # ts index for time-window queries; id is already the PK (indexed).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")


def insert_reading(ts: str, values: dict, status_ok: bool,
                   status_raw: int | None) -> int:
    """Insert one reading. `values` maps each field name in _FIELDS to a
    float or None. Returns the new row id."""
    cols = ("ts", *_FIELDS, "status_ok", "status_raw")
    placeholders = ", ".join("?" for _ in cols)
    params = [ts]
    params += [values.get(f) for f in _FIELDS]
    params += [1 if status_ok else 0, status_raw]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO readings ({', '.join(cols)}) VALUES ({placeholders})",
            params,
        )
        return cur.lastrowid


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = {
        "id": row["id"],
        "ts": row["ts"],
        "status_ok": bool(row["status_ok"]),
        "status_raw": row["status_raw"],
    }
    for f in _FIELDS:
        d[f] = row[f]
    return d


def latest() -> dict | None:
    """Most recent reading, or None if the table is empty."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def query_latest_n(limit: int) -> list[dict]:
    """Last `limit` readings, returned oldest-first for easy plotting."""
    limit = max(1, min(int(limit), config.MAX_LIMIT))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    # We fetched newest-first to honor LIMIT; reverse to chronological order.
    return [_row_to_dict(r) for r in reversed(rows)]


def query_since_ts(since_iso: str, limit: int) -> list[dict]:
    """All readings with ts >= since_iso (chronological), capped at limit."""
    limit = max(1, min(int(limit), config.MAX_LIMIT))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM readings WHERE ts >= ? ORDER BY id ASC LIMIT ?",
            (since_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def query_after_id(after_id: int, limit: int) -> tuple[list[dict], int]:
    """All readings with id > after_id (chronological), capped at limit.

    This is the cursor query: a client passes the last id it has seen and
    receives only newer rows. Stateless on the server side.

    Returns (rows, high_water_id) where high_water_id is the current MAX(id)
    read *in the same connection/snapshot* as the SELECT. This lets the caller
    advance the cursor safely on an empty batch without a second connection
    racing an in-flight insert: if the SELECT saw no rows, high_water_id cannot
    exceed after_id, so no unseen row is ever skipped. (See the cursor-race
    fix -- computing MAX(id) in a separate connection could observe a row the
    SELECT missed and skip it permanently.)
    """
    after_id = int(after_id)
    limit = max(1, min(int(limit), config.MAX_LIMIT))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM readings WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        ).fetchall()
        # Same connection, same read transaction -> consistent snapshot.
        hw = conn.execute("SELECT MAX(id) AS m FROM readings").fetchone()["m"]
    return [_row_to_dict(r) for r in rows], (hw or 0)


def max_id() -> int:
    """Current maximum row id (0 if empty). Used as the cursor high-water mark."""
    with _connect() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM readings").fetchone()
    return row["m"] or 0


def prune_older_than(cutoff_iso: str) -> int:
    """Delete rows with ts < cutoff_iso. Returns number of rows deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff_iso,))
        return cur.rowcount
