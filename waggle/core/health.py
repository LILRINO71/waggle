"""Health of the core: a snapshot built from runtime.db (plus live loop state when called inside the core),
served on 127.0.0.1 with a bearer token.

GET /health          the snapshot below
GET /metrics?days=N  metrics_daily for the last N days (default 7)
Anything else is 404; without the token everything is 401."""
import hmac
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from waggle.core import ledger, settings, store

log = logging.getLogger("waggle.core.health")

HEARTBEAT_STALE_S = 120.0


def token():
    """The bearer token, generated on first use into the Waggle home."""
    p = settings.path("core.token")
    try:
        existing = p.read_text(encoding="ascii").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    value = secrets.token_urlsafe(32)
    p.write_text(value, encoding="ascii")
    return value


def _json_meta(key):
    value = store.get_meta(key)
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def snapshot(loops=None, now=None):
    """Everything /health reports. `loops` is the live loop table when called inside the core; otherwise the
    one saved with the last heartbeat is used."""
    now = store.now_or(now)
    con = store.connect()
    hb = _json_meta("heartbeat") or {}
    cadence = settings.Cadence.from_env()
    hb_age = now - hb["ts"] if hb.get("ts") else None
    loop_table = loops if loops is not None else hb.get("loops", {})
    loop_out = {}
    for name, info in loop_table.items():
        age = now - info["last"] if info.get("last") else None
        limit = max(3 * info.get("cadence", cadence.poll), 30.0)
        loop_out[name] = {"age_s": age, "cadence_s": info.get("cadence"),
                          "busy": bool(info.get("busy")),
                          "alive": bool(info.get("alive", True)) and (bool(info.get("busy")) or
                                                                      (age is not None and age <= limit))}

    def one(sql, *args):
        return con.execute(sql, args).fetchone()[0]

    oldest_job = one("SELECT MIN(COALESCE(run_after, created)) FROM jobs WHERE state='queued' "
                     "AND (run_after IS NULL OR run_after<=?)", now)
    oldest_out = one("SELECT MIN(COALESCE(next_try, created)) FROM outbox WHERE state='queued'")
    guard = _json_meta("guard.status") or {"status": "not checked"}
    guard["frozen"] = store.get_meta("guard.frozen") == "1"
    unhealthy = _json_meta("core.unhealthy")
    ready = {r["name"]: r["ready"] for r in ledger.status(now)}

    reasons = []
    if hb_age is None:
        reasons.append("no heartbeat")
    elif hb_age > max(3 * cadence.heartbeat, 45.0):
        reasons.append(f"heartbeat {hb_age:.0f} s old")
    if hb.get("stopping"):
        reasons.append("stopping")
    reasons += [f"loop {n} not ticking" for n, v in loop_out.items() if not v["alive"]]
    if unhealthy:
        reasons += unhealthy.get("reasons", [])
    if guard["frozen"]:
        reasons.append("safety core frozen by the guard")

    return {
        "healthy": not reasons,
        "reasons": reasons,
        "now": now,
        "pid": hb.get("pid"),
        "version": hb.get("version"),
        "started": hb.get("started"),
        "heartbeat_age_s": hb_age,
        "loops": loop_out,
        "queues": {
            "jobs_queued": one("SELECT COUNT(*) FROM jobs WHERE state='queued'"),
            "jobs_running": one("SELECT COUNT(*) FROM jobs WHERE state='running'"),
            "outbox_queued": one("SELECT COUNT(*) FROM outbox WHERE state='queued'"),
            "outbox_sending": one("SELECT COUNT(*) FROM outbox WHERE state='sending'"),
        },
        "oldest_queued_job_age_s": None if oldest_job is None else now - oldest_job,
        "oldest_queued_outbox_age_s": None if oldest_out is None else max(0.0, now - oldest_out),
        "dead_jobs_24h": one("SELECT COUNT(*) FROM events WHERE kind='job.dead' AND ts>=?", now - 86400),
        "failed_sends_24h": one("SELECT COUNT(*) FROM events WHERE kind IN ('outbox.failed', 'outbox.expired') "
                                "AND ts>=?", now - 86400),
        "guard": guard,
        "ready": ready,
        "unhealthy_flag": unhealthy,
        "last_catch_up": _json_meta("core.last_catch_up"),
    }


def metrics_days(days=7):
    con = store.connect()
    wanted = [r[0] for r in con.execute("SELECT DISTINCT day FROM metrics_daily ORDER BY day DESC LIMIT ?",
                                        (int(days),)).fetchall()]
    out = {}
    for day in sorted(wanted):
        rows = con.execute("SELECT name, value FROM metrics_daily WHERE day=? ORDER BY name", (day,)).fetchall()
        out[day] = {r[0]: r[1] for r in rows}
    return out


def serve(loops_fn=None, port=None):
    """Start the health server in a daemon thread; returns the server (call .shutdown() to stop it)."""
    secret = token()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("health %s", fmt % args)

        def _send(self, code, body):
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - http.server API
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {secret}".encode()):
                return self._send(401, {"error": "unauthorized"})
            url = urlparse(self.path)
            try:
                if url.path == "/health":
                    return self._send(200, snapshot(loops_fn() if loops_fn else None))
                if url.path == "/metrics":
                    days = int(parse_qs(url.query).get("days", ["7"])[0])
                    return self._send(200, metrics_days(max(1, min(days, 366))))
            except Exception as e:  # a broken snapshot is reported, not a dropped connection
                log.exception("health request failed")
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", port or settings.port()), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="core-health", daemon=True).start()
    return server


def fetch(path="/health", port=None, timeout=3.0, auth=True):
    """GET from a running core's health server. Returns (status, body)."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port or settings.port()}{path}")
    if auth:
        req.add_header("Authorization", f"Bearer {token()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


