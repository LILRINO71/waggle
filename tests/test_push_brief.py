from waggle import brief, notify, projects, vault
from waggle.config import Settings


def test_ntfy_topic_is_private_and_stable():
    s = Settings.load()
    t1 = notify.ntfy_topic(s)
    assert t1.startswith("waggle-") and len(t1) >= 30
    assert notify.ntfy_topic(Settings.load()) == t1


def test_to_ntfy_body():
    seen = []
    ok = notify.to_ntfy("Robot battery low", title="t", priority=5, post=lambda url, b: seen.append((url, b)) or True)
    url, body = seen[0]
    assert ok and url == "https://ntfy.sh" and body["priority"] == 5 and body["message"] == "Robot battery low"


def test_urgent_send_pushes_and_normal_does_not(monkeypatch):
    pushed = []
    monkeypatch.setattr(notify, "to_ntfy", lambda text, **k: pushed.append((text, k["priority"])) or True)
    notify.send("meet starts in 1 hour", urgent=False)
    notify.send("intake motor overheating", urgent=True)
    assert pushed == [("intake motor overheating", 5)]


def test_morning_brief_facts_and_fallback(monkeypatch):
    s = Settings.load()
    vault.init(s)
    projects.create("Robot", s, next_actions=["Tune the shooter"])
    projects.create("Paused Thing", s, status="paused", next_actions=["ignore me"])
    monkeypatch.setattr(brief.live, "weather", lambda *a, **k: "Menifee today: sunny, 72F")
    f = brief.facts(s)
    assert any("sunny" in x for x in f) and "Next on Robot: Tune the shooter" in f
    assert not any("ignore me" in x for x in f)
    out = brief.compose(s, chat=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama down")))
    assert out.startswith("🐝 Good morning.") and "Tune the shooter" in out
    assert brief.compose(s, chat=lambda m, settings=None: "Morning. Sunny, 72.") == "🐝 Morning. Sunny, 72."


def test_brief_includes_calendar_when_connected(settings, monkeypatch):
    from waggle import brief, claude_cli
    monkeypatch.setattr(claude_cli, "mcp_servers", lambda *a, **k: ["mcp__claude_ai_Google_Calendar"])
    monkeypatch.setattr(brief.live, "weather", lambda *a, **k: "NO_LOCATION")
    seen = {}

    def asker(prompt, **kw):
        seen.update(kw)
        return {"text": "15:30 Robotics practice; 18:00 Math study group"}
    assert brief.calendar_today(settings, asker=asker).startswith("15:30 Robotics")
    assert seen["connectors"] == ["Google_Calendar"]
    assert brief.calendar_today(settings, asker=lambda *a, **k: {"text": "NONE"}) == ""
    monkeypatch.setattr(claude_cli, "mcp_servers", lambda *a, **k: [])
    assert brief.calendar_today(settings, asker=lambda *a, **k: 1 / 0) == ""
