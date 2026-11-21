"""autonomy.db: the one SQLite file the autonomous-work modules share (activity caches, goals, steps, indicator
readings, repeated-work candidates, settings). It lives in the Waggle home next to the other module databases and
is never part of the repository.

Tables
  settings      k/v: autonomy_enabled (default off), max_steps_per_day
  cc_files      one row per Claude Code session log read: size/mtime/offset and the derived facts (never content)
  reflog        per repository HEAD log: mtime and recent commit times (no names, no messages)
  goals         standing goals with indicators and a charter
  steps         every step the work loop attempted, with its proof and outcome
  readings      indicator values measured by code, with time
  candidates    repeated work found by the miner and its proof / shadow-run state
"""
import json
import sqlite3
import time

from .config import data_path

DB = "autonomy.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cc_files (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, offset INTEGER,
    data TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS reflog (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, data TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY, title TEXT NOT NULL, why TEXT, indicators TEXT,
    charter TEXT, status TEXT NOT NULL DEFAULT 'disabled', created REAL, updated REAL, last_step REAL);
CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT, time REAL, day TEXT,
    title TEXT, action TEXT, objective_id TEXT, outcome TEXT, detail TEXT, evidence TEXT, units REAL DEFAULT 0,
    indicators TEXT);
CREATE INDEX IF NOT EXISTS steps_goal ON steps(goal_id, time);
CREATE TABLE IF NOT EXISTS readings (goal_id TEXT, indicator TEXT, time REAL, value REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS readings_goal ON readings(goal_id, indicator, time);
CREATE TABLE IF NOT EXISTS candidates (key TEXT PRIMARY KEY, kind TEXT, label TEXT, count INTEGER, days INTEGER,
    sessions INTEGER, workstreams TEXT, evidence TEXT, recipe TEXT, status TEXT NOT NULL DEFAULT 'found',
    proof TEXT, shadow_runs INTEGER NOT NULL DEFAULT 0, shadow_log TEXT, created REAL, updated REAL);
"""


def db():
    con = sqlite3.connect(data_path(DB), timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get(k, default=None):
    con = db()
    try:
        row = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    finally:
        con.close()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return row[0]


def put(k, v):
    con = db()
    try:
        con.execute("INSERT INTO settings(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (k, json.dumps(v)))
        con.commit()
    finally:
        con.close()


def dumps(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def now_or(now):
    return time.time() if now is None else float(now)
