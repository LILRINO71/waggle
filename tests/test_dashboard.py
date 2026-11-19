"""Command center: snapshot shape and the local HTTP server."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from waggle import dashboard, traces


def test_snapshot_has_every_panel(monkeypatch):
    monkeypatch.setattr(dashboard, "health", lambda: {"gateway": False, "ollama": False})
    monkeypatch.setattr(dashboard, "_models_summary", lambda: {"hardware": {}, "local": [], "cloud": []})
    traces.record("hello", route="sonnet", model="haiku", reply="hi", latency_s=2.0)
    snap = dashboard.snapshot()
    for key in ("health", "quota", "today", "activity", "jobs", "agents", "routines", "approvals", "audit", "skills",
                "learning", "memory", "models", "errors", "cron", "objectives", "ops", "budget"):
        assert key in snap, key
    assert snap["activity"][0]["request"] == "hello"
    json.dumps(snap, default=str)


def test_server_serves_page_and_state(monkeypatch):
    monkeypatch.setattr(dashboard, "snapshot", lambda: {"ok": True})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Waggle Command Center" in page
        assert json.loads(urllib.request.urlopen(base + "/api/state").read()) == {"ok": True}
    finally:
        srv.shutdown()
