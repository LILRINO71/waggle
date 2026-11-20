"""Job queue with leases in runtime.db.

A job is claimed by an owner token under a lease; only that owner may heartbeat, finish or fail it
(`WHERE id=? AND lease_owner=?`). `attempts` counts failed or crashed runs. Recovery requeues a running job
as soon as its recorded process is gone (or the pid now belongs to a different process), whatever its lease
says; the lease only matters while that process lives, and a live worker past its lease is re-adopted.

Handlers are plain functions registered per process with `register`. They receive the job payload, or the
payload and the whole job row (id, attempts, ...) when registered with `with_job=True`.
"""
import builtins
import os
import uuid
from dataclasses import dataclass

from waggle.core import procs, store

DEFAULT_LEASE_S = 60.0
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_S = 30.0
RETRY_CAP_S = 3600.0
STATES = ("queued", "running", "done", "dead")


@dataclass(frozen=True)
class Handler:
    fn: object
    detached: bool
    max_attempts: int
    lease_s: float
    timeout_s: float | None = None
    with_job: bool = False


_handlers = {}


def register(kind, fn, *, detached=False, max_attempts=DEFAULT_MAX_ATTEMPTS, lease_s=DEFAULT_LEASE_S,
             timeout_s=None, with_job=False):
    """Register a job kind. `timeout_s`: a detached run past it is killed and failed; an in-process run past it
    marks the core unhealthy (a thread cannot be killed safely, so the watchdog restarts the core)."""
    _handlers[kind] = Handler(fn, bool(detached), int(max_attempts), float(lease_s),
                              None if timeout_s is None else float(timeout_s), bool(with_job))


def handler(kind):
    return _handlers.get(kind)


def kinds(*, detached=None):
    """Registered kinds; only in-process ones with detached=False, only detached ones with detached=True."""
    return [k for k, h in _handlers.items() if detached is None or h.detached == detached]


def invoke(h, job):
    """Call a handler the way it was registered."""
    return h.fn(job["payload"], job) if h.with_job else h.fn(job["payload"])


def new_owner(prefix="core"):
    """A fresh owner token for one claim: unique per process and per call."""
    return f"{prefix}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def retry_delay(attempts):
    return min(RETRY_BASE_S * 2 ** max(attempts - 1, 0), RETRY_CAP_S)


def enqueue(kind, payload=None, *, idem_key=None, run_after=None, priority=0, max_attempts=None, now=None):
    """Queue a job; with an idem_key already used, returns the existing job's id and changes nothing."""
    if not kind:
        raise ValueError("job kind is required")
    now = store.now_or(now)
    h = _handlers.get(kind)
    pending = max_attempts is None and h is None
    if max_attempts is None:
        max_attempts = h.max_attempts if h else DEFAULT_MAX_ATTEMPTS
    with store.transaction() as con:
        if idem_key is not None:
            row = con.execute("SELECT id FROM jobs WHERE idem_key=?", (idem_key,)).fetchone()
            if row:
                return row[0]
        cur = con.execute(
            "INSERT INTO jobs(kind, payload, idem_key, state, priority, attempts, max_attempts, run_after, "
            "detached, lease_s, defaults_pending, created, updated) VALUES(?,?,?,'queued',?,0,?,?,?,?,?,?,?)",
            (kind, store.dumps(payload), idem_key, int(priority), int(max_attempts),
             None if run_after is None else float(run_after), int(bool(h and h.detached)),
             h.lease_s if h else None, int(pending), now, now))
        return cur.lastrowid


def claim(owner, *, kinds=None, lease_s=None, now=None):
    """Take the next due job (highest priority, then oldest) for `owner`; None when nothing is due."""
    if not owner:
        raise ValueError("owner is required")
    now = store.now_or(now)
    sql = "SELECT * FROM jobs WHERE state='queued' AND (run_after IS NULL OR run_after<=?)"
    args = [now]
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else builtins.list(kinds)
        if not kinds:
            return None
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    sql += " ORDER BY priority DESC, COALESCE(run_after, created) ASC, id ASC LIMIT 1"
    with store.transaction() as con:
        row = con.execute(sql, args).fetchone()
        if row is None:
            return None
        h = _handlers.get(row["kind"])
        lease = float(lease_s or (h.lease_s if h else None) or row["lease_s"] or DEFAULT_LEASE_S)
        con.execute("UPDATE jobs SET state='running', lease_owner=?, lease_until=?, lease_s=?, pid=?, "
                    "pid_ctime=?, started=?, updated=? WHERE id=? AND state='queued'",
                    (owner, now + lease, lease, os.getpid(), procs.own_ctime(), now, now, row["id"]))
        if h is not None and row["defaults_pending"]:
            con.execute("UPDATE jobs SET max_attempts=?, detached=?, defaults_pending=0 WHERE id=?",
                        (h.max_attempts, int(h.detached), row["id"]))
        return get(row["id"])


def heartbeat(job_id, owner, *, lease_s=None, now=None):
    """Extend the lease; False when `owner` no longer holds this job."""
    now = store.now_or(now)
    with store.transaction() as con:
        return con.execute(
            "UPDATE jobs SET lease_until = ? + COALESCE(?, lease_s, ?), updated=? "
            "WHERE id=? AND lease_owner=? AND state='running'",
            (now, lease_s, DEFAULT_LEASE_S, now, job_id, owner)).rowcount == 1


def set_pid(job_id, owner, pid, *, now=None):
    """Record the detached process that now runs this job (its creation time guards against pid reuse)."""
    now = store.now_or(now)
    with store.transaction() as con:
        return con.execute(
            "UPDATE jobs SET pid=?, pid_ctime=?, detached=1, updated=? "
            "WHERE id=? AND lease_owner=? AND state='running'",
            (int(pid), procs.ctime(pid), now, job_id, owner)).rowcount == 1


def complete(job_id, owner, result=None, *, now=None):
    now = store.now_or(now)
    with store.transaction() as con:
        done = con.execute(
            "UPDATE jobs SET state='done', result=?, error=NULL, lease_until=NULL, finished=?, updated=? "
            "WHERE id=? AND lease_owner=? AND state='running'",
            (store.dumps(result), now, now, job_id, owner)).rowcount == 1
        if done:
            kind = con.execute("SELECT kind FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            store.insert_event(con, "job.done", source="jobs", ref=job_id, data={"kind": kind}, now=now)
        return done


def fail(job_id, owner, error, *, retry=True, now=None):
    """Record a failed run: back to 'queued' with backoff while attempts remain, else 'dead'.

    Returns the new state, or None when `owner` does not hold the job."""
    now = store.now_or(now)
    with store.transaction() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=? AND lease_owner=? AND state='running'",
                          (job_id, owner)).fetchone()
        if row is None:
            return None
        return _end_attempt(con, row, str(error), retry=retry, now=now, reason="failed")


def _end_attempt(con, row, error, *, retry, now, reason, delay=None):
    attempts = row["attempts"] + 1
    data = {"kind": row["kind"], "attempts": attempts, "max_attempts": row["max_attempts"], "error": error,
            "reason": reason}
    if retry and attempts < row["max_attempts"]:
        run_after = now + (retry_delay(attempts) if delay is None else delay)
        con.execute("UPDATE jobs SET state='queued', attempts=?, error=?, run_after=?, lease_owner=NULL, "
                    "lease_until=NULL, pid=NULL, pid_ctime=NULL, updated=? WHERE id=?",
                    (attempts, error, run_after, now, row["id"]))
        store.insert_event(con, "job.retried", source="jobs", ref=row["id"], data={**data, "run_after": run_after},
                           now=now)
        return "queued"
    con.execute("UPDATE jobs SET state='dead', attempts=?, error=?, lease_until=NULL, finished=?, updated=? "
                "WHERE id=?", (attempts, error, now, now, row["id"]))
    store.insert_event(con, "job.dead", source="jobs", ref=row["id"], data=data, now=now)
    return "dead"


def recover(now=None):
    """Run at core start and periodically: settle running jobs.

    Process gone, or its pid reused by another process -> queued at once with attempts+1 (dead when exhausted),
    whatever the lease says. The recorded process still running past its lease -> left running and re-adopted
    with a fresh lease."""
    now = store.now_or(now)
    counts = {"requeued": 0, "dead": 0, "adopted": 0}
    with store.transaction() as con:
        rows = con.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
        for row in rows:
            if procs.same_process(row["pid"], row["pid_ctime"]):
                if row["lease_until"] is not None and row["lease_until"] >= now:
                    continue
                lease = row["lease_s"] or DEFAULT_LEASE_S
                con.execute("UPDATE jobs SET lease_until=?, updated=? WHERE id=? AND state='running'",
                            (now + lease, now, row["id"]))
                store.insert_event(con, "job.adopted", source="jobs", ref=row["id"],
                                   data={"kind": row["kind"], "pid": row["pid"], "owner": row["lease_owner"]},
                                   now=now)
                counts["adopted"] += 1
                continue
            state = _end_attempt(con, row, "lease expired and its process is gone", retry=True, now=now,
                                 reason="crashed", delay=0)
            counts["requeued" if state == "queued" else "dead"] += 1
        if any(counts.values()):
            store.insert_event(con, "jobs.recovered", source="jobs", data=counts, now=now)
    return counts


def run_one(owner=None, *, kinds=None, now=None):
    """Claim one job of a registered in-process kind, run its handler, and record the outcome.

    Returns the finished job row, or None when nothing was due. Detached kinds are not run here."""
    if kinds is None:
        kinds = [k for k, h in _handlers.items() if not h.detached]
    owner = owner or new_owner()
    job = claim(owner, kinds=kinds, now=now)
    if job is None:
        return None
    h = _handlers.get(job["kind"])
    if h is None:
        fail(job["id"], owner, f"no handler registered for {job['kind']!r}", retry=True, now=now)
        return get(job["id"])
    try:
        result = invoke(h, job)
    except Exception as e:  # a handler failure is recorded on the job, never raised into the scheduler
        fail(job["id"], owner, f"{type(e).__name__}: {e}", now=now)
    else:
        complete(job["id"], owner, result, now=now)
    return get(job["id"])


def prune(older_than_s, now=None):
    """Delete done jobs finished more than `older_than_s` ago. Returns how many."""
    cutoff = store.now_or(now) - older_than_s
    with store.transaction() as con:
        return con.execute("DELETE FROM jobs WHERE state='done' AND COALESCE(finished, updated) < ?",
                           (cutoff,)).rowcount


def get(job_id):
    row = store.connect().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row(row) if row else None


def list(state=None, kind=None, limit=100):  # noqa: A001 - the public API name
    """Most recent jobs first."""
    where, args = [], []
    if state is not None:
        where.append("state=?")
        args.append(state)
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_row(r) for r in store.connect().execute(sql, args).fetchall()]


def _row(r):
    d = dict(r)
    d["payload"] = store.loads(d["payload"])
    d["result"] = store.loads(d["result"])
    d["detached"] = bool(d.get("detached"))
    return d
