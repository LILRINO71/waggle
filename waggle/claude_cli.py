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

from . import cooldowns, quota

# Exact IDs: an inherited environment can remap short aliases (kalshi bot caught "sonnet" running as Haiku).
MODEL_IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# Without these, a headless call loads MCP servers, settings and tool prompts: ~72k tokens instead of ~1.3k.
LEAN_FLAGS = ["--tools", "", "--no-session-persistence", "--strict-mcp-config",
              "--setting-sources", "", "--disable-slash-commands"]


# A resting Claude (waggle/cooldowns.py) raises one of the kinds callers already treat as "try later".
_REST_KIND = {"limit": "limit", "credit": "limit", "busy": "timeout", "down": "timeout", "auth": "auth",
              "missing": "missing"}


def _resting():
    """A ClaudeError for a Claude that failed a moment ago (a limit, billing, overload or sign-in problem), raised
    before any process starts; None when it is free."""
    on, kind, until, why = cooldowns.cooling("claude")
    if not on:
        return None
    err = ClaudeError(f"Claude is {cooldowns.describe('claude')} ({why[:80]})", _REST_KIND.get(kind, "error"))
    err.resting = True
    return err


def _rest_after(e):
    """Start the rest an error of this kind earns. Never raises; a rest itself is not noted again."""
    if getattr(e, "resting", False):
        return
    cooldowns.note_error("claude", e.kind, str(e), reset_in_hours=e.reset_in_hours)


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
                             encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
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
    err = _resting()
    if err:
        raise err
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
                                  encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        _rest_after(e)
        raise
    finally:
        quota.record(wanted, usage, purpose, headless=True, ok=ok, error_kind=kind)


STREAM_FLAGS = ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
TOOL_RESULT_CAP = 500  # characters of a tool's result kept for the trace (after redaction)

_AUTH_WORDS = ("authenticate", "authentication", "oauth", "log in", "login", "not logged in", "invalid api key",
               "credentials")
_LIMIT_WORDS = ("usage limit", "limit reached", "hit your limit", "rate limit", "rate_limit", "quota",
                "limit will reset", "resets ")


def error_kind(msg):
    """auth | limit | error, from a CLI error text."""
    low = (msg or "").lower()
    if any(w in low for w in _AUTH_WORDS):
        return "auth"
    if any(w in low for w in _LIMIT_WORDS) or re.search(r"\blimit\b", low):
        return "limit"
    return "error"


def _redact(text):
    from .policy import redact
    return redact(text)


_REFUSAL_MARKS = ("NEEDS_APPROVAL", "Blocked by Waggle's safety policy")


def is_refusal(body, error=True):
    """True when a tool result is the safety hook turning the call down (the safety hook's two messages: "NEEDS_
    APPROVAL [code]: ..." and "Blocked by Waggle's safety policy: ..."). A successful result that merely mentions
    those words (a file about the policy) is not a refusal: it must be an error result, or start with the mark."""
    body = str(body or "")
    if body.lstrip().startswith(_REFUSAL_MARKS):
        return True
    return bool(error) and any(m in body for m in _REFUSAL_MARKS) or (bool(error) and "safety policy" in body)


class StreamParser:
    """Reads Claude Code `stream-json` output (with --include-partial-messages) one line at a time.

    - text: everything the model wrote this turn (text blocks of every assistant message, in order). This is
      what the user sees while it streams, so it is also what is kept as the reply.
    - steps: tool calls with their outcome ({"tool", "id", "input", "ok", "denied", "result"}), for the trace;
      inputs and results are redacted, results cut to TOOL_RESULT_CAP characters.
    - result: the final `result` event (None until the turn ends); session_id from any event.
    - garbage: lines that were not stream-json (counted, never fatal).
    on_text(full_text) fires on every new piece of text; on_tool(step) when a tool call starts.
    Events from sub-agents (parent_tool_use_id set) are ignored: only the main conversation is shown.
    feed() takes whole lines; feed_chunk() takes raw output and keeps an incomplete last line for later."""

    def __init__(self, on_text=None, on_tool=None, clock=time.time):
        self.on_text, self.on_tool, self.clock = on_text, on_tool, clock
        self.parts, self.steps, self._by_id, self._streamed = [], [], {}, set()
        self.result, self.session_id, self.first_text_at, self.rate_limit, self.model = None, "", None, None, ""
        self.garbage, self._buf, self._msg_id = 0, "", None
        self._open = False  # a text block is being streamed

    @property
    def text(self):
        return "\n\n".join(p.strip() for p in self.parts if p.strip())

    def _emit(self):
        if self.first_text_at is None and self.text:
            self.first_text_at = self.clock()
        if self.on_text and self.text:
            self.on_text(self.text)

    def feed_chunk(self, data):
        """Raw output that may end in the middle of a line. Returns True once the result has arrived."""
        self._buf += data or ""
        done = False
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            done = self.feed(line) or done
        return done

    def feed(self, line):
        """One output line. Returns True when the turn's result has arrived."""
        line = (line or "").strip()
        if not line:
            return False
        if not line.startswith("{"):
            self.garbage += 1
            return False
        try:
            ev = json.loads(line)
            if not isinstance(ev, dict):
                raise ValueError("not an event")
            return self._event(ev)
        except (ValueError, TypeError, AttributeError, KeyError):
            self.garbage += 1  # a partial line, a stray print, an event shape we don't know
            return False

    def _event(self, ev):
        if isinstance(ev.get("session_id"), str) and ev["session_id"]:
            self.session_id = ev["session_id"]
        if ev.get("parent_tool_use_id"):
            return False
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.model = ev.get("model") or self.model
        elif t == "stream_event":
            self._stream_event(ev)
        elif t == "assistant":
            msg = ev.get("message") or {}
            streamed = msg.get("id") is not None and msg.get("id") in self._streamed
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and not streamed and block.get("text"):
                    self.parts.append(block["text"])  # no partial events for this message: take it whole
                    self._open = False
                    self._emit()
                elif block.get("type") == "tool_use" and block.get("id") not in self._by_id:
                    step = {"tool": block.get("name", ""), "id": block.get("id") or "",
                            "input": _redact(json.dumps(block.get("input"), default=str))[:400],
                            "ok": None, "denied": False, "result": ""}
                    self._by_id[block.get("id")] = step
                    self.steps.append(step)
                    if self.on_tool:
                        self.on_tool(step)
        elif t == "user":
            content = (ev.get("message") or {}).get("content") or []
            for block in content if isinstance(content, list) else []:
                step = self._by_id.get(block.get("tool_use_id")) if isinstance(block, dict) else None
                if not step or block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                body = str(body or "")
                step["ok"] = not block.get("is_error")
                step["denied"] = is_refusal(body, error=not step["ok"])
                step["result"] = _redact(body)[:TOOL_RESULT_CAP]
        elif t == "rate_limit_event":
            self.rate_limit = ev.get("rate_limit_info")
            try:  # the real used share of the 5-hour window; must never break a turn
                from . import quota
                quota.note_real_usage(self.rate_limit)
            except Exception:
                pass
        elif t == "result":
            self.result = ev
            if not self.text and ev.get("result") and not ev.get("is_error"):
                self.parts.append(str(ev["result"]))
                self._emit()
            return True
        return False

    def _stream_event(self, ev):
        e = ev.get("event") or {}
        et = e.get("type")
        if et == "message_start":
            self._msg_id = (e.get("message") or {}).get("id")
            self._open = False
        elif et == "content_block_start":
            kind = (e.get("content_block") or {}).get("type")
            if kind == "text":
                self.parts.append("")
                self._open = True
            else:
                self._open = False
        elif et == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
            if not self._open:
                self.parts.append("")
                self._open = True
            for mid in (self._msg_id, ev.get("api_message_id")):
                if mid:
                    self._streamed.add(mid)
            self.parts[-1] += e["delta"].get("text", "")
            self._emit()
        elif et == "content_block_stop":
            self._open = False

    def error(self):
        """(kind, message) when the turn failed, else None."""
        r = self.result
        if r is None:
            return "error", "no result"
        if r.get("is_error") or r.get("subtype") not in (None, "success"):
            errs = r.get("errors")
            errs = "; ".join(str(e) for e in errs) if isinstance(errs, list) else ""
            msg = str(r.get("result") or errs or r.get("terminal_reason") or r.get("subtype") or "unknown error")
            if r.get("api_error_status") == 429:
                return "limit", msg[:300]
            return error_kind(msg), msg[:300]
        return None

    def main_model(self):
        per = (self.result or {}).get("modelUsage") or {}
        if isinstance(per, dict) and per:
            return max(per, key=lambda k: (per[k] or {}).get("outputTokens", 0))
        return self.model


def stream(prompt, model="sonnet", system=None, effort="low", purpose="stream", timeout=300, on_text=None,
           popen=None):
    """One lean headless call that streams: on_text(full_text_so_far) fires as words arrive. Plain text, no
    JSON schema. Returns {'text', 'usage', 'model_id', 'ttfw_s', 'total_s'}; raises ClaudeError."""
    claude = find_claude() if popen is None else "claude"
    if not claude:
        raise ClaudeError("Claude Code CLI not found. Set CLAUDE_BIN.", "missing")
    wanted = MODEL_IDS.get(model, model)
    err = _resting()
    if err:
        raise err
    cmd = [claude, "-p", "--model", wanted, "--effort", effort, *STREAM_FLAGS, *LEAN_FLAGS]
    if system:
        cmd += ["--system-prompt", system]
    t0 = time.time()
    parser = StreamParser(on_text=on_text)
    ok, kind = False, None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            proc = (popen or subprocess.Popen)(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True, env=clean_env(), cwd=tmp,
                                               encoding="utf-8", errors="replace",
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.stdin.write(prompt)
            proc.stdin.close()
            deadline = t0 + timeout
            for line in proc.stdout:
                if parser.feed(line):
                    break
                if time.time() > deadline:
                    proc.kill()
                    kind = "timeout"
                    raise ClaudeError(f"Claude CLI timed out after {timeout}s", "timeout")
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        err = parser.error()
        if err:
            kind = err[0]
            raise ClaudeError(err[1], kind, parse_reset_hours(err[1]))
        ok = True
        return {"text": parser.text, "usage": (parser.result or {}).get("usage") or {},
                "model_id": parser.main_model() or wanted,
                "ttfw_s": round(parser.first_text_at - t0, 2) if parser.first_text_at else None,
                "total_s": round(time.time() - t0, 2)}
    except ClaudeError as e:
        if e.kind == "limit":
            quota.note_limit_hit(reset_in_hours=e.reset_in_hours)
        _rest_after(e)
        raise
    finally:
        quota.record(wanted, (parser.result or {}).get("usage") or {}, purpose, headless=True, ok=ok,
                     error_kind=kind)


def start_session(prompt, model, cwd, log_path, system_append=None, permission_mode="bypassPermissions",
                  mcp_config=None):
    """Launch a full Claude Code run (tools on, session saved) as a detached background process."""
    claude = find_claude()
    if not claude:
        raise ClaudeError("Claude Code CLI not found. Set CLAUDE_BIN.", "missing")
    cmd = [claude, "-p", "--model", MODEL_IDS.get(model, model), "--output-format", "stream-json",
           "--verbose", "--permission-mode", permission_mode]
    if system_append:
        cmd += ["--append-system-prompt", system_append]
    if mcp_config:  # an extra MCP server file for this run only (a proposal build: Context7)
        cmd += ["--mcp-config", str(mcp_config)]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True,
                            env=clean_env(), cwd=cwd, encoding="utf-8", errors="replace", creationflags=flags)
    proc.stdin.write(prompt)
    proc.stdin.close()
    return proc
