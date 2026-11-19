"""One place for the knowledge pipeline's own state: ~/.waggle/knowledge.db.

- state      what ran when, source hashes, per-job bookkeeping (ingest, digest, learning, quiz)
- facts      the fact model (learn.py): durable facts and dated events, with status and history
- retrieval  which notes were injected, searched or read on every turn, with scores (memory.py)
- growth     one row per day: notes, facts, quiz score, skills used ... (learning.py, shown on the HQ page)

Everything here is derived or bookkeeping; the vault stays the readable memory."""
import json
import sqlite3
import time

from .config import data_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT, time REAL);
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, kind TEXT, key TEXT,
    text TEXT, date TEXT, expires TEXT, source TEXT, evidence TEXT, status TEXT DEFAULT 'active',
    replaced_by INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS facts_status ON facts(status, kind);
CREATE TABLE IF NOT EXISTS retrieval (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, session TEXT, mode TEXT,
    query TEXT, used_query TEXT, follow_up INTEGER, paths TEXT, top REAL, cutoff REAL, candidates INTEGER);
CREATE INDEX IF NOT EXISTS retrieval_time ON retrieval(time);
CREATE TABLE IF NOT EXISTS growth (day TEXT PRIMARY KEY, time REAL, data TEXT);
"""


def db():
    con = sqlite3.connect(data_path("knowledge.db"), timeout=20)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get(key, default=None):
    con = db()
    try:
        row = con.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    finally:
        con.close()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except ValueError:
        return default


def put(key, value):
    con = db()
    try:
        con.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (key, json.dumps(value, default=str), time.time()))
        con.commit()
    finally:
        con.close()


def set_growth(day, data):
    con = db()
    try:
        old = con.execute("SELECT data FROM growth WHERE day=?", (day,)).fetchone()
        merged = {**(json.loads(old["data"]) if old else {}), **data}
        con.execute("INSERT OR REPLACE INTO growth VALUES (?,?,?)", (day, time.time(), json.dumps(merged)))
        con.commit()
    finally:
        con.close()
    return merged


def growth(days=14):
    con = db()
    try:
        rows = con.execute("SELECT day, data FROM growth ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    finally:
        con.close()
    return [{"day": r["day"], **json.loads(r["data"])} for r in reversed(rows)]
