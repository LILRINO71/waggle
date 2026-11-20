"""runtime.db: the one store the core, its workers and its tools share.

SQLite in WAL mode with a 15 s busy timeout. Connections are cached per thread and per database file, opened
in autocommit mode; every write goes through `transaction()`, which takes the write lock up front
(BEGIN IMMEDIATE) so compare-and-set updates never race. A `transaction()` inside another one on the same
thread joins it, so callers can group writes from several modules atomically:

    with store.transaction():
        outbox.enqueue(...)
        events.emit(...)
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import waggle.config as config

DB_NAME = "runtime.db"
BUSY_TIMEOUT_MS = 15000
SCHEMA_VERSION = 4

# Events younger than this (by the real clock) cannot be deleted; the retention job removes older ones.
EVENT_DELETE_GUARD_S = 89 * 86400

MIGRATIONS = {
    # v1: the schema sketched in the architecture review (section 3.2).
    1: [
        "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
        "CREATE TABLE events(id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT '', chat TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', data TEXT)",
        "CREATE INDEX events_ts ON events(ts)",
        "CREATE INDEX events_kind_ts ON events(kind, ts)",
        "CREATE TABLE inbox(update_id INTEGER PRIMARY KEY, ts REAL, chat TEXT, kind TEXT, text TEXT, media TEXT, "
        "state TEXT, turn_id TEXT)",
        "CREATE TABLE chats(chat TEXT PRIMARY KEY, platform TEXT, address TEXT, claude_session TEXT, "
        "session_turns INTEGER NOT NULL DEFAULT 0, model TEXT, last_user REAL)",
        "CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT, "
        "idem_key TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 0, "
        "attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, run_after REAL, "
        "lease_owner TEXT, lease_until REAL, pid INTEGER, pid_ctime INTEGER, result TEXT, error TEXT, "
        "created REAL, updated REAL)",
        "CREATE INDEX jobs_claim ON jobs(state, priority, run_after)",
        "CREATE TABLE timers(id TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL, tz TEXT, next_run REAL, "
        "last_run REAL, catch_up TEXT NOT NULL DEFAULT 'skip', max_late_s REAL, job_kind TEXT NOT NULL, "
        "payload TEXT, enabled INTEGER NOT NULL DEFAULT 1, origin TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT UNIQUE, chat TEXT NOT NULL "
        "DEFAULT '', kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', file TEXT, priority INTEGER NOT NULL "
        "DEFAULT 0, origin TEXT NOT NULL DEFAULT '', not_before REAL, expires REAL, state TEXT NOT NULL DEFAULT "
        "'queued', attempts INTEGER NOT NULL DEFAULT 0, next_try REAL, last_error TEXT, provider_msg_id TEXT, "
        "sent_at REAL)",
        "CREATE TABLE capabilities(name TEXT PRIMARY KEY, verified_at REAL, method TEXT, evidence_ref TEXT, "
        "ttl_s REAL, last_failure_at REAL, last_failure TEXT)",
        "CREATE TABLE metrics_daily(day TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY(day, name))",
    ],
    # v2: bookkeeping the library needs beyond the sketch, and append-only events enforced by the database.
    2: [
        "ALTER TABLE jobs ADD COLUMN detached INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN lease_s REAL",
        "ALTER TABLE jobs ADD COLUMN started REAL",
        "ALTER TABLE jobs ADD COLUMN finished REAL",
        "ALTER TABLE timers ADD COLUMN created REAL",
        "ALTER TABLE timers ADD COLUMN updated REAL",
        "ALTER TABLE timers ADD COLUMN last_job INTEGER",
        "ALTER TABLE outbox ADD COLUMN created REAL",
        "ALTER TABLE outbox ADD COLUMN updated REAL",
        "ALTER TABLE capabilities ADD COLUMN detail TEXT",
        "ALTER TABLE capabilities ADD COLUMN last_failure_method TEXT",
        "ALTER TABLE chats ADD COLUMN updated REAL",
        "CREATE INDEX outbox_due ON outbox(state, priority, next_try)",
        "CREATE INDEX timers_due ON timers(enabled, next_run)",
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
        "CREATE TRIGGER events_no_early_delete BEFORE DELETE ON events "
        f"WHEN OLD.ts > CAST(strftime('%s','now') AS REAL) - {EVENT_DELETE_GUARD_S} "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only until retention'); END",
    ],    # v3: free-form delivery details (title, topic, decision id, reply target) that transports may use.
    3: [
        "ALTER TABLE outbox ADD COLUMN meta TEXT",
    ],
    # v4: a job queued by a process that does not know its kind takes the kind's registered settings
    # (max_attempts, detached) from the process that claims it.
    4: [
        "ALTER TABLE jobs ADD COLUMN defaults_pending INTEGER NOT NULL DEFAULT 0",
    ],
}

_local = threading.local()
_registry_lock = threading.Lock()
_open_conns = []
_generation = 0
_migrated = set()


def db_path():
    """runtime.db inside the Waggle home (config.HOME, so tests' temporary home applies)."""
    return Path(config.data_path(DB_NAME))


def connect(path=None):
    """This thread's connection to runtime.db (or `path`), pragmas applied and schema ensured."""
    key = str(Path(path).resolve()) if path else str(db_path().resolve())
    if getattr(_local, "generation", None) != _generation or getattr(_local, "pid", None) != os.getpid():
        _local.conns, _local.generation, _local.pid = {}, _generation, os.getpid()
    con = _local.conns.get(key)
    if con is None:
        con = _open(key)
        _local.conns[key] = con
        with _registry_lock:
            _open_conns.append(con)
    return con


def _open(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA journal_mode=WAL")
    # FULL rather than NORMAL: a 'sent' mark or an offset lost to a power cut means a duplicate message.
    con.execute("PRAGMA synchronous=FULL")
    if path not in _migrated:
        migrate(con)
        _migrated.add(path)
    return con


def close_all():
    """Close every cached connection in this process (tests; shutdown)."""
    global _generation
    with _registry_lock:
        conns = list(_open_conns)
        _open_conns.clear()
        _generation += 1
        _migrated.clear()
    for con in conns:
        try:
            con.close()
        except sqlite3.Error:
            pass


@contextmanager
def transaction(path=None):
    """A write transaction (BEGIN IMMEDIATE); joins the enclosing one on this thread if there is one."""
    con = connect(path)
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def schema_version(con=None):
    con = con or connect()
    row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    return int(row[0]) if row else 0


def migrate(con):
    """Bring a database up to SCHEMA_VERSION. Refuses a database written by a newer version."""
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        current = schema_version(con)
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"runtime.db has schema {current}; this code knows up to {SCHEMA_VERSION}")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS[version]:
                con.execute(stmt)
        if current != SCHEMA_VERSION:
            con.execute("INSERT INTO meta(k, v) VALUES('schema_version', ?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(SCHEMA_VERSION),))
            insert_event(con, "schema.migrated", source="store",
                         data={"from": current, "to": SCHEMA_VERSION})
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def get_meta(k, default=None):
    row = connect().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(k, v):
    with transaction() as con:
        con.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (k, None if v is None else str(v)))


# ---- helpers shared by the modules --------------------------------------------------------------------------

def now_or(now):
    return time.time() if now is None else float(now)


def dumps(value):
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def new_id():
    make = getattr(uuid, "uuid7", uuid.uuid4)
    return make().hex


def insert_event(con, kind, *, source="", chat="", ref="", data=None, now=None):
    eid = new_id()
    con.execute("INSERT INTO events(id, ts, kind, source, chat, ref, data) VALUES(?,?,?,?,?,?,?)",
                (eid, now_or(now), kind, source or "", str(chat or ""), str(ref or ""), dumps(data)))
    return eid
