"""Claude through the Claude Code CLI on the user's Pro login (no API key).
Adapted from kalshi-claude-bot's claude_cli.py."""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import quota

# Exact IDs: an inherited environment can remap short aliases (kalshi bot caught "sonnet" running as Haiku).
MODEL_IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# Without these, a headless call loads MCP servers, settings and tool prompts: ~72k tokens instead of ~1.3k.
LEAN_FLAGS = ["--tools", "", "--no-session-persistence", "--strict-mcp-config",
              "--setting-sources", "", "--disable-slash-commands"]


class ClaudeError(RuntimeError):
    def __init__(self, message, kind="error", reset_in_hours=None):
        super().__init__(message)
        self.kind = kind  # error | limit | auth | missing | model_mismatch | timeout
        self.reset_in_hours = reset_in_hours


def clean_env():
    keep = {"CLAUDE_CONFIG_DIR", "CLAUDE_BIN"}
    return {k: v for k, v in os.environ.items()
            if k in keep or not (k.startswith("CLAUDE_") or k.startswith("ANTHROPIC_"))}


def find_claude():
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if path:
        return path
    from .config import Settings
    saved = Settings.load().claude_bin
    if saved and os.path.exists(saved):
        return saved
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    patterns = [
        # Claude desktop app (2026 layout): Claude-Data\claude_<id>\claude-code\<version>\<hash>\claude.exe
        os.path.join(local, "Claude-Data", "claude_*", "claude-code", "*", "*", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*", "LocalCache",
                     "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.expanduser("~/.local/bin/claude"),
    ]
    found = [p for pat in patterns for p in glob.glob(pat)]
    if not found:
        return None
    def version_key(p):
        # The version folder is the parent, or the grandparent in the hash-folder layout.
        parts = Path(p).parts
        ver = next((x for x in reversed(parts[:-1]) if re.fullmatch(r"\d+(\.\d+)+", x)), "0")
        return [int(x) for x in ver.split(".")]
    return max(found, key=version_key)


def parse_reset_hours(msg):
    """'... resets 3pm' / 'resets in 2h' -> rough hours until reset (None if unknown)."""
    m = re.search(r"(\d+)\s*h", msg)
    if m:
        return float(m.group(1))
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", msg, re.I)
    if m:
        import datetime as dt
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        now = dt.datetime.now()
        target = now.replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        return (target - now).total_seconds() / 3600
    return None


# claude.ai connectors (the user's own Gmail/Drive/Docs, signed in on claude.ai). Read-only tools plus drafts:
# Waggle never sends, deletes or shares on its own.
CONNECTOR_TOOLS = {
    "Gmail": ["search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft",
              "create_draft"],
    "Google_Drive": ["search_files", "list_recent_files", "read_file_content", "get_file_metadata"],
    "Google_Docs": ["read_doc"],
}
_SERVERS_FILE = "mcp_servers.json"


def mcp_servers(claude=None, refresh=False):
    """Tool-name prefixes of every MCP server the CLI loads (cached a day: `claude mcp list` takes ~5 s)."""
    from .config import data_path
    path = data_path(_SERVERS_FILE)
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    claude = claude or find_claude()
    try:
        out = subprocess.run([claude, "mcp", "list"], capture_output=True, text=True, timeout=90, env=clean_env(),
                             encoding="utf-8", errors="replace").stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        m = re.match(r"^(.+?):\s+\S", line)
        if not m or line.startswith("Checking"):
            continue
        name = m.group(1).strip()
        if name.startswith("claude.ai "):
            name = "claude_ai " + name[len("claude.ai "):]
        names.append("mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", name))
    path.write_text(json.dumps(names), encoding="utf-8")
    return names


def connector_flags(connectors, servers):
    """Allow the read tools of `connectors` (whole server for ones without a known list, e.g. Calendar, which the
    PreToolUse policy hook then filters: no sending, deleting, sharing or inviting); hide every other MCP server
    (the user's full set adds ~150k tokens)."""
    wanted = {f"mcp__claude_ai_{c}" for c in connectors}
    allowed = [f"mcp__claude_ai_{c}__{t}" for c in connectors for t in CONNECTOR_TOOLS.get(c, [])]
    allowed += [f"mcp__claude_ai_{c}" for c in connectors if c not in CONNECTOR_TOOLS]
    hidden = [s for s in servers if s not in wanted]
    from .agent import settings_json  # the same policy hook the tool agents use
    flags = ["--tools", "", "--no-session-persistence", "--disable-slash-commands",
             "--settings", json.dumps(settings_json()), "--allowedTools", *allowed]
    if hidden:
        flags += ["--disallowedTools", *hidden]
    return flags


OPTIONAL_CONNECTORS = ("Google_Calendar", "Gcal", "Google_Tasks", "Google_Sheets")


def available_connectors(base=("Gmail", "Google_Drive", "Google_Docs")):
    """The base connectors plus Calendar / Tasks / Sheets once the user has connected them on claude.ai."""
    servers = set(mcp_servers())
    return list(base) + [c for c in OPTIONAL_CONNECTORS if f"mcp__claude_ai_{c}" in servers]


def ask(prompt, model="sonnet", system=None, schema=None, effort="medium", purpose="ask", timeout=300,
        connectors=None):
    """One lean headless call. Returns {'text', 'json', 'usage', 'model_id'}; raises ClaudeError.
    connectors=["Gmail", ...] lets it read the user's claude.ai connectors (slower: ~6 s instead of ~2 s)."""
    claude = find_claude()
    if not claude:
        raise ClaudeError("Claude Code CLI not found. Set CLAUDE_BIN.", "missing")
    wanted = MODEL_IDS.get(model, model)
    from . import budget
    try:  # background work that skipped its own budget check still can't overspend
        budget.guard(wanted, purpose)
    except budget.Deferred as e:
        raise ClaudeError(f"background budget: {e.reason}", "budget")
    flags = connector_flags(connectors, mcp_servers(claude)) if connectors else LEAN_FLAGS
    cmd = [claude, "-p", "--model", wanted, "--effort", effort, "--output-format", "json", *flags]
    if system:
        cmd += ["--system-prompt", system]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    usage, main, ok, kind = {}, None, False, None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=clean_env(),
                                  encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp)
        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if "Not logged in" in out or "Not logged in" in err:
            kind = "auth"
            raise ClaudeError("Claude CLI is not logged in. Run `claude` and /login.", "auth")
        try:
            env = json.loads(out)
        except json.JSONDecodeError:
            low = (out + err).lower()
            if "limit" in low:
                kind = "limit"
                raise ClaudeError((out or err)[:300], "limit", parse_reset_hours(out + err))
            raise ClaudeError(f"Unreadable CLI output: {(out or err)[:300]}")
        usage = env.get("usage") or {}
        per_model = env.get("modelUsage") or {}
        main = max(per_model, key=lambda k: per_model[k].get("outputTokens", 0)) if per_model else None
        if env.get("is_error"):
            msg = str(env.get("result") or env.get("terminal_reason") or "unknown error")
            low = msg.lower()
            kind = ("auth" if ("authenticate" in low or "oauth" in low or "log in" in low or "login" in low)
                    else "limit" if "limit" in low else "error")
            if kind == "auth":
                msg = "Claude CLI sign-in expired. Run `waggle login` and type /login."
            raise ClaudeError(msg[:300], kind, parse_reset_hours(msg))
        if main and main != wanted:
            kind = "model_mismatch"
            raise ClaudeError(f"Asked for {wanted} but {main} answered", "model_mismatch")
        text = env.get("result") or ""
        data = env.get("structured_output")
        if schema and not isinstance(data, dict):
            a, b = text.find("{"), text.rfind("}")
            data = json.loads(text[a:b + 1]) if a >= 0 else None
        ok = True
        return {"text": text, "json": data, "usage": usage, "model_id": main or wanted}
    except subprocess.TimeoutExpired:
        kind = "timeout"
        raise ClaudeError(f"Claude CLI timed out after {timeout}s", "timeout")
    except ClaudeError as e:
        if e.kind == "limit":
            quota.note_limit_hit(reset_in_hours=e.reset_in_hours)
        raise
    finally:
        quota.record(wanted, usage, purpose, headless=True, ok=ok, error_kind=kind)


def start_session(prompt, model, cwd, log_path, system_append=None, permission_mode="bypassPermissions"):
    """Launch a full Claude Code run (tools on, session saved) as a detached background process."""
    claude = find_claude()
    if not claude:
        raise ClaudeError("Claude Code CLI not found. Set CLAUDE_BIN.", "missing")
    cmd = [claude, "-p", "--model", MODEL_IDS.get(model, model), "--output-format", "stream-json",
           "--verbose", "--permission-mode", permission_mode]
    if system_append:
        cmd += ["--append-system-prompt", system_append]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True,
                            env=clean_env(), cwd=cwd, encoding="utf-8", errors="replace", creationflags=flags)
    proc.stdin.write(prompt)
    proc.stdin.close()
    return proc
