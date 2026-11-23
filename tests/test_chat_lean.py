"""Step 1: the lean chat launch (own empty folder, only Waggle's MCP servers, no user settings, MCP tools deferred
behind ToolSearch except the core set). Measured live: fixed context 115,686 -> 16,444 tokens."""
import json
from pathlib import Path

from engine_fakes import FakeClaude

from waggle import chat_session, config, waggle_mcp
from waggle.chat_session import ChatSession


def test_lean_command_line_has_only_waggle_servers_and_no_user_settings(settings):
    cmd, _ = chat_session.build_cmd("telegram:1", settings, "claude", servers=["mcp__rea", "mcp__claude_ai_Gmail"])
    assert "--strict-mcp-config" in cmd
    i = cmd.index("--setting-sources")
    assert cmd[i + 1] == ""  # no user, project or local settings files
    assert "--disallowedTools" not in cmd  # nothing to hide: only --mcp-config servers load
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert "ToolSearch" in tools  # without it nothing can be deferred
    assert json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"]  # the policy hook still applies


def test_old_launch_is_one_switch_away(settings):
    settings.chat_lean = False
    cmd, _ = chat_session.build_cmd("telegram:1", settings, "claude", servers=["mcp__rea", "mcp__waggle"])
    assert "--strict-mcp-config" not in cmd and "--setting-sources" not in cmd
    assert cmd[cmd.index("--disallowedTools") + 1:] == ["mcp__rea"]
    assert "ToolSearch" not in cmd[cmd.index("--tools") + 1].split(",")
    assert chat_session.chat_cwd(settings) == settings.agent_workdir or str(Path.home() / "Claude")


def test_lean_process_runs_in_its_own_empty_folder_with_tool_search_on(settings):
    fake = FakeClaude()
    ChatSession("telegram:1", settings=settings, popen=fake, claude="claude").turn("hi")
    kw = fake.procs[0].kwargs
    assert Path(kw["cwd"]) == config.HOME / "chat_home" and Path(kw["cwd"]).is_dir()
    assert not any(Path(kw["cwd"]).iterdir())
    env = kw["env"]
    for k, v in chat_session.LEAN_ENV.items():
        assert env[k] == v
    assert env["ENABLE_TOOL_SEARCH"] == "true" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_core_tools_are_marked_always_load_and_the_rest_are_not():
    entries = {t["name"]: t for t in waggle_mcp.handle({"id": 1, "method": "tools/list"})["result"]["tools"]}
    assert waggle_mcp.ALWAYS_LOAD <= set(entries)  # every core name is a real tool
    for name, t in entries.items():
        marked = (t.get("_meta") or {}).get("anthropic/alwaysLoad") is True
        assert marked == (name in waggle_mcp.ALWAYS_LOAD), name
    assert len(waggle_mcp.ALWAYS_LOAD) <= 15  # the core stays small: each loaded tool costs every turn


def test_the_brain_is_told_to_search_for_tools_and_not_to_use_connectors(settings):
    text = chat_session.system_prompt(settings)
    assert "ToolSearch" in text and "Never say a tool is missing before searching" in text
    assert "connector" not in text.lower()
