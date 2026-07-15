import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.db")

# Per-thread sqlite3 connection. The previous design held a single
# threading.Lock around every DB call, which defeated WAL's whole point
# (concurrent readers). Now reads run lock-free against per-thread
# connections; only the write path inside a single transaction is
# serialised by SQLite itself.
_tls = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    torrent_id INTEGER NOT NULL,
    torrent_name TEXT,
    started_at TEXT,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    dest_host TEXT,
    dest_path TEXT,
    folder_name TEXT,
    media_type TEXT,
    total_bytes INTEGER DEFAULT 0,
    bytes_transferred INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    torrent_id INTEGER,
    torrent_name TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS custom_names (
    hash TEXT PRIMARY KEY,
    custom_name TEXT NOT NULL,
    default_name TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS removed_torrents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE,
    name TEXT,
    custom_name TEXT,
    magnet_link TEXT,
    total_size INTEGER,
    download_dir TEXT,
    removed_at TEXT NOT NULL,
    deleted_local_data INTEGER DEFAULT 0,
    uploaded_ever INTEGER
);

CREATE TABLE IF NOT EXISTS unloaded_torrents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE,
    name TEXT,
    custom_name TEXT,
    magnet_link TEXT NOT NULL,
    total_size INTEGER,
    percent_done REAL,
    download_dir TEXT,
    labels TEXT,
    unloaded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copy_history_finished ON copy_history(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_unloaded_torrents_at ON unloaded_torrents(unloaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_removed_torrents_removed ON removed_torrents(removed_at DESC);
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _conn():
    """Per-thread sqlite3 connection. WAL + busy_timeout lets concurrent
    readers proceed and lets writers wait their turn instead of failing
    with SQLITE_BUSY. PRAGMAs only need to run once per connection."""
    c = getattr(_tls, "conn", None)
    if c is not None:
        return c
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    _tls.conn = c
    return c


@contextmanager
def _tx():
    """Wrap a write in an explicit BEGIN/COMMIT so multiple statements stay
    atomic. Uses the per-thread connection."""
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def init():
    c = _conn()
    # auto_vacuum=INCREMENTAL has to be set BEFORE the first table is
    # created (on a fresh DB) or it's a no-op. For pre-existing installs
    # the operator can run `VACUUM;` once offline to switch — we don't do
    # that automatically because it locks the DB for the duration.
    c.execute("PRAGMA auto_vacuum=INCREMENTAL")
    # executescript implicitly commits, so don't wrap it in BEGIN/COMMIT.
    c.executescript(SCHEMA)
    # Add uploaded_ever to removed_torrents for existing installs.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(removed_torrents)")}
    if "uploaded_ever" not in cols:
        c.execute("ALTER TABLE removed_torrents ADD COLUMN uploaded_ever INTEGER")
    # Reclaim a bounded number of free pages on each restart so a long-
    # running install eventually shrinks the file after lots of deletes.
    # 256 pages * 4KB ≈ 1MB per boot — cheap, won't stall startup.
    try:
        c.execute("PRAGMA incremental_vacuum(256)")
    except sqlite3.OperationalError:
        # auto_vacuum=NONE on legacy installs — incremental_vacuum errors
        # out cleanly. Nothing to do.
        pass
    # Drop events past retention on every boot; day-to-day pruning happens
    # opportunistically from log_event.
    _maybe_prune_events()


def record_copy(torrent_id, torrent_name, **fields):
    with _tx() as c:
        c.execute(
            """INSERT INTO copy_history
            (torrent_id, torrent_name, started_at, finished_at, status,
             dest_host, dest_path, folder_name, media_type,
             total_bytes, bytes_transferred, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(torrent_id),
                torrent_name,
                fields.get("started_at"),
                fields.get("finished_at") or _now_iso(),
                fields.get("status") or "unknown",
                fields.get("dest_host"),
                fields.get("dest_path"),
                fields.get("folder_name"),
                fields.get("media_type"),
                int(fields.get("total_bytes") or 0),
                int(fields.get("bytes_transferred") or 0),
                fields.get("error_message"),
            ),
        )


# The events table grew unboundedly — every copy/refresh/redownload left a
# row forever. Prune on startup and then at most once a day from the write
# path, so a long-running install can't accumulate years of rows.
EVENTS_RETENTION_DAYS = int(os.getenv("EVENTS_RETENTION_DAYS", "90"))
_EVENTS_PRUNE_INTERVAL = 86400.0
_events_prune_lock = threading.Lock()
_events_last_prune = 0.0


def prune_events(days=None):
    days = EVENTS_RETENTION_DAYS if days is None else days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _tx() as c:
        # ts is ISO8601 UTC, so lexicographic comparison is chronological.
        c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


def _maybe_prune_events():
    global _events_last_prune
    now = time.monotonic()
    with _events_prune_lock:
        if _events_last_prune and now - _events_last_prune < _EVENTS_PRUNE_INTERVAL:
            return
        _events_last_prune = now
    try:
        prune_events()
    except sqlite3.Error:
        # Pruning is housekeeping — never let it break event logging.
        pass


def log_event(type, severity, message, torrent_id=None, torrent_name=None, details=None):
    _maybe_prune_events()
    with _tx() as c:
        c.execute(
            """INSERT INTO events
            (ts, type, severity, message, torrent_id, torrent_name, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                type,
                severity,
                message,
                int(torrent_id) if torrent_id is not None else None,
                torrent_name,
                json.dumps(details) if details else None,
            ),
        )


def list_copies(limit=50):
    c = _conn()
    rows = c.execute(
        "SELECT * FROM copy_history ORDER BY finished_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_custom_name(hash):
    if not hash:
        return None
    cached = _custom_names_cache_get()
    if cached is not None:
        return cached.get(hash)
    c = _conn()
    row = c.execute(
        "SELECT custom_name FROM custom_names WHERE hash = ?",
        (hash,),
    ).fetchone()
    return row["custom_name"] if row else None


def get_custom_names_map():
    cached = _custom_names_cache_get()
    if cached is not None:
        return dict(cached)
    c = _conn()
    rows = c.execute("SELECT hash, custom_name FROM custom_names").fetchall()
    fresh = {r["hash"]: r["custom_name"] for r in rows}
    _custom_names_cache_set(fresh)
    return dict(fresh)


def set_custom_name(hash, custom_name, default_name=None):
    if not hash:
        raise ValueError("hash is required")
    with _tx() as c:
        c.execute(
            """INSERT INTO custom_names (hash, custom_name, default_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                custom_name = excluded.custom_name,
                default_name = COALESCE(excluded.default_name, custom_names.default_name),
                updated_at = excluded.updated_at""",
            (hash, custom_name, default_name, _now_iso()),
        )
    _custom_names_cache_invalidate()


def delete_custom_name(hash):
    if not hash:
        return
    with _tx() as c:
        c.execute("DELETE FROM custom_names WHERE hash = ?", (hash,))
    _custom_names_cache_invalidate()


def record_removed_torrent(hash, name, magnet_link, total_size=None,
                           download_dir=None, custom_name=None,
                           deleted_local_data=False, uploaded_ever=None):
    with _tx() as c:
        c.execute(
            """INSERT INTO removed_torrents
            (hash, name, custom_name, magnet_link, total_size,
             download_dir, removed_at, deleted_local_data, uploaded_ever)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                name = excluded.name,
                custom_name = excluded.custom_name,
                magnet_link = COALESCE(excluded.magnet_link, removed_torrents.magnet_link),
                total_size = excluded.total_size,
                download_dir = excluded.download_dir,
                removed_at = excluded.removed_at,
                deleted_local_data = excluded.deleted_local_data,
                uploaded_ever = excluded.uploaded_ever""",
            (
                hash,
                name,
                custom_name,
                magnet_link,
                int(total_size) if total_size is not None else None,
                download_dir,
                _now_iso(),
                1 if deleted_local_data else 0,
                int(uploaded_ever) if uploaded_ever is not None else None,
            ),
        )


def list_removed_torrents(limit=100):
    c = _conn()
    rows = c.execute(
        "SELECT * FROM removed_torrents ORDER BY removed_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_removed_torrent(id):
    c = _conn()
    row = c.execute(
        "SELECT * FROM removed_torrents WHERE id = ?", (int(id),),
    ).fetchone()
    return dict(row) if row else None


def delete_removed_torrent(id):
    with _tx() as c:
        c.execute("DELETE FROM removed_torrents WHERE id = ?", (int(id),))


def delete_removed_torrent_by_hash(hash):
    if not hash:
        return
    with _tx() as c:
        c.execute("DELETE FROM removed_torrents WHERE hash = ?", (hash,))


def record_unloaded_torrent(hash, name, magnet_link, total_size=None,
                            percent_done=None, download_dir=None,
                            custom_name=None, labels=None):
    """Park a torrent outside Transmission: keep everything needed to
    re-add it later (magnet + download dir so existing data is found).
    Upserts on hash so unload → load → unload doesn't duplicate."""
    if not magnet_link:
        raise ValueError("magnet_link is required")
    with _tx() as c:
        c.execute(
            """INSERT INTO unloaded_torrents
            (hash, name, custom_name, magnet_link, total_size,
             percent_done, download_dir, labels, unloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                name = excluded.name,
                custom_name = excluded.custom_name,
                magnet_link = excluded.magnet_link,
                total_size = excluded.total_size,
                percent_done = excluded.percent_done,
                download_dir = excluded.download_dir,
                labels = excluded.labels,
                unloaded_at = excluded.unloaded_at""",
            (
                hash,
                name,
                custom_name,
                magnet_link,
                int(total_size) if total_size is not None else None,
                float(percent_done) if percent_done is not None else None,
                download_dir,
                json.dumps(labels) if labels else None,
                _now_iso(),
            ),
        )


def _unloaded_row_to_dict(r):
    d = dict(r)
    if d.get("labels"):
        try:
            d["labels"] = json.loads(d["labels"])
        except (ValueError, TypeError):
            d["labels"] = []
    else:
        d["labels"] = []
    return d


def list_unloaded_torrents():
    c = _conn()
    rows = c.execute(
        "SELECT * FROM unloaded_torrents ORDER BY unloaded_at DESC",
    ).fetchall()
    return [_unloaded_row_to_dict(r) for r in rows]


def get_unloaded_torrent(id):
    c = _conn()
    row = c.execute(
        "SELECT * FROM unloaded_torrents WHERE id = ?", (int(id),),
    ).fetchone()
    return _unloaded_row_to_dict(row) if row else None


def delete_unloaded_torrent(id):
    with _tx() as c:
        c.execute("DELETE FROM unloaded_torrents WHERE id = ?", (int(id),))


def delete_unloaded_torrent_by_hash(hash):
    if not hash:
        return
    with _tx() as c:
        c.execute("DELETE FROM unloaded_torrents WHERE hash = ?", (hash,))


def get_lifetime_stats():
    """Aggregate copy counters used by the System page's stats card."""
    c = _conn()
    copy = c.execute(
        """SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done,
            COALESCE(SUM(CASE WHEN status = 'done'
                              THEN bytes_transferred ELSE 0 END), 0) AS bytes
        FROM copy_history"""
    ).fetchone()
    return {
        "copies_total": copy["total"],
        "copies_done": copy["done"],
        "copy_bytes": copy["bytes"],
    }


def list_events(limit=200, since=None):
    c = _conn()
    if since:
        rows = c.execute(
            "SELECT * FROM events WHERE ts > ? ORDER BY ts DESC LIMIT ?",
            (since, int(limit)),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


# ---------- caches ----------
#
# get_custom_names_map() is called on every /api/torrents poll (5s). The
# cache is invalidated by set/delete writes, so reads against a steady
# state cost nothing.

_custom_names_lock = threading.Lock()
_custom_names_cache = None


def _custom_names_cache_get():
    with _custom_names_lock:
        return None if _custom_names_cache is None else dict(_custom_names_cache)


def _custom_names_cache_set(m):
    global _custom_names_cache
    with _custom_names_lock:
        _custom_names_cache = dict(m)


def _custom_names_cache_invalidate():
    global _custom_names_cache
    with _custom_names_lock:
        _custom_names_cache = None
