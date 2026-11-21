"""waggle/usage_view.py: the ccusage view is read-only, offline, and never breaks the quota line."""
from waggle import usage_view as uv
from waggle.config import Settings

Q = {"window_pct": 54.0, "source": "real", "resets_at": "2026-11-21T22:00", "waggle_units": 1.5, "seven_day_pct": 30.0}
BLOCKS = {"blocks": [
    {"isGap": True, "startTime": "2026-11-20T11:00:00Z", "endTime": "2026-11-20T14:00:00Z"},
    {"isGap": False, "startTime": "2026-11-20T14:00:00Z", "endTime": "2026-11-20T19:00:00Z", "isActive": True,
     "totalTokens": 254908953, "tokenCounts": {"outputTokens": 1394047}, "entries": 1021, "costUSD": 124.5,
     "models": ["claude-opus-5-5", "claude-sonnet-5-5"]}]}
SESSIONS = {"session": [
    {"period": "ff54b6ac-7199", "totalTokens": 5000, "outputTokens": 100, "totalCost": 0.5,
     "metadata": {"lastActivity": "2026-11-21T20:19:59Z"}, "modelsUsed": ["claude-haiku-5-5"]},
    {"period": "aaaaaaaa-1111", "totalTokens": 9000000, "outputTokens": 2000, "totalCost": 50.8,
     "metadata": {"lastActivity": "2026-11-21T20:00:00Z"}, "modelsUsed": ["claude-sonnet-5-5"]}]}


def fake(kind, days=2):
    return BLOCKS if kind == "blocks" else SESSIONS


def test_view_shows_window_blocks_and_busiest_session_first():
    text = uv.view(Settings(), fetcher=fake, quota_status=Q)
    assert text.startswith("Window: 54.0% used (real)")
    assert "(active)" in text and "254.9M tokens (1.4M written)" in text and "opus,sonnet" in text
    assert "gap" not in text.lower()
    assert text.index("aaaaaaaa") < text.index("ff54b6ac") and "API prices" in text


def test_missing_ccusage_keeps_the_window_line():
    def boom(kind, days=2):
        raise RuntimeError("ccusage is not installed")
    text = uv.view(Settings(), fetcher=boom, quota_status=Q)
    assert text.startswith("Window:") and "ccusage view unavailable: ccusage is not installed" in text


def test_off_switch_never_calls_ccusage():
    s = Settings()
    s.ccusage_enabled = False
    text = uv.view(s, fetcher=lambda *a, **k: 1 / 0, quota_status=Q)
    assert "off" in text and "By 5-hour block" not in text


def test_fetch_runs_offline_and_reads_only(monkeypatch, tmp_path):
    js = tmp_path / "cli.js"
    js.write_text("")
    monkeypatch.setattr(uv, "entry", lambda: js)
    monkeypatch.setattr(uv.shutil, "which", lambda n: "node")
    seen = {}

    class P:
        returncode, stdout, stderr = 0, b'{"blocks": []}', b""

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return P()
    monkeypatch.setattr(uv.subprocess, "run", run)
    assert uv.fetch("blocks") == {"blocks": []}
    assert "--offline" in seen["cmd"] and "--json" in seen["cmd"] and seen["cmd"][2] == "blocks"
