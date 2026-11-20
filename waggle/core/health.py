"""Health of the core: a snapshot built from runtime.db (plus live loop state when called inside the core),
served on 127.0.0.1 with a bearer token.

GET /health          the snapshot below
GET /metrics?days=N  metrics_daily for the last N days (default 7)
POST /turn           an inbound message, JSON {chat, address, text, kind, media, platform, origin, dedupe_key}
                     -> 202 {"turn_id", "duplicate"} once it is stored; the turn runs later. 503 while the core
                     is stopping or has no inbound handler (the caller falls back), 400 on a bad body.
                     The turn becomes runnable only if the 202 was written within the accept budget (1 s,
                     settings.accept_budget_s) of the request arriving; later than that it is cancelled (and
                     answered 503 when not yet written), because the caller gives up after 2 s and answers the
                     message itself.
Anything else is 404; without the token everything is 401."""
import hmac
import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from waggle.core import inbound, ledger, sender, settings, store

log = logging.getLogger("waggle.core.health")

MAX_TURN_BODY = 1_000_000

# Seams for tests: the clock that measures the accept budget, a pause before the reply is written, and the
# function that writes it.
_clock = time.monotonic


def _before_reply():
    pass


def _write_reply(handler, data):
    handler.wfile.write(data)
    handler.wfile.flush()


class _Server(ThreadingHTTPServer):
    """Records when each connection was accepted, before a thread is found for it."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.arrivals = {}

    def process_request(self, request, client_address):
        self.arrivals[id(request)] = _clock()
        super().process_request(request, client_address)

    def arrival(self, request):
        return self.arrivals.pop(id(request), _clock())
TURN_FIELDS = {"chat": (str, int), "address": (str, int), "text": (str,), "kind": (str,), "platform": (str,),
               "origin": (str,), "dedupe_key": (str, int)}


def token(create=True):
    """The bearer token in the Waggle home. Only the core creates it (atomically, so two starters never end up
    with different tokens); clients pass create=False and get None before any core has run."""
    p = settings.path("core.token")
    for _ in range(50):
        try:
            existing = p.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            existing = None
        if existing:
            return existing
        if existing is None:
            if not create:
                return None
            try:
                with open(p, "x", encoding="ascii") as f:
                    f.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
            continue
        time.sleep(0.01)  # another process has created the file and is writing it
    raise RuntimeError("core.token exists but stays empty")


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
    if loops is not None:
        transports, has_handler = sender.kinds(), inbound.handler() is not None
    else:
        transports, has_handler = hb.get("transports", []), bool(hb.get("inbound_handler"))
    inbox = inbound.counts(now)

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
            "inbox_waiting": inbox["waiting"],
            "inbox_running": inbox["running"],
        },
        "oldest_waiting_inbox_age_s": inbox["oldest_waiting_age_s"],
        "transports": transports,
        "inbound_handler": has_handler,
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


def parse_turn(raw):
    """Validate a POST /turn body; returns submit() keyword arguments or raises ValueError."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"body is not JSON: {e}") from None
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    for key, types in TURN_FIELDS.items():
        value = body.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, types)):
            raise ValueError(f"{key} has the wrong type")
    chat = str(body.get("chat") or "").strip()
    if not chat:
        raise ValueError("chat is required")
    if body.get("text") is None and body.get("media") is None:
        raise ValueError("text or media is required")
    return {"chat": chat, "text": body.get("text") or "", "address": str(body.get("address") or ""),
            "kind": body.get("kind") or "text", "media": body.get("media"),
            "platform": body.get("platform") or "telegram", "origin": body.get("origin") or "user",
            "dedupe_key": None if body.get("dedupe_key") in (None, "") else str(body["dedupe_key"])}


def serve(loops_fn=None, port=None, accepting_fn=None):
    """Start the health server in a daemon thread; returns the server (call .shutdown() to stop it).
    `accepting_fn()` says whether POST /turn is accepted now (default: when an inbound handler exists)."""
    secret = token()
    accepting_fn = accepting_fn or (lambda: inbound.handler() is not None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("health %s", fmt % args)

        def _send(self, code, body, write=None):
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            (write or (lambda h, d: h.wfile.write(d)))(self, data)

        def _authorized(self):
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(auth.encode(), f"Bearer {secret}".encode())

        def do_POST(self):  # noqa: N802 - http.server API
            arrived = self.server.arrival(self.request)
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if urlparse(self.path).path != "/turn":
                return self._send(404, {"error": "not found"})
            if not accepting_fn():
                return self._send(503, {"error": "not accepting turns now"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_TURN_BODY:
                return self._send(400, {"error": "a JSON body of at most 1 MB is required"})
            try:
                args = parse_turn(self.rfile.read(length))
                result = inbound.submit_pending(**args)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # storage failed: the caller must fall back
                log.exception("turn submission failed")
                return self._send(503, {"error": f"{type(e).__name__}: {e}"})
            if result["duplicate"]:
                return self._send(202, result)
            self._reply_then_confirm(result, arrived)

        def _reply_then_confirm(self, result, arrived):
            turn_id = result["turn_id"]
            budget = settings.accept_budget_s()
            _before_reply()
            late = _clock() - arrived
            if late > budget:
                inbound.cancel(turn_id, f"accept took {late:.2f} s before the reply (budget {budget} s)")
                try:
                    self._send(503, {"error": "too slow to accept; answer it on the other path"})
                except OSError:
                    pass
                return
            try:
                self._send(202, result, write=_write_reply)
            except OSError as e:
                inbound.cancel(turn_id, f"writing the 202 failed: {type(e).__name__}: {e}")
                return
            late = _clock() - arrived
            if late > budget:
                inbound.cancel(turn_id, f"the 202 was written after {late:.2f} s (budget {budget} s)")
                return
            inbound.confirm(turn_id)

        def do_GET(self):  # noqa: N802 - http.server API
            if not self._authorized():
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

    server = _Server(("127.0.0.1", port or settings.port()), Handler)
    threading.Thread(target=server.serve_forever, name="core-health", daemon=True).start()
    return server


def fetch(path="/health", port=None, timeout=3.0, auth=True, body=None):
    """GET (or POST, with a JSON-able `body`) to a running core's health server. Returns (status, body)."""
    import urllib.error
    import urllib.request
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port or settings.port()}{path}", data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    secret = token(create=False) if auth else None
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


