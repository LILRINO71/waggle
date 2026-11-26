"""The owner's standing permission (2026-11-26): "It has permission to do anything, everything. Stop asking me."
Under full trust no tool call asks; money, the owner's one limit, is refused instead; a "Did you say...?" stays."""
import json

import pytest

import waggle.config as config
import waggle.guard as guard
from waggle import policy, security


@pytest.fixture
def trust(monkeypatch):
    monkeypatch.setattr(guard, "FULL_TRUST_DEFAULT", True)


# Real asks from ~/.waggle/approvals.db that reached the owner's phone.
REAL_ASKS = [
    ("Bash", {"command": "awk 'NR>=399 && NR<=475 && /return|out = /' landplanner/outlook.py | cut -c1-200"}),
    ("Bash", {"command": r"sed -i 's/USGSImageryTopo\/MapServer/USGSImageryOnly\/MapServer/' report/app.js"}),
    ("Bash", {"command": "rm -f /c/Users/hiheo/Claude/lp-full.png /c/Users/hiheo/Claude/lp-map.png"}),
    ("Bash", {"command": "cd /tmp && curl -s -m 20 https://api.ftcscout.org/graphql -H 'content-type: application/json' "
                         "-d '{\"query\":\"{ __schema { queryType { name } } }\"}'"}),
    ("Bash", {"command": "git push --force origin main"}),
    ("Write", {"file_path": r"C:\Users\hiheo\Somewhere\Weekly Review.md", "content": "# Weekly"}),
    ("mcp__claude_ai_Gmail__send_message", {"to": ["someone@example.com"], "subject": "hi", "body": "in order to"}),
    ("mcp__waggle__install_tool", {"name": "some-mcp"}),
    ("mcp__waggle__settings", {"action": "set", "key": "voice", "value": "af_heart"}),
    ("StructuredOutput", {"events": []}),
    ("mcp__waggle__hermes_tool", {"name": "manage_connections", "args": {"action": "status"}}),
]


@pytest.mark.parametrize("tool,args", REAL_ASKS)
def test_real_asks_now_go_ahead(trust, tool, args):
    d = policy.check(tool, args, session="telegram:1", turn=None)
    assert d.action == policy.ALLOW, d.reason
    assert not policy.pending()


def test_the_same_calls_still_ask_with_full_trust_off():
    asked = [t for t, a in REAL_ASKS if policy.decide(t, a).action == policy.ASK]
    assert len(asked) >= 7, asked


@pytest.mark.parametrize("tool,args", [
    ("mcp__stripe__create_payment", {"amount": 5}),
    ("mcp__shop__purchase", {"item": "x"}),
    ("make_payment", {"amount": 1}),
])
def test_money_is_refused_never_asked(trust, tool, args):
    d = policy.check(tool, args, session="telegram:1", turn=None)
    assert d.action == policy.DENY
    assert "never spends money" in d.reason
    assert not policy.pending()


def test_a_message_that_mentions_a_bank_still_sends(trust):
    d = policy.decide("mcp__claude_ai_Gmail__send_message", {"to": ["a@b.c"], "body": "my bank called"})
    assert d.action == policy.ALLOW


def test_hard_refusals_stay(trust):
    assert policy.decide("Bash", {"command": "Set-MpPreference -DisableRealtimeMonitoring $true"}).action == policy.DENY


def test_hearing_question_stays(trust):
    d = policy.full_trust("Bash", {"command": "rm x"}, policy.Decision(policy.ASK, 'Did you say: "x"?', kind="hearing"))
    assert d.action == policy.ASK


def test_off_switch_is_a_protected_key(trust):
    assert "full_trust" in guard.CONFIG_KEYS
    config.HOME.mkdir(parents=True, exist_ok=True)
    (config.HOME / "config.json").write_text(json.dumps({"full_trust": False}), encoding="utf-8")
    assert not guard.full_trust_enabled()
    assert policy.decide("Bash", {"command": "rm -f /c/Users/hiheo/Claude/x.png"}).action == policy.ASK


def test_hermes_tools_follow_it(trust):
    assert security.check_tool_call("send_message", {"to": "x", "text": "hi"}, turn=None) is None
    assert security.check_tool_call("terminal", {"command": "rm -rf build_old"}, turn=None) is None
    out = security.check_tool_call("make_payment", {"amount": 3}, turn=None)
    assert out and out["action"] in ("approve", "block")
