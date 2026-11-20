"""The always-on core process (`python -m waggle.core run`).

Start: single-instance lock -> runtime.db -> plug-ins register -> guard check -> jobs.recover ->
outbox.recover -> timers.catch_up (report kept in meta core.last_catch_up) -> health server -> loops.

Loops (daemon threads; cadences in settings.Cadence):
  heartbeat     main thread, meta.heartbeat every 15 s; also watches for the stop file every 0.5 s
  scheduler     timers.fire_due every 5 s
  worker-N      claim and run registered in-process kinds (default 4 threads; 1 s idle poll)
  dispatcher    claim registered detached kinds and start `python -m waggle.core.jobrun <job> <owner>`
  sender        outbox rows through registered transports; one no-transport event per row
  inbound       inbox messages through the inbound handler: one thread per chat with waiting messages (up
                to 8 chats at once), each running that chat's messages in arrival order
  leases        every 5 s: renew in-process leases, flag overdue in-process jobs, kill timed-out detached ones
  housekeeping  every 60 s: jobs.recover, outbox.recover, outbox.expire
  guard         guard.verify hourly
Retention (done jobs and sent/expired outbox rows older than 30 days, events older than 90 days) runs as the
daily `core.retention` timer, so a missed night is caught up at the next start.

Stop: the stop file (core.stop in the Waggle home) -> no new work is admitted (POST /turn answers 503),
in-flight sends, turns and jobs get up to 5 s, exit 0. Detached workers keep running and are re-adopted by the next core.
"""
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from waggle.core import (events, health, inbound, jobs, ledger, metrics, outbox, plugins, procs, sender, settings,
                         store, timers)

REPO = settings.REPO
RETENTION_S = 30 * 86400
EXIT_ALREADY_RUNNING = 3
LOCK_WAIT_S = 2.0  # a watchdog probing the lock holds it for an instant; do not refuse because of that

log = logging.getLogger("waggle.core")


def code_version():
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5, creationflags=procs.NO_WINDOW)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    import waggle
    return getattr(waggle, "__version__", "unknown")


def setup_logging(name="core.log"):
    logger = logging.getLogger("waggle.core")
    if any(getattr(h, "_waggle_core", False) for h in logger.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(settings.path(name), maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(threadName)s %(levelname)s %(message)s"))
    handler._waggle_core = True
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def retention(payload=None):
    """The daily `core.retention` job."""
    now = time.time()
    pruned_jobs = jobs.prune(RETENTION_S, now)
    pruned_out = outbox.prune(RETENTION_S, now)
    rolled = metrics.rollup_events(now)
    metrics.incr("retention.jobs_pruned", pruned_jobs)
    metrics.incr("retention.outbox_pruned", pruned_out)
    result = {"jobs": pruned_jobs, "outbox": pruned_out, "events": rolled["events"]}
    events.emit("retention.pruned", source="core", data=result)
    return result


# ---- guard --------------------------------------------------------------------------------------------------

def _violations(result):
    if result is None or result is True:
        return []
    if result is False:
        return ["guard.verify() returned False"]
    if isinstance(result, dict):
        found = result.get("violations") or []
        if not found and result.get("ok") is False:
            found = ["guard.verify() reported not ok"]
        return [str(v) for v in found]
    if isinstance(result, (list, tuple, set)):
        return [str(v) for v in result]
    found = getattr(result, "violations", None)
    if found is not None:
        return [str(v) for v in found]
    if getattr(result, "ok", True) is False:
        return ["guard.verify() reported not ok"]
    return []


def check_guard(now=None):
    """Run guard.verify() if the guard is installed. Violations (or a guard that errors) freeze the safety core
    until a later clean check. Returns the status saved in meta guard.status."""
    import importlib
    now = store.now_or(now)
    try:
        guard = importlib.import_module("waggle.guard")
    except ModuleNotFoundError as e:
        if e.name not in ("waggle.guard", "waggle"):
            guard, violations = None, [f"guard cannot load: {e}"]
        else:
            status = {"status": "not installed", "checked_at": now}
            store.set_meta("guard.status", json.dumps(status))
            return status
    else:
        try:
            violations = _violations(guard.verify())
        except Exception as e:  # a guard that cannot check fails closed
            violations = [f"guard error: {type(e).__name__}: {e}"]
    if violations:
        events.emit("integrity_violation", source="guard", now=now, data={"violations": violations[:50]})
        ledger.record("safety_core", False, method="guard.verify", detail="; ".join(violations)[:1000], now=now)
        store.set_meta("guard.frozen", "1")
        status = {"status": "frozen", "checked_at": now, "violations": len(violations)}
    else:
        ledger.record("safety_core", True, method="guard.verify", ttl_s=3 * 3600, now=now)
        if store.get_meta("guard.frozen") == "1":
            events.emit("guard.unfrozen", source="guard", now=now)
        store.set_meta("guard.frozen", "0")
        status = {"status": "ok", "checked_at": now, "violations": 0}
    store.set_meta("guard.status", json.dumps(status))
    return status


def _catch_up_summary(report):
    return {"at": report["now"], "offline_since": report.get("last_tick"), "fired": len(report["fired"]),
            "late": report["late"][:50], "skipped": report["skipped"][:50]}


# ---- the core ------------------------------------------------------------------------------------------------

class Core:
    def __init__(self, cadence=None, workers=None):
        self.cad = cadence or settings.Cadence.from_env()
        self.n_workers = workers or settings.workers()
        self.stopping = threading.Event()
        self.pid = os.getpid()
        self.started = time.time()
        self.version = None
        self.lock = procs.InstanceLock(settings.path("core.lock"))
        self.loops = {}
        self.threads = {}
        self.inflight = {}
        self.chat_threads = {}
        self.turns = {}
        self.unhealthy = {}
        self.server = None
        self._state_lock = threading.Lock()
        self._last_error = {}

    # -- loop bookkeeping --

    def loops_view(self):
        with self._state_lock:
            return {name: {**info, "alive": self.threads[name].is_alive() if name in self.threads else True}
                    for name, info in self.loops.items()}

    def _tick(self, name):
        with self._state_lock:
            if name in self.loops:
                self.loops[name]["last"] = time.time()

    def _busy(self, name, busy):
        with self._state_lock:
            if name in self.loops:
                self.loops[name]["busy"] = busy

    def _error(self, name, exc):
        log.exception("loop %s failed", name)
        now = time.time()
        if now - self._last_error.get(name, 0) >= 60:
            self._last_error[name] = now
            try:
                events.emit("core.loop_error", source="core", ref=name, data={"error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                log.exception("could not record the loop error")

    def _start_thread(self, name, cadence, body):
        # The loop is published only once its thread runs: /health must never see a registered loop whose
        # thread has not started yet (is_alive() is False then, which read as a stalled loop).
        t = threading.Thread(target=body, name=f"core-{name}", daemon=True)
        t.start()
        with self._state_lock:
            self.threads[name] = t
            self.loops[name] = {"last": time.time(), "cadence": cadence}

    def _periodic(self, name, cadence, fn):
        def body():
            while not self.stopping.is_set():
                try:
                    fn()
                except Exception as e:  # one failed pass must not end the loop
                    self._error(name, e)
                self._tick(name)
                self.stopping.wait(cadence)
        self._start_thread(name, cadence, body)

    def _set_unhealthy(self, key, reason):
        with self._state_lock:
            if reason is None:
                self.unhealthy.pop(key, None)
            else:
                self.unhealthy[key] = reason
            reasons = list(self.unhealthy.values())
        store.set_meta("core.unhealthy", json.dumps({"reasons": reasons, "restart": True, "since": time.time()})
                       if reasons else None)

    # -- start and stop --

    def startup(self):
        deadline = time.time() + LOCK_WAIT_S
        while not self.lock.acquire():
            if time.time() >= deadline:
                return False
            time.sleep(0.1)
        settings.path("core.pid").write_text(json.dumps(
            {"pid": self.pid, "ctime": procs.own_ctime(), "started": self.started}), encoding="utf-8")
        stop_file = settings.path("core.stop")
        if stop_file.exists():
            stop_file.unlink()
        store.connect()
        store.set_meta("core.unhealthy", None)
        self.version = code_version()
        self.heartbeat()
        events.emit("core.started", source="core", data={"pid": self.pid, "version": self.version})
        log.info("core %s starting (pid %s)", self.version, self.pid)

        jobs.register("core.retention", retention, timeout_s=600)
        timers.add("core.retention", "cron", "15 4 * * *", "core.retention", catch_up="latest", origin="core")
        loaded = plugins.load()
        log.info("plug-ins loaded %s failed %s", loaded["loaded"], list(loaded["failed"]))
        check_guard()

        recovered = jobs.recover()
        turns = inbound.recover()
        resent = outbox.recover()
        report = timers.catch_up()
        store.set_meta("core.last_catch_up", json.dumps(_catch_up_summary(report), default=str))
        log.info("recovered jobs %s, turns %s, outbox rows %s; catch-up fired %s", recovered, turns, resent,
                 len(report["fired"]))

        try:
            self.server = health.serve(self.loops_view, accepting_fn=self.accepting_turns)
            ledger.record("core.health_endpoint", True, method="bound 127.0.0.1 at core start", ttl_s=7 * 86400)
        except OSError as e:
            log.error("health server could not start: %s", e)
            events.emit("core.health_failed", source="core", data={"error": str(e), "port": settings.port()})
            ledger.record("core.health_endpoint", False, method="bind at core start", detail=str(e))

        self._periodic("scheduler", self.cad.scheduler, timers.fire_due)
        for i in range(self.n_workers):
            self._start_thread(f"worker-{i}", self.cad.poll, lambda i=i: self._worker(f"worker-{i}"))
        self._start_thread("dispatcher", self.cad.poll, self._dispatcher)
        self._start_thread("sender", self.cad.poll, self._sender)
        self._start_thread("inbound", self.cad.poll, self._inbound)
        self._periodic("leases", self.cad.leases, self.check_leases)
        self._periodic("housekeeping", self.cad.housekeeping, self.housekeeping)
        self._start_thread("guard", self.cad.guard, self._guard_loop)
        return True

    def run(self):
        setup_logging()
        if not self.startup():
            log.warning("another core holds the lock; exiting")
            if sys.stderr:
                print("waggle core: another instance is already running", file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        stop_file = settings.path("core.stop")
        next_beat = time.time() + self.cad.heartbeat
        try:
            while not stop_file.exists():
                if time.time() >= next_beat:
                    try:
                        self.heartbeat()
                    except Exception as e:
                        self._error("heartbeat", e)
                    next_beat = time.time() + self.cad.heartbeat
                time.sleep(self.cad.stop_check)
        except KeyboardInterrupt:
            pass
        self.shutdown()
        return 0

    def heartbeat(self):
        store.set_meta("heartbeat", json.dumps({
            "ts": time.time(), "pid": self.pid, "pid_ctime": procs.own_ctime(), "started": self.started,
            "version": self.version, "loops": self.loops_view(), "stopping": self.stopping.is_set(),
            "in_flight": len(self.inflight), "transports": sender.kinds(),
            "inbound_handler": inbound.handler() is not None}))

    def shutdown(self):
        log.info("stopping")
        self.stopping.set()
        try:
            self.heartbeat()
        except Exception:
            log.exception("final heartbeat failed")
        deadline = time.time() + self.cad.send_grace
        order = ["sender"] + [n for n in self.threads if n.startswith("worker-")]
        for name in order:
            self.threads[name].join(max(0.0, deadline - time.time()))
        with self._state_lock:
            chats = list(self.chat_threads.values())
        for t in chats:
            t.join(max(0.0, deadline - time.time()))
        if self.server is not None:
            self.server.shutdown()
        left = len(self.inflight)
        events.emit("core.stopped", source="core", data={"pid": self.pid, "in_flight_left": left})
        for name in ("core.stop", "core.pid"):
            try:
                settings.path(name).unlink()
            except FileNotFoundError:
                pass
        self.lock.release()
        log.info("stopped (%s jobs left in flight)", left)

    # -- loops --

    def _worker(self, name):
        while not self.stopping.is_set():
            try:
                kinds = jobs.kinds(detached=False)
                owner = jobs.new_owner(f"core-{name}")
                job = jobs.claim(owner, kinds=kinds) if kinds else None
                if job is not None:
                    self._busy(name, True)
                    try:
                        self.run_inprocess(job, owner)
                    finally:
                        self._busy(name, False)
            except Exception as e:
                self._error(name, e)
                job = None
            self._tick(name)
            if job is None:
                self.stopping.wait(self.cad.poll)

    def run_inprocess(self, job, owner):
        h = jobs.handler(job["kind"])
        if h is None:
            jobs.fail(job["id"], owner, f"no handler registered for {job['kind']!r}")
            return
        with self._state_lock:
            self.inflight[job["id"]] = {"owner": owner, "kind": job["kind"], "started": time.time(),
                                        "timeout_s": h.timeout_s, "lease_s": h.lease_s, "overdue": False}
        try:
            result = jobs.invoke(h, job)
        except Exception as e:  # recorded on the job
            log.warning("job %s (%s) failed: %s", job["id"], job["kind"], e)
            jobs.fail(job["id"], owner, f"{type(e).__name__}: {e}")
        else:
            jobs.complete(job["id"], owner, result)
        finally:
            with self._state_lock:
                info = self.inflight.pop(job["id"], None)
            if info and info["overdue"]:
                self._set_unhealthy(f"job:{job['id']}", None)

    def _dispatcher(self):
        while not self.stopping.is_set():
            spawned = False
            try:
                spawned = self.dispatch_one()
            except Exception as e:
                self._error("dispatcher", e)
            self._tick("dispatcher")
            if not spawned:
                self.stopping.wait(self.cad.poll)

    def dispatch_one(self):
        kinds = jobs.kinds(detached=True)
        if not kinds:
            return False
        marks = ",".join("?" * len(kinds))
        running = store.connect().execute(
            f"SELECT COUNT(*) FROM jobs WHERE state='running' AND kind IN ({marks})", kinds).fetchone()[0]
        if running >= self.n_workers:
            return False
        owner = jobs.new_owner("core-detached")
        job = jobs.claim(owner, kinds=kinds)
        if job is None:
            return False
        try:
            pid = procs.spawn_detached([procs.pythonw(), "-m", "waggle.core.jobrun", str(job["id"]), owner],
                                       cwd=REPO, log_path=settings.path("jobrun.log"))
        except OSError as e:
            jobs.fail(job["id"], owner, f"could not start the worker process: {e}")
            return False
        jobs.set_pid(job["id"], owner, pid)
        events.emit("job.spawned", source="core", ref=job["id"], data={"kind": job["kind"], "pid": pid})
        return True

    def _sender(self):
        while not self.stopping.is_set():
            state = None
            try:
                state = sender.send_one()
                if state is None:
                    sender.notice_no_transport()
            except Exception as e:
                self._error("sender", e)
            self._tick("sender")
            if state is None:
                self.stopping.wait(self.cad.poll)

    def check_leases(self, now=None):
        """Renew leases of in-process jobs, flag overdue ones, kill and fail timed-out detached ones."""
        now = store.now_or(now)
        with self._state_lock:
            running = list(self.inflight.items())
        for job_id, info in running:
            if not jobs.heartbeat(job_id, info["owner"], lease_s=info["lease_s"], now=now):
                log.warning("lost the lease of job %s", job_id)
            timeout = info["timeout_s"]
            if timeout and not info["overdue"] and now - info["started"] > timeout:
                info["overdue"] = True
                running_s = round(now - info["started"], 1)
                events.emit("job.overdue", source="core", ref=job_id, now=now,
                            data={"kind": info["kind"], "timeout_s": timeout, "running_s": running_s})
                self._set_unhealthy(f"job:{job_id}", f"job {job_id} ({info['kind']}) is overdue: "
                                                     f"running {running_s} s, timeout {timeout} s")
        self.check_turns(now)
        for row in store.connect().execute("SELECT * FROM jobs WHERE state='running' AND detached=1").fetchall():
            h = jobs.handler(row["kind"])
            if h is None or not h.timeout_s or row["started"] is None or now - row["started"] <= h.timeout_s:
                continue
            killed = procs.kill_tree(row["pid"], row["pid_ctime"])
            state = jobs.fail(row["id"], row["lease_owner"], f"timed out after {h.timeout_s} s", now=now)
            events.emit("job.timeout", source="core", ref=row["id"], now=now,
                        data={"kind": row["kind"], "timeout_s": h.timeout_s, "killed": killed, "state": state})

    def check_turns(self, now=None):
        """Give up turns past the turn timeout and ask for a restart: the stuck handler thread cannot be stopped,
        and its chat stays blocked until the process is replaced."""
        now = store.now_or(now)
        limit = settings.turn_timeout_s()
        with self._state_lock:
            running = list(self.turns.items())
        for turn_id, info in running:
            running_s = now - info["started"]
            if info["overdue"] or running_s <= limit:
                continue
            info["overdue"] = True
            inbound.give_up(turn_id, running_s, now=now)
            self._set_unhealthy(f"turn:{turn_id}", f"turn {turn_id} in chat {info['chat']} ran {running_s:.0f} s "
                                                   f"(limit {limit:.0f} s); its chat is blocked")

    def housekeeping(self):
        jobs.recover()
        inbound.recover()
        outbox.recover()
        outbox.expire()

    def accepting_turns(self):
        return not self.stopping.is_set() and inbound.handler() is not None

    def _inbound(self):
        cap = settings.inbound_chats()
        while not self.stopping.is_set():
            try:
                if inbound.handler() is not None:
                    for chat in inbound.waiting_chats():
                        with self._state_lock:
                            if chat in self.chat_threads or len(self.chat_threads) >= cap:
                                continue
                            t = threading.Thread(target=self._chat_runner, args=(chat,), name=f"core-chat-{chat}",
                                                 daemon=True)
                            self.chat_threads[chat] = t
                        t.start()
            except Exception as e:
                self._error("inbound", e)
            self._tick("inbound")
            inbound.wait_for_work(self.cad.poll)

    def _chat_runner(self, chat):
        """Run one chat's waiting messages in order, then end (a new message starts a new runner)."""
        try:
            while not self.stopping.is_set():
                row = inbound.claim_next(chat)
                if row is None:
                    with self._state_lock:
                        if not inbound.has_waiting(chat):
                            self.chat_threads.pop(chat, None)
                            return
                    continue
                with self._state_lock:
                    self.turns[row["turn_id"]] = {"chat": chat, "started": time.time(), "overdue": False}
                try:
                    inbound.run(row)
                finally:
                    with self._state_lock:
                        info = self.turns.pop(row["turn_id"], None)
                    if info and info["overdue"]:
                        self._set_unhealthy(f"turn:{row['turn_id']}", None)
        except Exception as e:
            self._error("inbound", e)
        with self._state_lock:
            self.chat_threads.pop(chat, None)

    def _guard_loop(self):
        while not self.stopping.wait(self.cad.guard):
            try:
                check_guard()
            except Exception as e:
                self._error("guard", e)
            self._tick("guard")


def main():
    return Core().run()
