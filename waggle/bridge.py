"""The bridge between Claude agents and Hermes's tools.

The Hermes gateway already has every tool configured and working (cua-driver computer use, browser, vision,
cron, web search, Waggle's own tools). The plugin starts this tiny HTTP server *inside* the gateway, bound to
127.0.0.1 with a random token, so the Waggle MCP server can ask the gateway to run a tool for a Claude agent.

- POST /call  {"name", "args"}  -> {"ok", "result"} (policy-checked; "never" rules are enforced here too)
- GET  /tools?q=...             -> [{"name", "description", "parameters"}]
- GET  /health                  -> {"ok": true}
Connection details live in ~/.waggle/bridge.json (port, token, pid), readable only by the user's account.
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import policy
from .config import data_path

INFO_FILE = "bridge.json"
# Claude Code has better versions of these built in (and they go through the PreToolUse policy hook).
HIDDEN = {"terminal", "process", "read_file", "write_file", "patch", "search_files", "execute_code",
          "tool_search", "tool_describe", "clarify"}

_server = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "WaggleBridge/1"

    def log_message(self, *a):  # keep the gateway log clean
        pass

    def _send(self, code, body):
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return secrets.compare_digest(self.headers.get("X-Waggle-Token", ""), self.server.token)

    def do_GET(self):
        if not self._authorized():
            return self._send(403, {"ok": False, "error": "bad token"})
        url = urllib.parse.urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"ok": True, "pid": os.getpid()})
        if url.path == "/tools":
            q = (urllib.parse.parse_qs(url.query).get("q") or [""])[0]
            return self._send(200, {"ok": True, "tools": search_tools(self.server.list_tools(), q)})
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(403, {"ok": False, "error": "bad token"})
        if self.path != "/call":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad json"})
        name, args = req.get("name", ""), req.get("args") or {}
        if name in HIDDEN:
            return self._send(200, {"ok": False, "error": f"{name} is not exposed; use your built-in tools"})
        # The agent's PreToolUse hook already asked for approvals; here only the hard "never" rules apply.
        d = policy.decide(name, args)
        if d.action == policy.DENY:
            policy.audit(name, args, d, source="bridge")
            return self._send(200, {"ok": False, "error": f"Blocked by Waggle's safety policy: {d.reason}"})
        if self.server.pre:
            directive = self.server.pre(name, args) or {}
            if directive.get("action") == "block":
                return self._send(200, {"ok": False, "error": directive.get("message", "blocked")})
            if directive.get("action") == "modify":
                args = {**args, **(directive.get("args") or {})}
        t0 = time.time()
        try:
            result = self.server.dispatch(name, args)
            ok = True
        except Exception as e:
            result, ok = f"{type(e).__name__}: {e}", False
        policy.audit(name, args, policy.Decision(policy.ALLOW, f"bridge {round(time.time() - t0, 2)}s"),
                     source="bridge")
        self._send(200, {"ok": ok, "result": result if isinstance(result, str) else json.dumps(result, default=str)})


def search_tools(tools, q=""):
    """Rank tool specs by query words (name hits count double). Empty query = all, sorted by name."""
    tools = [t for t in tools if t.get("name") not in HIDDEN]
    words = [w for w in (q or "").lower().replace("_", " ").split() if len(w) > 1]
    if not words:
        return sorted(tools, key=lambda t: t["name"])
    scored = []
    for t in tools:
        name = t["name"].lower().replace("_", " ")
        desc = (t.get("description") or "").lower()
        score = sum(2 * (w in name) + (w in desc) for w in words)
        if score:
            scored.append((score, t))
    return [t for _, t in sorted(scored, key=lambda x: (-x[0], x[1]["name"]))]


def start(dispatch, list_tools, pre=None, port=0):
    """Start the bridge in a daemon thread (idempotent). Returns (port, token)."""
    global _server
    if _server is not None:
        return _server.server_address[1], _server.token
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.daemon_threads = True
    srv.token = secrets.token_urlsafe(24)
    srv.dispatch, srv.list_tools, srv.pre = dispatch, list_tools, pre
    threading.Thread(target=srv.serve_forever, daemon=True, name="waggle-bridge").start()
    _server = srv
    info = {"port": srv.server_address[1], "token": srv.token, "pid": os.getpid(), "started": time.time()}
    path = data_path(INFO_FILE)
    path.write_text(json.dumps(info), encoding="utf-8")
    _restrict(path)
    return info["port"], info["token"]


def stop():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


def _restrict(path):
    """Owner-only file permissions (best effort; on Windows the profile folder is already private)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---- client side (used by the MCP server) -------------------------------------------------------

class Client:
    def __init__(self, info=None, timeout=180):
        self.info = info
        self.timeout = timeout

    def _info(self):
        if self.info:
            return self.info
        p = data_path(INFO_FILE)
        if not p.exists():
            raise ConnectionError("Waggle bridge not running (start the Hermes gateway)")
        return json.loads(p.read_text(encoding="utf-8"))

    def _req(self, method, path, body=None):
        info = self._info()
        url = f"http://127.0.0.1:{info['port']}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-Waggle-Token": info["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise ConnectionError(f"Waggle bridge unreachable: {e}") from e

    def health(self):
        try:
            return bool(self._req("GET", "/health").get("ok"))
        except Exception:
            return False

    def tools(self, q=""):
        return self._req("GET", "/tools?q=" + urllib.parse.quote(q or "")).get("tools", [])

    def call(self, name, args=None):
        res = self._req("POST", "/call", {"name": name, "args": args or {}})
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "tool failed")
        return res.get("result", "")
