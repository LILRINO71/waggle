"""Windows-MCP (waggle/winmcp.py, tools/winmcp_server.py): every call goes through the same rules as computer_use
(blocked apps, no secrets typed, no payments, the Claude window off-limits), and the server is offered to the chat brain.
No real windows are read: a fake resolver says which window is where."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from waggle import agent_hook, chat_session, policy, security, winmcp
from waggle.config import Settings, data_path

REPO = Path(__file__).resolve().parent.parent
T = "mcp__windows-mcp__"


class Fake:
    """foreground = the window in front; at = {(x, y): window} for points; anything else lands on the foreground."""

    def __init__(self, fg=("Untitled - Notepad", "notepad.exe"), at=None):
        self.fg, self.map = {"title": fg[0], "app": fg[1]}, {k: {"title": v[0], "app": v[1]} for k, v in (at or {}).items()}

    def foreground(self):
        return self.fg

    def at(self, x, y):
        return self.map.get((x, y), self.fg)


def check(tool, args, fg=("Untitled - Notepad", "notepad.exe"), at=None, state=None):
    return security.winmcp_check(T + tool, args, resolver=Fake(fg, at), state=state or {})


@pytest.fixture
def fake_windows(monkeypatch):
    """The policy path builds its own resolver: hand it a fake whose windows the test sets."""
    box = {"fake": Fake()}
    monkeypatch.setattr(security, "WinResolver", lambda: box["fake"])
    return box


# ---- looking is free, acting on an ordinary window runs ---------------------------------------------------------------

def test_looking_never_asks_even_at_a_bank_or_the_claude_window():
    for tool in ("Snapshot", "Screenshot", "WaitFor", "Wait"):
        assert check(tool, {}, fg=("Chase Bank - Google Chrome", "chrome.exe")) is None
        assert check(tool, {}, fg=("Claude", "claude.exe")) is None


def test_acting_on_an_ordinary_window_runs():
    assert check("Click", {"loc": [10, 20]}) is None
    assert check("Type", {"loc": [10, 20], "text": "Waggle test"}) is None
    assert check("Scroll", {"loc": [10, 20], "direction": "down"}) is None
    assert check("Shortcut", {"shortcut": "ctrl+n"}) is None
    assert check("App", {"mode": "switch", "name": "Notepad"}) is None
    assert check("Clipboard", {"mode": "get"}) is None


# ---- 1. blocked apps (the computer_use SENSITIVE_APPS list) ---------------------------------------------------------

@pytest.mark.parametrize("title,app", [("PayPal - Send money - Google Chrome", "chrome.exe"), ("Outlook", "olk.exe"),
                                       ("Chase Bank", "chrome.exe"), ("Bitwarden", "Bitwarden.exe"),
                                       ("Settings", "SystemSettings.exe"), ("Discord", "Discord.exe")])
def test_a_blocked_app_in_front_asks_for_every_acting_tool(title, app):
    for tool, args in (("Click", {"loc": [1, 1]}), ("Type", {"loc": [1, 1], "text": "hi"}), ("Scroll", {"loc": [1, 1]}),
                       ("Shortcut", {"shortcut": "ctrl+a"})):
        verdict = check(tool, args, fg=(title, app))
        assert verdict and verdict[0] == "ask", (tool, title)


def test_a_click_that_lands_on_a_blocked_window_asks_even_when_another_window_is_in_front():
    at = {(500, 400): ("Outlook", "olk.exe")}
    assert check("Click", {"loc": [500, 400]}, at=at)[0] == "ask"
    assert check("Click", {"loc": "[500, 400]"}, at=at)[0] == "ask"  # the brain sometimes sends the list as a string
    assert check("Click", {"loc": [5, 4]}, at=at) is None


def test_a_label_is_looked_up_in_the_last_snapshot():
    state = {"labels": {"3": {"window": "Outlook - Inbox", "name": "Send", "type": "Button", "x": 9, "y": 9},
                        "4": {"window": "Untitled - Notepad", "name": "Text editor", "type": "Document", "x": 8, "y": 8}}}
    assert check("Click", {"label": 3}, state=state)[0] == "ask"
    assert check("Click", {"label": 4}, state=state) is None
    assert check("Click", {"label": 9}, state=state)[0] == "ask"  # not in the snapshot: unknown window, so it asks
    assert check("Click", {"label": 4}, state=state, at={(8, 8): ("PayPal", "chrome.exe")})[0] == "ask"  # moved since


def test_launching_or_switching_to_a_blocked_app_asks():
    assert check("App", {"mode": "launch", "name": "Outlook"})[0] == "ask"
    assert check("App", {"mode": "switch", "name": "PayPal"})[0] == "ask"
    assert check("App", {"mode": "launch", "name": "Notepad"}) is None


# ---- 2. no secrets typed --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["4111 1111 1111 1111", "my password is hunter2", "pin 4821", "sk-ant-api03-x token",
                                  "the api key is abc"])
def test_typing_a_secret_asks_in_any_window(text):
    assert check("Type", {"loc": [1, 1], "text": text})[0] == "ask"
    assert check("Clipboard", {"mode": "set", "text": text})[0] == "ask"


def test_typing_into_a_password_or_card_field_asks_whatever_the_text():
    state = {"labels": {"2": {"window": "Untitled - Notepad", "name": "Card number", "type": "Edit", "x": 1, "y": 1},
                        "5": {"window": "Untitled - Notepad", "name": "Password", "type": "Edit", "x": 2, "y": 2}}}
    assert check("Type", {"label": 2, "text": "hello"}, state=state)[0] == "ask"
    assert check("Type", {"label": 5, "text": "hello"}, state=state)[0] == "ask"
    assert check("Click", {"label": 5}, state=state) is None  # clicking the field is not typing a secret


# ---- 3. no payments / log-in pages ----------------------------------------------------------------------------------

@pytest.mark.parametrize("title", ["Checkout - Amazon.com - Google Chrome", "Payment details - Google Chrome",
                                   "Sign in to your account - Google Chrome", "Billing - Google Chrome",
                                   "Venmo - Google Chrome", "Coinbase - Google Chrome"])
def test_a_payment_or_login_page_asks(title):
    assert check("Click", {"loc": [1, 1]}, fg=(title, "chrome.exe"))[0] == "ask"


# ---- 4. the Claude window is off-limits (a refusal, not a question) --------------------------------------------------

def test_the_claude_window_is_denied_whether_in_front_or_at_the_point():
    for tool, args in (("Click", {"loc": [1, 1]}), ("Type", {"loc": [1, 1], "text": "x"}), ("Scroll", {"loc": [1, 1]}),
                       ("Shortcut", {"shortcut": "ctrl+a"})):
        assert check(tool, args, fg=("Claude", "claude.exe"))[0] == "deny", tool
    at = {(7, 7): ("Claude", "claude.exe")}
    assert check("Click", {"loc": [7, 7]}, at=at)[0] == "deny"  # Notepad in front, the point is in Claude
    assert check("Click", {"label": 1}, state={"labels": {"1": {"window": "Claude", "name": "Close"}}})[0] == "deny"
    assert check("App", {"mode": "switch", "name": "Claude"})[0] == "deny"
    assert check("Click", {"loc": [1, 1]}, fg=("Claude - Google Chrome", "chrome.exe"))[0] == "deny"


# ---- 5. no shell, no registry, no files, no system shortcuts --------------------------------------------------------

@pytest.mark.parametrize("tool", ["PowerShell", "Registry", "FileSystem", "Process", "Scrape", "Notification", "Move",
                                  "MultiEdit", "MultiSelect", "Anything"])
def test_tools_waggle_does_not_offer_are_denied(tool):
    assert check(tool, {})[0] == "deny"


def test_launching_an_arbitrary_executable_is_denied():
    assert check("App", {"mode": "launch_executable", "executable": "C:/x/evil.exe"})[0] == "deny"


@pytest.mark.parametrize("keys", ["win+r", "win+x", "win+i", "ctrl+shift+esc", "ctrl+alt+delete", "win+l", "WIN + R"])
def test_shortcuts_that_open_a_shell_or_system_tool_ask(keys):
    assert check("Shortcut", {"shortcut": keys})[0] == "ask"


def test_a_window_that_cannot_be_identified_asks():
    class Broken(Fake):
        def foreground(self):
            raise OSError("no desktop")
    assert security.winmcp_check(T + "Click", {"loc": [1, 1]}, resolver=Broken(), state={})[0] == "ask"


def test_other_tools_and_other_servers_are_not_this_checks_business():
    assert security.winmcp_check("Click", {"loc": [1, 1]}) is None
    assert security.winmcp_check("mcp__wolfram__WolframAlpha", {"query": "x"}) is None
    assert security.winmcp_check("mcp__windows_mcp__Click", {"loc": [1, 1]}, resolver=Fake(("Claude", "claude.exe")),
                                 state={})[0] == "deny"  # the underscore spelling is the same server


# ---- the same rules through the policy, the hook and the Hermes guard ----------------------------------------------

def test_policy_asks_denies_and_allows_through_the_new_path(fake_windows):
    fake_windows["fake"] = Fake(("Untitled - Notepad", "notepad.exe"))
    assert policy.decide(T + "Click", {"loc": [1, 1]}).allowed
    assert policy.decide(T + "Snapshot", {}).allowed
    d = policy.decide(T + "Type", {"loc": [1, 1], "text": "my password is x"})
    assert d.action == policy.ASK and d.kind == "screen"
    assert policy.decide(T + "PowerShell", {"command": "ls"}).action == policy.DENY
    fake_windows["fake"] = Fake(("PayPal", "chrome.exe"))
    assert policy.decide(T + "Click", {"loc": [1, 1]}).action == policy.ASK
    fake_windows["fake"] = Fake(("Claude", "claude.exe"))
    assert policy.decide(T + "Click", {"loc": [1, 1]}).action == policy.DENY


def test_the_owners_words_never_waive_a_screen_ask(fake_windows):
    fake_windows["fake"] = Fake(("Outlook", "olk.exe"))
    turn = {"owner": True, "confident": True, "text": "click send in outlook"}
    assert policy.decide(T + "Click", {"loc": [1, 1]}, turn=turn).action == policy.ASK
    fake_windows["fake"] = Fake(("Claude", "claude.exe"))
    assert policy.decide(T + "Click", {"loc": [1, 1]}, turn=turn).action == policy.DENY


def test_the_hook_turns_an_ask_into_needs_approval_and_a_deny_into_a_refusal(fake_windows):
    fake_windows["fake"] = Fake(("PayPal", "chrome.exe"))
    out = agent_hook.handle({"tool_name": T + "Click", "tool_input": {"loc": [3, 4]}})["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "NEEDS_APPROVAL" in out["permissionDecisionReason"]
    fake_windows["fake"] = Fake(("Claude", "claude.exe"))
    out = agent_hook.handle({"tool_name": T + "Click", "tool_input": {"loc": [3, 4]}})["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "off-limits" in out["permissionDecisionReason"]
    fake_windows["fake"] = Fake()
    out = agent_hook.handle({"tool_name": T + "Click", "tool_input": {"loc": [3, 4]}})["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"


def test_the_hermes_guard_checks_it_too(fake_windows):
    fake_windows["fake"] = Fake()
    assert security.check_tool_call(T + "Type", {"loc": [1, 1], "text": "hello"}) is None
    assert security.check_tool_call(T + "Type", {"loc": [1, 1], "text": "card 4111 1111 1111 1111"})["action"] == "approve"
    assert security.check_tool_call(T + "Registry", {})["action"] == "block"
    fake_windows["fake"] = Fake(("Claude", "claude.exe"))
    assert security.check_tool_call(T + "Click", {"loc": [1, 1]})["action"] == "block"


# ---- the real Windows lookup (windows only) -------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows window calls")
def test_the_real_resolver_names_a_window_without_raising():
    r = security.WinResolver()
    w = r.foreground()
    assert set(w) == {"title", "app"}
    assert set(r.at(0, 0)) == {"title", "app"}


# ---- the offered server ---------------------------------------------------------------------------------------------

def test_windows_mcp_is_offered_only_when_installed_and_on(settings, monkeypatch):
    def servers():
        return json.loads(Path(chat_session.mcp_config("telegram:1", settings)).read_text(encoding="utf-8"))["mcpServers"]
    assert Settings().windows_mcp is True and settings.windows_mcp is False  # tests start with it off
    assert winmcp.SERVER_NAME not in servers()
    monkeypatch.setattr(winmcp, "installed", lambda: True)
    assert winmcp.SERVER_NAME not in servers()  # still off in settings
    settings.windows_mcp = True
    entry = servers()[winmcp.SERVER_NAME]
    assert entry["type"] == "stdio" and entry["env"]["ANONYMIZED_TELEMETRY"] == "false"
    assert entry["env"]["WINDOWS_MCP_DISABLE_FLASH"] == "1" and entry["env"]["WAGGLE_HOME"] == str(data_path("."))
    assert entry["args"] == [str(REPO / "tools" / "winmcp_server.py")]
    assert "windows-mcp" in entry["command"] or "windows-mcp" in entry["command"].replace("\\", "/")
    monkeypatch.setattr(winmcp, "installed", lambda: False)
    assert winmcp.SERVER_NAME not in servers()  # not installed: never a dangling server


def test_the_server_name_makes_the_tool_names_the_policy_matches():
    assert security.WINMCP_TOOL.match(f"mcp__{winmcp.SERVER_NAME}__Click")


def test_the_brain_is_told_to_prefer_windows_mcp_and_confirm_each_step():
    text = chat_session.system_prompt(Settings())
    assert "mcp__windows-mcp__" in text and "Start-Process" in text and "Snapshot again" in text
    assert "computer_use" in text and "off-limits" in text
    soul = (REPO / "persona" / "SOUL.md").read_text(encoding="utf-8")
    screen = next(line for line in soul.splitlines() if line.startswith("Screen:"))
    assert "Windows-MCP" in screen and "Start-Process" in screen and "computer_use" in screen


# ---- the launcher ---------------------------------------------------------------------------------------------------

def load_launcher():
    spec = importlib.util.spec_from_file_location("winmcp_server", REPO / "tools" / "winmcp_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_desktop():
    def node(window, name, x, y, kind="Button"):
        return types.SimpleNamespace(window_name=window, name=name, control_type=kind,
                                     center=types.SimpleNamespace(x=x, y=y))
    tree = types.SimpleNamespace(interactive_nodes=[node("Notepad", "Text editor", 5, 6, "Document"),
                                                    node("Notepad", "Close Tab", 7, 8)],
                                 scrollable_nodes=[node("Notepad", "Text editor", 9, 10, "Document")])
    return types.SimpleNamespace(desktop_state=types.SimpleNamespace(
        tree_state=tree, active_window=types.SimpleNamespace(name="Notepad")))


def test_the_launcher_offers_only_the_allowed_tools_and_no_shell():
    m = load_launcher()
    offered = set(m.TOOLS.split(","))
    assert offered == {"App", "Snapshot", "WaitFor", "Wait", "Click", "Type", "Scroll", "Shortcut", "Clipboard"}
    assert not offered & {"PowerShell", "Registry", "FileSystem", "Process", "Scrape", "Move", "MultiEdit"}
    assert {t.lower() for t in offered} - {"app"} <= security.WINMCP_ACT | security.WINMCP_LOOK  # each one is ruled on


def test_the_label_map_numbers_interactive_then_scrollable_like_windows_mcp(settings, monkeypatch):
    m = load_launcher()
    monkeypatch.setenv("WAGGLE_HOME", str(data_path(".")))
    m.dump(fake_desktop())
    state = json.loads((data_path(".") / security.WINMCP_STATE).read_text(encoding="utf-8"))
    assert state["active"] == "Notepad"
    assert [state["labels"][k]["name"] for k in "012"] == ["Text editor", "Close Tab", "Text editor"]
    assert state["labels"]["2"] == {"window": "Notepad", "name": "Text editor", "type": "Document", "x": 9, "y": 10}
    # the policy reads exactly this file
    assert security._winmcp_state()["labels"]["1"]["name"] == "Close Tab"


def test_a_failed_map_write_removes_the_old_map_so_labels_ask(settings, monkeypatch):
    m = load_launcher()
    monkeypatch.setenv("WAGGLE_HOME", str(data_path(".")))
    m.dump(fake_desktop())
    path = data_path(".") / security.WINMCP_STATE
    assert path.exists()
    m.dump(types.SimpleNamespace(desktop_state=None))  # a state it cannot read
    assert not path.exists()
    assert check("Click", {"label": 1}, state=security._winmcp_state())[0] == "ask"
