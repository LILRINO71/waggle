from waggle import claude_cli, router
from waggle.config import Settings


def test_connector_flags_allow_read_tools_and_hide_other_servers():
    servers = ["mcp__claude_ai_Gmail", "mcp__claude_ai_Canva", "mcp__rea"]
    flags = claude_cli.connector_flags(["Gmail"], servers)
    allowed = flags[flags.index("--allowedTools") + 1:flags.index("--disallowedTools")]
    hidden = flags[flags.index("--disallowedTools") + 1:]
    assert "mcp__claude_ai_Gmail__search_threads" in allowed
    assert not any("send" in t or "trash" in t for t in allowed)  # never sends or deletes on its own
    assert hidden == ["mcp__claude_ai_Canva", "mcp__rea"]
    assert "--strict-mcp-config" not in flags  # that flag would also hide the claude.ai connectors


def test_email_request_goes_straight_to_connectors(tmp_path, monkeypatch):
    monkeypatch.setattr(router.quota, "can_escalate", lambda m, s: (True, "ok"))
    calls = []

    def asker(prompt, **kw):
        calls.append(kw)
        return {"text": "Seven unread. One from your math teacher about Friday's test.", "json": None}

    d = router.decide("can you check my emails?", settings=Settings(vault=str(tmp_path)), asker=asker)
    assert d["route"] == "sonnet" and d["by"] == "connected"
    assert "math teacher" in d["reply"]
    assert len(calls) == 1 and calls[0]["connectors"] == router.CONNECTORS


def test_router_can_pick_connected_for_other_wording(tmp_path, monkeypatch):
    monkeypatch.setattr(router.quota, "can_escalate", lambda m, s: (True, "ok"))

    def asker(prompt, **kw):
        if kw.get("connectors"):
            return {"text": "Your robot notes doc says the shooter ratio is 1:1.", "json": None}
        return {"text": "", "json": {"route": "connected", "reason": "needs docs", "reply": "",
                                     "build_prompt": "", "title": ""}}

    d = router.decide("what did I write about the shooter in my notes doc?", settings=Settings(vault=str(tmp_path)),
                      asker=asker)
    assert d["route"] == "sonnet" and "1:1" in d["reply"]


def test_connected_failure_falls_back_to_normal_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(router.quota, "can_escalate", lambda m, s: (True, "ok"))

    def asker(prompt, **kw):
        if kw.get("connectors"):
            raise claude_cli.ClaudeError("down")
        return {"text": "", "json": {"route": "answer", "reason": "", "reply": "Can't reach Gmail right now.",
                                     "build_prompt": "", "title": ""}}

    d = router.decide("any new emails?", settings=Settings(vault=str(tmp_path)), asker=asker)
    assert d["route"] == "sonnet" and d["by"] != "connected"
