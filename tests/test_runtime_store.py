import sqlite3
import threading
import time
from pathlib import Path

import pytest

import waggle.config as config
from waggle.core import events, metrics, store


@pytest.fixture(autouse=True)
def _fresh_connections():
    yield
    store.close_all()


def test_db_lives_in_the_test_home_not_the_real_one():
    path = store.db_path()
    assert path.parent == Path(config.HOME)
    assert path.parent != Path.home() / ".waggle"
    store.connect()
    assert path.exists()


def test_pragmas_and_schema():
    con = store.connect()
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    assert store.schema_version() == store.SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("events", "inbox", "meta", "chats", "jobs", "timers", "outbox", "capabilities", "metrics_daily"):
        assert t in tables


def test_connection_is_per_thread():
    main = store.connect()
    assert store.connect() is main
    other = []
    t = threading.Thread(target=lambda: other.append(store.connect()))
    t.start()
    t.join()
    assert other[0] is not main


def test_transaction_rolls_back_and_nests():
    with pytest.raises(RuntimeError):
        with store.transaction():
            events.emit("a.b")
            raise RuntimeError("boom")
    assert events.query("a.b") == []
    with store.transaction():
        events.emit("x.one")
        with store.transaction():
            events.emit("x.two")
    assert [e["kind"] for e in events.query("x.*")] == ["x.one", "x.two"]


def test_meta_roundtrip():
    assert store.get_meta("telegram_offset") is None
    store.set_meta("telegram_offset", 42)
    assert store.get_meta("telegram_offset") == "42"


def _v1_database(path):
    con = sqlite3.connect(path, isolation_level=None)
    for stmt in store.MIGRATIONS[1]:
        con.execute(stmt)
    con.execute("INSERT INTO meta(k, v) VALUES('schema_version', '1')")
    con.execute("INSERT INTO jobs(kind, payload, idem_key, created, updated) VALUES('old', '{}', 'k1', 1, 1)")
    con.execute("INSERT INTO outbox(kind, body, idem_key) VALUES('text', 'hi', 'o1')")
    con.execute("INSERT INTO events(id, ts, kind) VALUES('e1', 1, 'legacy')")
    con.close()


def test_upgrades_a_version_1_database(tmp_path):
    path = tmp_path / "old.db"
    _v1_database(path)
    con = store.connect(path)
    assert store.schema_version(con) == 2
    job = con.execute("SELECT * FROM jobs WHERE idem_key='k1'").fetchone()
    assert job["kind"] == "old" and job["detached"] == 0 and job["lease_s"] is None
    assert con.execute("SELECT body FROM outbox WHERE idem_key='o1'").fetchone()[0] == "hi"
    migrated = con.execute("SELECT data FROM events WHERE kind='schema.migrated'").fetchone()
    assert store.loads(migrated[0]) == {"from": 1, "to": 2}
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE events SET kind='changed' WHERE id='e1'")


def test_refuses_a_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO meta VALUES('schema_version', '99')")
    con.close()
    with pytest.raises(RuntimeError, match="schema 99"):
        store.connect(path)


# ---- events ------------------------------------------------------------------------------------------------

def test_emit_and_query_filters():
    events.emit("job.dead", source="jobs", ref="7", data={"why": "x"}, now=100)
    events.emit("job.retried", source="jobs", ref="8", now=200)
    events.emit("outbox.failed", chat="c1", ref="3", now=300)
    assert [e["kind"] for e in events.query(since=0) if e["ts"] < 1000] == ["job.dead", "job.retried",
                                                                            "outbox.failed"]
    assert [e["ref"] for e in events.query("job.*")] == ["7", "8"]
    assert events.query("job.dead")[0]["data"] == {"why": "x"}
    assert [e["kind"] for e in events.query(since=150) if e["ts"] < 1000] == ["job.retried", "outbox.failed"]
    assert [e["kind"] for e in events.query(chat="c1")] == ["outbox.failed"]
    assert [e["kind"] for e in events.query(ref="8")] == ["job.retried"]
    assert [e["kind"] for e in events.query("*", limit=2)][0] == "outbox.failed"


def test_events_are_append_only():
    eid = events.emit("x", now=time.time())
    con = store.connect()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE events SET kind='y' WHERE id=?", (eid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM events WHERE id=?", (eid,))
    assert len(events.query("x")) == 1


# ---- metrics -----------------------------------------------------------------------------------------------

def test_incr_set_get():
    metrics.incr("turns", day="2026-11-19")
    metrics.incr("turns", 2, day="2026-11-19")
    metrics.set("uptime_pct", 97.5, day="2026-11-19")
    metrics.set("uptime_pct", 99.0, day="2026-11-19")
    assert metrics.get("2026-11-19", "turns") == 3
    assert metrics.get("2026-11-19", "uptime_pct") == 99.0
    assert metrics.get("2026-11-19", "missing") is None
    assert metrics.for_day("2026-11-19") == {"turns": 3.0, "uptime_pct": 99.0}
    metrics.incr("today_count")
    assert metrics.get(metrics.day_of(), "today_count") == 1


def test_rollup_moves_old_events_into_daily_metrics():
    now = time.time()
    old = now - 100 * 86400
    events.emit("job.dead", now=old)
    events.emit("job.dead", now=old + 1)
    events.emit("timer.late", now=old + 2)
    events.emit("job.dead", now=now - 86400)
    out = metrics.rollup_events(now=now)
    assert out == {"events": 3, "days": 1}
    day = metrics.day_of(old)
    assert metrics.get(day, "events.job.dead") == 2
    assert metrics.get(day, "events.timer.late") == 1
    assert [e["ts"] for e in events.query("job.dead")] == [now - 86400]
    assert metrics.rollup_events(now=now) == {"events": 0, "days": 0}
    assert metrics.get(day, "events.job.dead") == 2


def test_rollup_never_deletes_young_events_even_with_a_future_clock():
    events.emit("recent", now=time.time() - 10 * 86400)
    assert metrics.rollup_events(now=time.time() + 400 * 86400)["events"] == 0
    assert len(events.query("recent")) == 1
