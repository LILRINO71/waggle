"""One permission policy for every tool call Waggle's Claude agents make.

Enforced by a Claude Code PreToolUse hook (`waggle.agent_hook`), so a prompt injection cannot argue its
way past it. The user's choice (2026-11-18, "full auto in projects"):
- runs on its own: reading anywhere (except secret files), web search/fetch, file edits, scripts and
  package installs inside the project roots (Claude folder, vault, Waggle's own folders);
- needs the user's yes: money, passwords/secrets, messaging or posting as the user, deleting the user's
  data, security/system settings, writing outside the roots, system-wide installs;
- never: disabling security, piping downloads straight into a shell, reading credential stores.

"ask" cannot pop up a dialog in a headless run, so it becomes a one-time approval request: the call is
denied with an id and a 4-letter code, Waggle tells the user, and "approve <code>" grants exactly that call, in
that session, once (approvals.db). A bare "yes" only answers an approval the same chat was just asked about.
"""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import guard
from .config import Settings, data_path

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass
class Decision:
    action: str
    reason: str = ""
    approval_id: str = ""
    kind: str = ""  # why an ASK asks (guard, secret, money, outside, shell, destroy, outbound, system, ...): owner_rule

    @property
    def allowed(self):
        return self.action == ALLOW


# ---- secrets -----------------------------------------------------------------------------------

SECRET_PATTERNS = [
    (r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}", "api-key"),
    (r"gh[pousr]_[A-Za-z0-9]{30,}", "github-token"), (r"github_pat_[A-Za-z0-9_]{40,}", "github-token"),
    (r"xox[abposr]-[A-Za-z0-9-]{10,}", "slack-token"), (r"AKIA[0-9A-Z]{16}", "aws-key"),
    (r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", "telegram-token"),
    (r"\bAC[0-9a-f]{32}\b", "twilio-sid"), (r"\bSK[0-9a-f]{32}\b", "twilio-key"),
    (r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "jwt"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "private-key"),
    (r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}", "bearer"),
    (r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|auth[_-]?token)\s*[=:]\s*)[\"']?[^\s\"']{6,}", "assigned"),
    (r"\b[3-6]\d{3}(?:[ -]?\d{4}){2}[ -]?\d{3,4}\b", "card-number"),
]
_SECRET_RES = [(re.compile(p), name) for p, name in SECRET_PATTERNS]


def redact(text):
    """Replace anything that looks like a credential with [REDACTED:<kind>]."""
    if not text:
        return text
    out = str(text)
    for rx, name in _SECRET_RES:
        if name == "assigned":
            out = rx.sub(lambda m: m.group(1) + f"[REDACTED:{name}]", out)
        elif name == "card-number":
            out = rx.sub(lambda m: f"[REDACTED:{name}]" if _luhn(m.group(0)) else m.group(0), out)
        else:
            out = rx.sub(f"[REDACTED:{name}]", out)
    return out


def _luhn(s):
    digits = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def has_secret(text):
    return redact(text) != str(text or "")


# ---- paths -------------------------------------------------------------------------------------

SECRET_FILES = re.compile(
    r"(^|/)(\.env(\.[\w-]+)?|[\w-]*\.env|id_rsa[\w.]*|id_ed25519[\w.]*|[\w-]*\.pem|[\w-]*\.pfx|[\w-]*\.p12|"
    r"credentials(\.json)?|\.netrc|\.git-credentials|auth\.json|token\.json|google_token\.json|"
    r"client_secret[\w-]*\.json|login data|cookies(\.sqlite)?|key[34]\.db|logins\.json|\.npmrc|\.pypirc)$", re.I)
SECRET_DIRS = re.compile(r"/(\.ssh|\.gnupg|\.aws|microsoft/credentials|microsoft/protect|1password|bitwarden)(/|$)",
                         re.I)


def norm(path):
    """'C:\\Users\\x' / '/c/Users/x' / '~/x' -> 'c:/users/x' (lowercase, forward slashes)."""
    p = os.path.expanduser(str(path or "").strip().strip('"'))
    p = p.replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        p = f"{m.group(1)}:/{m.group(2)}"
    p = re.sub(r"/+", "/", p)
    parts = []
    for seg in p.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg != ".":
            parts.append(seg)
    return "/".join(parts).lower().rstrip("/")


def write_roots(settings=None):
    s = settings or Settings.load()
    home = Path.home()
    roots = [home / "Claude", s.vault, s.jobs_dir, data_path("."), home / ".hermes" / "skills" / "waggle",
             os.environ.get("TEMP", ""), *getattr(s, "extra_write_roots", [])]
    return [norm(r) for r in roots if str(r)]


def is_secret_path(path):
    p = "/" + norm(path)
    return bool(SECRET_FILES.search(p) or SECRET_DIRS.search(p) or p.endswith("/.waggle/twilio.env"))


def under(path, roots):
    p = norm(path)
    return any(p == r or p.startswith(r + "/") for r in roots)


# ---- shell -------------------------------------------------------------------------------------

NEVER_SHELL = [  # (pattern, reason): refused outright
    (r"Set-MpPreference|Add-MpPreference\s+-Exclusion|DisableRealtimeMonitoring|sc(\.exe)?\s+(stop|config)\s+windefend",
     "turning off Windows Defender"),
    (r"(curl|wget|iwr|Invoke-WebRequest|irm|Invoke-RestMethod)\b[^|\n]*\|\s*(sh|bash|iex|Invoke-Expression|python|pwsh|powershell)\b",
     "running a download straight from the internet"),
    (r"\biex\s*\(\s*(New-Object\s+Net\.WebClient|iwr|irm|Invoke-WebRequest)", "running a download straight from the internet"),
    (r"\b(format(\.com)?\s+[a-z]:|diskpart|bcdedit|vssadmin\s+delete|cipher\s+/w)", "disk or boot changes"),
    (r"\bnet\s+user\b.*\s/(add|delete)|\bnet\s+localgroup\s+administrators\b", "changing Windows accounts"),
    (r"netsh\s+advfirewall\s+set\s+\w+\s+state\s+off|Set-NetFirewallProfile.*-Enabled\s+False", "turning off the firewall"),
    (r"(Login Data|Cookies|key3\.db|key4\.db|logins\.json|Microsoft[/\\]Credentials|cmdkey\s+/list|vaultcmd)",
     "reading saved browser or Windows credentials"),
]
ASK_SHELL = [
    (r"(^|[;&|(]\s*)(rm|del|erase|rmdir|rd|ri)\s+\S|\bRemove-Item\b|\bshutil\.rmtree\b|\bos\.remove\b",
     "deleting files"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*[fdx]|push\s+[^\n]*(--force|-f\b)|branch\s+-D|checkout\s+--\s|restore\s+\.)",
     "a git command that throws away work"),
    (r"\b(shutdown|Restart-Computer|Stop-Computer|logoff)\b", "restarting or shutting down the PC"),
    (r"\breg(\.exe)?\s+(add|delete|import)\b|Set-ItemProperty\s+-Path\s+HK|New-ItemProperty\s+-Path\s+HK", "changing the registry"),
    (r"\bSet-ExecutionPolicy\b|\bschtasks\s+/(create|change|delete)\b|Register-ScheduledTask", "changing system settings"),
    (r"\b(winget|choco|scoop|msiexec)\s+(install|uninstall|upgrade)\b|\bnpm\s+(i|install)\s+(-g|--global)\b",
     "a system-wide install"),
    (r"\bgh\s+(issue|pr|release|gist|repo)\s+(create|comment|close|delete|edit|merge)\b|\bgh\s+api\s+[^\n]*-X\s*(POST|PATCH|DELETE)",
     "posting to GitHub as you"),
    (r"(curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)|-Method\s+(Post|Put|Patch|Delete)|--data|-d\s|-F\s|--upload-file|-Body\b|-InFile\b)",
     "sending data to a website"),
    (r"\b(Send-MailMessage|smtplib|sendmail)\b|api\.telegram\.org/bot|api\.twilio\.com|hooks\.slack\.com|discord(app)?\.com/api/webhooks",
     "sending a message as you"),
    (r"(\.env\b|twilio\.env|id_rsa|\.pem\b|credentials\.json|token\.json|\.git-credentials)", "touching a secrets file"),
    (r"\b(takeown|icacls)\b", "changing file permissions"),
]
REGENERABLE = re.compile(r"(node_modules|__pycache__|\.pytest_cache|\.venv|venv|dist|build|\.next|\.cache|target|"
                         r"coverage|\.mypy_cache|\.ruff_cache|tmp|temp)[\\/\"']?\s*$", re.I)


def classify_shell(cmd):
    """(action, reason) for one shell command line."""
    c = cmd or ""
    for pat, why in NEVER_SHELL:
        if re.search(pat, c, re.I):
            return DENY, why
    for pat, why in ASK_SHELL:
        if re.search(pat, c, re.I):
            if why == "deleting files" and _only_regenerable_deletes(c):
                continue
            return ASK, why
    if has_secret(c):
        return ASK, "the command contains what looks like a password or key"
    return ALLOW, ""


def _only_regenerable_deletes(cmd):
    """`rm -rf node_modules` / `Remove-Item -Recurse dist` are build junk, not the user's data."""
    pieces = [p.strip() for p in re.split(r"&&|;|\|\||\n", cmd)]
    deletes = [p for p in pieces if re.match(r"(rm|del|erase|rmdir|rd|ri)\s", p, re.I) or "remove-item" in p.lower()]
    return bool(deletes) and all(REGENERABLE.search(p.strip()) for p in deletes)


# ---- tools -------------------------------------------------------------------------------------

READ_TOOLS = {"read", "glob", "grep", "ls", "notebookread", "websearch", "webfetch", "todowrite", "todoread",
              "taskoutput", "toolsearch"}
WRITE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
SHELL_TOOLS = {"bash", "powershell", "terminal", "shell", "execute_command", "run_command", "process"}
# MCP / Hermes tool names that act on other people or the outside world.
OUTWARD = re.compile(r"(^|_)(send|forward|reply|post|publish|share|invite|tweet|dm|email_send|sms|call_me_now|"
                     r"make_call|transfer|payment|purchase|buy|checkout|pay)(_|$)", re.I)
DESTROY = re.compile(r"(^|_)(delete|trash|remove|purge|wipe|archive_all|empty|spam|unsubscribe|revoke)(_|$)", re.I)
MONEY = re.compile(r"payment|purchase|checkout|transfer|trade|order|billing|invoice|refund", re.I)
# Waggle's own MCP tools (waggle/waggle_mcp.py and the modules it loads), one row each: the decision and why.
# Allowed: what reaches only the user (their own chat, phone and reminders), reads and status, and Waggle's own
# memory, skills, routines and agent work (which runs under this same policy). Asks: anything that reaches other
# people, spends money, deletes, or installs code. A Waggle tool missing from this table asks
# (tests/test_guard.py checks that every tool the server offers has a row).
_ONLY_USER = "reaches only the user"
_READS = "reads or reports status"
_OWN = "Waggle's own memory, skills or agent work"
WAGGLE_TOOLS = {
    # to the user only: their own chat, phone and reminders
    "remind_me": (ALLOW, "a reminder in the user's own chat"),
    "cancel_reminder": (ALLOW, "cancels one of the user's own reminders"),
    "list_reminders": (ALLOW, _READS),
    "schedule_call": (ALLOW, "a call to the user's own phone at a time they asked for"),
    "call_me": (ALLOW, "a call to the user's own phone"),
    "notify_me": (ALLOW, _ONLY_USER),
    "send_to_me": (ALLOW, _ONLY_USER),
    # reads and status
    "vault_search": (ALLOW, _READS), "project_status": (ALLOW, _READS), "build_status": (ALLOW, _READS),
    "waggle_status": (ALLOW, _READS), "weather": (ALLOW, _READS), "look_at_screen": (ALLOW, _READS),
    "skill_search": (ALLOW, _READS), "skill_load": (ALLOW, _READS), "skill_list": (ALLOW, _READS),
    "skill_hub_search": (ALLOW, _READS), "recall": (ALLOW, _READS), "routine_list": (ALLOW, _READS),
    "agent_list": (ALLOW, _READS), "agent_status": (ALLOW, _READS), "jobs_list": (ALLOW, _READS),
    "hermes_tools": (ALLOW, _READS), "model_catalog": (ALLOW, _READS), "learning_report": (ALLOW, _READS),
    "memory_status": (ALLOW, _READS), "memory_review": (ALLOW, _READS), "objective_status": (ALLOW, _READS),
    "graph_query": (ALLOW, _READS), "background_status": (ALLOW, _READS), "waggle_doctor": (ALLOW, _READS),
    "note_read": (ALLOW, _READS), "related_to": (ALLOW, _READS), "memory_core": (ALLOW, _READS),
    # Waggle's own memory, skills, routines and agents (agent work goes through this policy again)
    "remember": (ALLOW, _OWN), "project_update": (ALLOW, _OWN), "rate_finding": (ALLOW, _OWN),
    "skill_create": (ALLOW, _OWN), "skill_update": (ALLOW, _OWN), "agent_note": (ALLOW, _OWN),
    "graph_note": (ALLOW, _OWN), "routine_add": (ALLOW, _OWN), "routine_pause": (ALLOW, _OWN),
    "routine_resume": (ALLOW, _OWN), "delegate": (ALLOW, _OWN), "start_build": (ALLOW, _OWN),
    "objective_create": (ALLOW, _OWN), "objective_control": (ALLOW, _OWN),
    # still asks
    "background_task": (ASK, "starts a background agent job from chat"),
    "graph_correct": (ASK, "retracts or merges what Waggle knows about you"),
    "routine_remove": (ASK, "deletes a routine"),
    "skill_install": (ASK, "installs community code"),
    "hermes_tool": (ASK, "runs a Hermes tool"),  # decided as the Hermes tool it names (decide() below)
    "settings": (ASK, "changes Waggle's comfort settings (voice, speed, quiet hours...)"),  # owner turn only (owner_acts)
    "file_proposal": (ASK, "files a proposal to change Waggle's own code"),  # owner turn only (owner_acts); the owner still approves every merge
}
WAGGLE_SAFE = {name for name, (action, _why) in WAGGLE_TOOLS.items() if action == ALLOW}


HERMES_READ = {"memory", "session_search", "todo_list", "skills_list", "skill_view", "text_to_speech", "read_terminal",
               "read_window_below", "desktop_preview", "drive_preview", "annotate_preview", "focus_pane", "show_tip",
               "gui_tour", "delegate_task", "look_at_screen", "vault_search", "project_status", "build_status",
               "waggle_status", "weather", "remember", "project_update", "rate_finding", "start_build"}
HERMES_ALLOW_UNLESS_DELETE = {"cronjob_manage", "skill_manage", "process_manage", "todo"}
HERMES_CONFIG = {"manage_catalog", "manage_connections", "set_theme", "setup_beat", "desktop_project",
                 "close_terminal", "call_me"}
SENSITIVE_WEB = re.compile(r"checkout|/cart|payment|billing|paypal|venmo|zelle|stripe|bank|coinbase|robinhood|kalshi|"
                           r"wallet|purchase|buy now|place order|password|/login|signin|sign-in|oauth|"
                           r"mail\.google|outlook\.live|web\.whatsapp|discord\.com/channels|messenger|"
                           r"\b(send|post|tweet|publish|submit order)\b", re.I)


SHELL_TOOLS = {"bash", "powershell"}


def _key_args(tool, args):
    """What identifies a call for approval. Shell tools: only the command, whitespace-normalised (the free-text
    description or timeout often differ when a step is re-run after the yes). Everything else: all arguments."""
    if (tool or "").lower().split("__")[-1] in SHELL_TOOLS and isinstance(args, dict) and args.get("command"):
        return {"command": " ".join(str(args["command"]).split())}
    return args or {}


def _short(tool, args):
    key_args = _key_args(tool, args)
    if set(key_args) == {"command"} and (tool or "").lower().split("__")[-1] in SHELL_TOOLS:
        return redact(f"{tool}: $ {key_args['command']}")[:300]
    try:
        body = json.dumps(args, sort_keys=True, default=str)
    except TypeError:
        body = str(args)
    return redact(f"{tool} {body}")[:300]


def _recipients(args):
    out = []
    for k in ("to", "cc", "bcc", "recipients", "recipient"):
        v = (args or {}).get(k)
        if isinstance(v, str):
            out += [x for x in re.split(r"[,;]+", v) if x.strip()]
        elif isinstance(v, (list, tuple)):
            for x in v:
                out.append(x.get("email", "") if isinstance(x, dict) else str(x))
    return [re.sub(r".*<([^>]+)>.*", r"\1", x).strip().lower() for x in out if x]


def _only_to_self(args, settings=None):
    """Every recipient is one of the user's own addresses (Settings.self_emails): it reaches nobody else."""
    mine = {e.lower() for e in (settings or Settings.load()).self_emails}
    who = _recipients(args)
    return bool(mine and who) and all(w in mine for w in who)


# Names the older Hermes bridge used for a shell; kept so the guard still looks at their command.
_OTHER_SHELL = {"terminal", "shell", "execute_command", "run_command", "process"}
_MUTATING_MCP = re.compile(r"write|edit|create|save|move|copy|rename|delete|remove|append|patch|replace|upload|"
                           r"overwrite", re.I)
# MCP tool names that only look: every other MCP tool (put_file, run, apply...) has all its string arguments checked.
_LOOKING_MCP = re.compile(r"^(read|get|list|search|find|view|show|query|fetch|describe|stat|head|tree|count|"
                          r"download|export)(_|$)|_(info|status|metadata)$", re.I)


def _guard_hit(name, base, args, cwd):
    """Why this call needs the user's yes because it could change a protected safety file (waggle.guard), or None.
    Runs before the write-roots test, so a protected file inside a write root is still asked about."""
    try:
        if name in WRITE_TOOLS or name in {"write_file", "patch"}:
            return guard.write_hit(args, cwd, patch=name == "patch")
        if name in SHELL_TOOLS or name in _OTHER_SHELL:
            # Claude Code's Bash tool runs Git Bash and its PowerShell tool runs PowerShell; other shell tools: any.
            return guard.shell_hit(args.get("command") or args.get("cmd") or "",
                                   args.get("cwd") or args.get("workdir") or cwd, shell=name if name in SHELL_TOOLS
                                   else None)
        if name == "execute_code":  # Hermes runs this Python itself
            return guard.code_hit(args.get("code") or "", args.get("cwd") or args.get("workdir") or cwd)
        if name.startswith("mcp__") and not name.startswith("mcp__waggle__") and (
                _MUTATING_MCP.search(base) or not _LOOKING_MCP.search(base)):
            return guard.write_hit(args, cwd, deep=True)
    except Exception:  # prefer asking too often over letting an unchecked call through
        return "the guard could not check whether this touches Waggle's safety files"
    return None


def decide(tool_name, args=None, settings=None, cwd=None, turn=None):
    """Decision for one tool call (before approvals are consulted). turn: the running turn's state
    (guard.read_turn_state); in the owner's own turn an ASK the owner's message covers becomes ALLOW (owner_rule)."""
    d = _base_decide(tool_name, args, settings, cwd)
    if d.action == ASK and turn:
        return owner_rule(tool_name, args if isinstance(args, dict) else {}, d, turn, settings, cwd) or d
    return d


def _base_decide(tool_name, args=None, settings=None, cwd=None):
    """Today's rules for one tool call: allow, ask (with a kind saying why) or deny."""
    bad = guard.gate()  # fail closed like the hook when the safety files on disk no longer match the manifest
    if bad:
        return Decision(DENY, "integrity_violation: " + guard.denial_text(bad))
    args = args or {}
    raw = tool_name or ""
    name = raw.lower()
    base = name.split("__")[-1]  # mcp__server__tool -> tool
    hit = _guard_hit(name, base, args, cwd) if isinstance(args, dict) else None
    if hit and not (name in SHELL_TOOLS and classify_shell(str(args.get("command") or args.get("cmd") or ""))[0] == DENY):
        return Decision(ASK, hit, kind="guard")
    if name in READ_TOOLS:
        path = args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
        if path and is_secret_path(path):
            return Decision(ASK, f"reading a secrets file ({Path(str(path)).name})", kind="secret")
        return Decision(ALLOW)
    if name in WRITE_TOOLS:
        path = args.get("file_path") or args.get("notebook_path") or ""
        if is_secret_path(path):
            return Decision(ASK, f"changing a secrets file ({Path(str(path)).name})", kind="secret")
        content = str(args.get("content") or args.get("new_string") or "")
        if has_secret(content):
            return Decision(ASK, "writing what looks like a password or key into a file", kind="secret")
        if under(path, write_roots(settings)):
            return Decision(ALLOW)
        return Decision(ASK, f"changing a file outside your project folders ({path})", kind="outside")
    if name in SHELL_TOOLS:
        action, why = classify_shell(str(args.get("command") or args.get("cmd") or ""))
        return Decision(action, why, kind="shell" if action == ASK else "")
    if name.startswith("mcp__waggle__"):
        if base == "hermes_tool":
            return _base_decide(str(args.get("name", "")), args.get("args") or {}, settings, cwd)
        if base == "settings" and args.get("action") == "get":  # reading the comfort settings changes nothing
            return Decision(ALLOW)
        action, why = WAGGLE_TOOLS.get(base, (ASK, "a Waggle tool the policy does not know"))
        return Decision(action) if action == ALLOW else Decision(ASK, f"Waggle tool {base}: {why}", kind="waggle")
    if name == "computer_use":
        from .security import screen_risk
        why = screen_risk(args)
        return Decision(ASK, f"controlling your screen ({why})", kind="screen") if why else Decision(ALLOW)
    if name in {"send_message", "send_email", "telegram_send", "discord_send", "sms_send"} or OUTWARD.search(base):
        if base == "create_draft":
            return Decision(ALLOW)
        if base in ("send_message", "send_email") and _only_to_self(args, settings):
            return Decision(ALLOW, "to the user's own inbox")
        money = MONEY.search(base) or re.search(r"(^|_)(transfer|payment|purchase|buy|checkout|pay)(_|$)", base)
        return Decision(ASK, f"{base.replace('_', ' ')}: this reaches other people or the outside world",
                        kind="money" if money else "outbound")
    if DESTROY.search(base):
        return Decision(ASK, f"{base.replace('_', ' ')}: deletes or removes your data",
                        kind="money" if MONEY.search(base) else "destroy")
    if MONEY.search(base):
        return Decision(ASK, f"{base}: involves money", kind="money")
    if name.startswith("mcp__claude_ai_") or name.startswith("mcp__"):
        attendees = args.get("attendees") or args.get("guests")
        if attendees:
            return Decision(ASK, "this would invite other people", kind="invite")
        if re.search(r"permission|sharing|acl", base):
            return Decision(ASK, "changes who can see your files", kind="share")
        return Decision(ALLOW)
    if name in {"task", "agent"}:
        return Decision(ALLOW)
    # Hermes tools reached through the bridge use their plain names.
    if name in {"web_search", "web_extract", "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
                "browser_scroll", "browser_back", "browser_press", "browser_get_images", "browser_vision",
                "browser_console", "vision_analyze", "text_to_speech", "skills_list", "skill_view", "read_file",
                "search_files", "list_directory", "cronjob_list", "session_search", "clarify", "todo"}:
        return Decision(ALLOW)
    # The rest of the live Hermes registry (bridge), tool by tool.
    blob = json.dumps(args, default=str).lower()
    if name in HERMES_READ:
        return Decision(ALLOW)
    if name.startswith("browser_") and name != "browser_vault":
        if SENSITIVE_WEB.search(blob):
            return Decision(ASK, "browser action on a payment, account or messaging page", kind="web")
        return Decision(ALLOW)
    if name == "browser_vault":
        return Decision(ASK, "using saved passwords / autofill", kind="secret")
    if name in HERMES_ALLOW_UNLESS_DELETE:
        if re.search(r"\b(delete|remove|kill|terminate|uninstall|purge)\b", blob):
            return Decision(ASK, f"{name}: removes or stops something", kind="destroy")
        return Decision(ALLOW)
    if name in HERMES_CONFIG:
        return Decision(ASK, f"{name}: changes Hermes settings", kind="system")
    if name in {"write_file", "patch"}:
        return _base_decide("write", {"file_path": args.get("path") or args.get("file_path") or "",
                                      "content": args.get("content", "")}, settings)
    if name in {"cronjob", "cronjob_manage"}:
        return Decision(ALLOW)
    return Decision(ASK, f"an unfamiliar tool ({raw})", kind="unknown")


# ---- the owner's instruction -------------------------------------------------------------------
# The owner's decision (2026-11-20): "any task I tell it to do, it should do the first time". In a turn whose origin
# is "user" (the owner's own message, guard.read_turn_state), an action that today ASKs is allowed when the owner's
# message covers it (covers(): one fixed rule, no model call). What still asks in such a turn: speech recognition
# unsure of the words and the action destroys or reaches others (worded as a question about what was heard); a
# destructive or outbound action on something the owner did not name; money; Waggle's protected safety files,
# approval and budget databases (the guard); secrets; screen control and payment/login pages; unknown tools. Every
# DENY stays a DENY, and every turn that is not the owner's keeps today's rules. Only the turn state's text counts as
# the owner's words: what a tool fetched (web pages, mail, files) never reaches it. Off switch: config.json
# "owner_instructions": false (a protected key).

# Instruction verbs by family. Only the plain (imperative) form counts: "deleted" or "deleting" is a report, not an
# instruction. Multi-word verbs are matched first.
VERB_FAMILIES = {
    "delete": ("delete", "remove", "trash", "wipe", "get rid of", "erase", "drop", "discard", "purge", "destroy",
               "nuke", "forget", "reset", "revert", "throw away"),
    "clean": ("clean up", "clean out", "clean", "clear out", "clear", "empty", "tidy up", "tidy"),  # contents only
    "move": ("archive", "move", "rename", "merge", "combine", "consolidate"),
    "edit": ("add", "create", "make", "write", "edit", "update", "fix", "change", "modify", "replace", "append",
             "set", "turn on", "turn off", "enable", "disable", "switch", "correct"),
    "run": ("install", "uninstall", "upgrade", "run", "build", "start", "stop", "restart", "reboot", "shut down",
            "shutdown", "kill", "launch", "close", "log off", "schedule"),
    "send": ("send", "email", "message", "text", "post", "reply", "forward", "share", "invite", "publish", "tweet",
             "dm"),
    "git": ("commit", "push", "pull request", "pr", "release"),
}
_PHRASES = sorted(((tuple(v.split()), fam) for fam, vs in VERB_FAMILIES.items() for v in vs), key=lambda x: -len(x[0]))
_NOUN_OK = {("pr",), ("pull", "request")}  # "open a PR": a determiner does not make these nouns
_DETERMINERS = {"the", "a", "an", "my", "your", "this", "that", "these", "those", "his", "her", "their", "our", "its",
                "some", "any", "every", "each"}
_NEG = {"not", "dont", "never", "no", "nothing", "without", "cant", "cannot", "shouldnt", "wont", "mustnt", "avoid",
        "nor", "neither"}
_REPORT = {"says", "said", "say", "saying", "wrote", "writes", "asks", "asked", "wants", "wanted", "told", "tells",
           "mentions", "mentioned", "suggests", "suggested", "claims", "claimed", "reads", "according", "instructs",
           "instructed", "recommends", "recommended", "is", "was", "has", "from"}
_QUESTION = {"what", "why", "how", "when", "where", "who", "whom", "whose", "which", "does", "did", "do", "is", "are",
             "was", "were", "has", "have", "had", "should", "shall", "am", "isnt", "arent", "doesnt", "didnt"}
_HYPOTHETICAL = {("should", "i"), ("should", "we"), ("shall", "i"), ("shall", "we"), ("do", "i"), ("how", "do"),
                 ("what", "if"), ("is", "it"), ("would", "it")}
_EXCEPT = {"except", "besides", "excluding", "but", "unless"}
_EXCEPT2 = {("other", "than"), ("apart", "from"), ("aside", "from"), ("but", "not")}
_PROTECT = {"keep", "leave", "preserve", "spare", "skip", "ignore", "exclude"}
_LOC = {"in", "inside", "from", "under", "within", "at", "on", "into", "onto", "to", "of"}
_ALLISH = {"everything", "all", "anything", "contents", "content", "every", "whatever"}
_TYPE = {"file", "files", "folder", "folders", "directory", "directories", "dir", "repo", "repos", "repository",
         "repositories", "note", "notes", "project", "projects", "document", "documents", "thing", "things", "stuff",
         "copy", "copies", "version", "versions", "branch", "branches", "page", "one", "ones"}
_FILE_WORDS, _DIR_WORDS = {"file", "document", "doc", "note"}, {"folder", "directory", "dir", "repo", "repository"}
_FUNCTION = {"the", "a", "an", "my", "your", "our", "their", "his", "her", "its", "this", "that", "these", "those",
             "it", "them", "they", "me", "i", "you", "we", "us", "he", "she", "and", "or", "also", "too", "please",
             "now", "then", "just", "entirely", "completely", "fully", "for", "with", "so", "yes", "ok", "okay", "can",
             "could", "would", "will", "should", "may", "might", "must", "go", "ahead", "both", "two", "three",
             "other", "another", "rest", "same", "is", "are", "was", "were", "be", "been", "as", "well", "pls", "do",
             "if", "there", "here", "out", "up", "away", "off", "over", "again", "right", "want", "need", "like",
             "let", "lets", "feel", "free", "sure", "go", "already", "anyway", "actually", "kindly", "able", "by"}
# Words too common to name something on their own: a name made only of these must appear exactly as written.
_GENERIC = _TYPE | {"test", "tests", "temp", "tmp", "data", "doc", "docs", "new", "old", "backup", "backups", "misc",
                    "output", "out", "log", "logs", "cache", "script", "scripts", "src", "lib", "build", "dist",
                    "config", "asset", "assets", "image", "images", "img", "readme", "main", "master", "index", "app",
                    "final", "draft", "untitled", "desktop", "download", "downloads", "user", "users", "home",
                    "inbox", "archive", "archives", "md", "txt", "json", "py", "js", "bin", "obj", "report",
                    "reports", "work", "general", "default", "local", "shared", "public", "private", "common",
                    "utils", "util", "tools", "tool", "music", "videos", "pictures", "photos", "result", "results",
                    "www", "com", "org", "net", "io", "api", "gmail", "outlook", "mail"}
OWNER_NEVER = {"guard", "secret", "money", "screen", "web", ""}


@dataclass
class _Act:
    """One thing a tool call does that the owner's words must cover."""
    kind: str  # destroy | outbound | system | free | never
    families: tuple = ()
    targets: list = None  # [{"name", "parents", "dir", "wild", "owner", "person", "label"}]
    what: str = ""
    verbs: tuple = ()  # only these verbs of the families count (restart the PC)
    words: tuple = ()  # the message must also contain these words (force push: "force")
    implicit: bool = False  # nothing to name (restart the PC)
    free: str = ""  # covered without the naming test, with this explanation


def _fold(t):
    """Simple plurals: notes -> note, repositories -> repository, boxes -> box (both sides fold the same way)."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("xes", "ches", "shes", "sses")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _lev1(a, b):
    """Edit distance of at most one (one letter changed, added or dropped)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _words(text):
    """Lower-case word tokens of a name or a message: CamelCase split, apostrophes dropped (don't -> dont),
    hyphens, underscores, dots and slashes separate words."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(text or ""))
    text = re.sub(r"(\w)['\u2019](\w)", r"\1\2", text.lower())
    return re.findall(r"[a-z0-9]+", text)


def _tok_eq(t, m):
    ft, fm = _fold(t), _fold(m)
    return ft == fm or (len(t) >= 5 and len(m) >= 5 and _lev1(ft, fm))


def _match(name, span, person=False, is_dir=None):
    """Words of `span` (a list of message words) that name `name` (a file or folder name without its extension, a
    repository, a recipient), or None. The threshold: every distinctive word of the name when it has one or two,
    otherwise 60% of them (rounded up); a person needs one. Words are equal after folding simple plurals, or one
    letter apart when both have five letters or more (speech recognition slips: Marlin / Marlen); words written
    together or apart count the same (SimBench / sim bench). A name made only of common words (tests, notes,
    Downloads) must appear exactly as written, and a "file" / "folder" next to it must fit what it is."""
    nw = [w for w in _words(name) if w not in _FUNCTION or w in _GENERIC]
    if not nw or not span:
        return None
    words = [w for w in span if w != ","]
    distinct = [w for w in nw if w not in _GENERIC and not w.isdigit() and len(w) >= 2]
    if not distinct:
        n = len(nw)
        for i in range(len(words) - n + 1):
            if words[i:i + n] == nw:
                near = set(words[max(0, i - 1):i] + words[i + n:i + n + 1])
                if is_dir is True and near & _FILE_WORDS or is_dir is False and near & _DIR_WORDS:
                    continue
                return " ".join(nw)
        return None
    windows = {}
    for size in (1, 2, 3):
        for i in range(len(words) - size + 1):
            windows.setdefault("".join(_fold(w) for w in words[i:i + size]), " ".join(words[i:i + size]))
    whole = "".join(_fold(w) for w in nw if not w.isdigit())
    if whole in windows and len(whole) >= 4:
        return windows[whole]
    hit = []
    for t in distinct:
        m = next((w for w in words if _tok_eq(t, w)), None) or windows.get(_fold(t))
        if m:
            hit.append(m)
    need = 1 if person else (len(distinct) if len(distinct) <= 2 else -(-len(distinct) * 3 // 5))
    return " ".join(dict.fromkeys(hit)) if len(hit) >= need else None


@dataclass
class _Occ:
    family: str
    verb: str
    objects: list
    locations: list
    allish: bool


def _sentences(text):
    """[(words, is_question)] per sentence; words keep "," and carry a quoted marker as '"'."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text or ""))
    out = []
    for part in re.split(r"(?<=[.!?;])\s+|\n+", text):
        part = part.strip()
        if not part:
            continue
        low = re.sub(r"(\w)['\u2019](\w)", r"\1\2", part.lower())
        low = re.sub(r"(\w)'s\b", r"\1s", low)
        toks = re.findall(r"[a-z0-9]+|,|[\"\u201c\u201d]", low)
        out.append((toks, part.rstrip().endswith("?")))
    return out


def _content(ws):
    return [w for w in ws if w not in (",", '"') and w not in _FUNCTION | _TYPE | _LOC | _ALLISH | _NEG]


def _instructions(message):
    """Parse the owner's message: live instruction verbs with what each names, and what was excluded."""
    live, excluded, every = [], {}, []
    prev_words = []
    for toks, question in _sentences(message):
        quoted, words, qflags = False, [], []
        for t in toks:
            if t in ('"', "\u201c", "\u201d"):
                quoted = not quoted
                continue
            words.append(t)
            qflags.append(quoted)
        every += words
        plain = [w for w in words if w != ","]
        hypothetical = (question and plain[:1] and plain[0] in _QUESTION) or tuple(plain[:2]) in _HYPOTHETICAL
        occs, i = [], 0  # (start, end, family, verb)
        while i < len(words):
            for phrase, fam in _PHRASES:
                if tuple(words[i:i + len(phrase)]) == phrase:
                    occs.append((i, i + len(phrase), fam, " ".join(phrase)))
                    i += len(phrase) - 1
                    break
            i += 1
        stops = {s for s, _e, _f, _v in occs}
        stops |= {j for j, w in enumerate(words) if w in _PROTECT or w in _EXCEPT}
        negated_at = {}
        for j, w in enumerate(words):
            if w in _NEG:
                nxt = next((o for o in occs if j < o[0] <= j + 3 and "," not in words[j:o[0]]), None)
                if nxt:
                    negated_at[nxt[0]] = True
        for j, w in enumerate(words):  # keep X / leave X / except X: excluded for every family
            if w in _PROTECT or w in _EXCEPT or tuple(words[j:j + 2]) in _EXCEPT2:
                end = min([s for s in stops if s > j] + [len(words)])
                span = [x for x in words[j + 1:end] if x not in _EXCEPT | {"not"}]
                if span:
                    excluded.setdefault("*", []).append(span)
        for s, e, fam, verb in occs:
            before = words[s - 1] if s else ""
            after = words[e] if e < len(words) else ""
            noun = (before in _DETERMINERS and tuple(verb.split()) not in _NOUN_OK) or after in _REPORT
            reported = any(w in _REPORT - {"is", "was", "has", "from"} and (k == 0 or words[k - 1] != "i")
                           for k, w in enumerate(words[:s]))
            end = min([x for x in stops if x > s] + [len(words)])
            span = words[e:end]
            if negated_at.get(s):
                excluded.setdefault(fam, []).append(span)
                if fam == "delete":
                    excluded.setdefault("clean", []).append(span)
                continue
            if noun or reported or hypothetical or qflags[s]:
                continue
            objects, locations, in_loc = [], [], False
            for w in span:
                if w in _LOC:
                    in_loc = True
                elif w != ",":
                    (locations if in_loc else objects).append(w)
            allish = bool(set(span) & _ALLISH)
            if not _content(objects) and not allish:  # "delete that repo", "combine the two": what was said before
                ante = words[:s]
                if not _content(ante):
                    ante = prev_words + [","] + ante
                in_loc = False
                for w in ante:
                    if w in _LOC:
                        in_loc = True
                    elif w == ",":
                        in_loc = False
                    elif w not in _NEG:
                        (locations if in_loc else objects).append(w)
            live.append(_Occ(fam, verb, objects, locations, allish))
        prev_words = words
    return live, excluded, every


def _own_github(settings):
    s = settings or Settings.load()
    out = {str(r).split("/")[0].lower() for r in (getattr(s, "github_repos", None) or []) if "/" in str(r)}
    try:
        raw = json.loads((Path(data_path(".")) / "config.json").read_text(encoding="utf-8")).get("github_login")
        out |= {str(x).lower() for x in ([raw] if isinstance(raw, str) else raw or [])}
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return out


def covers(message, act, settings=None):
    """THE covering rule (no model call): (covered, the words that matched or why not).

    1. The message holds a live instruction of the act's kind: a verb of one of act.families in its plain form,
       not after "don't / never / no / not" (within three words), not reported ("the email says delete ..."), not
       inside quotes, not in a question ("should I delete ...?"), not used as a noun ("the delete button").
    2. Each target was named in what that verb applies to: the words after it up to the next verb (or, for "it /
       that repo / the two", the words before it), by _match. Or it lies inside a folder named as the object
       ("delete the build folder"), or inside a named place when everything there was meant ("delete everything in
       Downloads", "clean up my downloads"). "clean up X" reaches what is inside X, never X itself.
    3. Nothing named after "except / but / keep / leave", or after a negated verb of the same kind, is covered.
    4. A repository of another GitHub account is covered only when that account was named too."""
    if act.free:
        return (bool(str(message or "").strip()), act.free)
    live, excluded, every = _instructions(message)
    occ = [o for o in live if o.family in act.families and (not act.verbs or o.verb in act.verbs)]
    if not occ:
        return False, f"you did not ask to {(act.verbs or act.families or ('do',))[0]} anything"
    if act.words and not all(any(_tok_eq(w, x) for x in every) for w in act.words):
        return False, "you did not say " + " ".join(act.words)
    if not act.targets:
        return (True, occ[0].verb) if act.implicit else (False, "it names nothing Waggle can match to your words")
    out = []
    for t in act.targets:
        hit = None
        for o in occ:
            hit = _named(t, o, excluded, every, settings)
            if hit:
                out.append(f"{o.verb} {hit}")
                break
        if not hit:
            return False, f"you did not name {t.get('label') or t.get('name')}"
    return True, "; ".join(dict.fromkeys(out))


def _named(t, o, excluded, every, settings):
    """What in occurrence `o` names target `t` (see covers), or None."""
    ex = excluded.get(o.family, []) + excluded.get("*", [])
    names = [t.get("name") or ""] + list(t.get("parents") or [])
    if any(_match(n, span, t.get("person")) for n in names if n for span in ex):
        return None
    owner = (t.get("owner") or "").lower()
    if owner and owner not in _own_github(settings) and not _match(owner, every):
        return None
    if o.family != "clean" and not t.get("wild"):
        span = o.objects + o.locations if t.get("place") else o.objects  # "open a PR on X", "post it to X"
        m = _match(t.get("name"), span, t.get("person"), t.get("dir"))
        if m:
            return m
    for p in t.get("parents") or []:
        m = _match(p, o.objects, is_dir=True)
        if not m and (o.allish or o.family == "clean"):
            m = _match(p, o.locations, is_dir=True)
        if m:
            return f"inside {m}"
    return None


# ---- what a tool call acts on

def _path_target(raw, cwd, is_dir=None):
    raw = str(raw or "").strip()
    full = guard._lex(guard.expand(raw), cwd or os.getcwd(), lower=False)
    parts = [x for x in full.split("/") if x]
    leaf = parts[-1] if parts else full
    wild = any(c in leaf for c in "*?[")
    if is_dir is None:
        is_dir = raw.endswith(("/", "\\")) or (not wild and os.path.isdir(full))
    home = [x for x in guard._lex(guard.USER_HOME, "/", lower=False).split("/") if x]
    if [x.lower() for x in parts[:len(home)]] == [x.lower() for x in home]:
        parents = parts[len(home):-1]
    else:
        parents = [x for x in parts[1:-1] if x.lower() not in ("users", "windows", "program files",
                                                              "program files (x86)", "programdata")]
    stem = leaf if is_dir or "." not in leaf.strip(".") else leaf.rsplit(".", 1)[0]
    return {"name": "" if wild else stem, "parents": list(reversed(parents)), "dir": is_dir, "wild": wild,
            "label": leaf, "path": full}


def _name_target(name, person=False, owner=""):
    name = str(name or "").strip()
    return {"name": name, "parents": [], "dir": None, "wild": False, "person": person, "owner": owner,
            "label": name}


def _person_target(addr):
    a = str(addr or "").strip()
    local = a.split("@")[0] if "@" in a else a
    return _name_target(re.sub(r"[\d+]+", " ", local), person=True) | {"label": a}


def _operands(rest, valued=(), slash_flags=False):
    """Arguments that are not options. slash_flags: cmd's /s /q /a:h style options too (del, rd)."""
    out, i, ends = [], 0, False
    while i < len(rest):
        t = rest[i]
        if not ends and t == "--":
            ends = True
        elif not ends and t.lower() in valued:
            i += 1
        elif ends or not (t.startswith("-") or (slash_flags and re.match(r"^/[a-z](:\S*)?$", t, re.I))):
            out.append(t)
        i += 1
    return out


def _flag_value(rest, *names):
    low = [t.lower() for t in rest]
    for n in names:
        if n in low and low.index(n) + 1 < len(rest):
            return rest[low.index(n) + 1]
        for t in rest:
            if t.lower().startswith(n + "="):
                return t.split("=", 1)[1]
    return None


_DELETE_VERBS = {"rm", "del", "erase", "rmdir", "rd", "ri", "remove-item", "unlink", "trash", "rimraf"}


def _shell_act(verb, rest, why, cwd, stmt):
    if why == "deleting files":
        if verb not in _DELETE_VERBS:
            return _Act("destroy", ("delete", "clean"), [], "deleting files")
        ops = _operands(rest, ("-filter", "-include", "-exclude"), slash_flags=verb in ("del", "erase", "rd", "rmdir"))
        return _Act("destroy", ("delete", "clean"), [_path_target(o, cwd) for o in ops], "deleting files")
    if why == "a git command that throws away work":
        sub = guard._git_sub(rest)
        repo = _path_target((guard._git_dirs(rest) or [cwd])[0], cwd, is_dir=True)
        repo["parents"] = []
        if sub == "push":
            return _Act("destroy", ("git",), [repo], "force-pushing over the remote's history", words=("force",))
        if sub == "branch":
            ops = [o for o in _operands(rest) if o.lower() != "branch"]
            return _Act("destroy", ("delete",), [_name_target(o) for o in ops[:1]], "deleting a git branch")
        return _Act("destroy", ("delete",), [repo], "throwing away uncommitted work")
    if why == "restarting or shutting down the PC":
        return _Act("system", ("run",), [], "restarting or shutting down the PC", implicit=True,
                    verbs=("restart", "reboot", "shut down", "shutdown", "log off"))
    if why == "changing the registry":
        key = _flag_value(rest, "-path", "-literalpath") or next(iter(_operands(rest)[1:2]), "")
        bits = [b for b in re.split(r"[\\/]+", key) if b]
        t = _name_target(bits[-1] if bits else "") | {"parents": list(reversed(bits[1:-1]))}
        return _Act("system", ("edit", "delete"), [t] if bits else [], "changing the registry")
    if why == "changing system settings":
        name = _flag_value(rest, "/tn", "-taskname") or ("execution policy" if verb == "set-executionpolicy" else "")
        return _Act("system", ("edit", "run", "delete"), [_name_target(name)] if name else [],
                    "changing a system setting")
    if why == "a system-wide install":
        ops = [o for o in _operands(rest, ("--source", "-s", "--scope")) if o.lower() not in
               ("install", "uninstall", "upgrade", "i")]
        return _Act("system", ("run",), [_name_target(o) for o in ops], "a system-wide install",
                    verbs=("install", "uninstall", "upgrade"))
    if why == "posting to GitHub as you":
        ops = _operands(rest, ("-r", "--repo", "-t", "--title", "-b", "--body", "-x", "--method", "-f", "-F",
                               "--field", "--raw-field", "-l", "--label", "-a", "--assignee", "--base", "--head"))
        noun, action = (ops + ["", ""])[:2]
        repo = _flag_value(rest, "-r", "--repo") or (ops[2] if noun == "repo" and len(ops) > 2 else "")
        if verb != "gh" or noun == "api":
            return _Act("outbound", ("send",), [], "posting to GitHub as you")
        if repo:
            owner, _, name = repo.rpartition("/")
            target = _name_target(name, owner=owner)
        else:
            target = _name_target(Path(str(cwd or os.getcwd())).name)
        if noun == "repo" and action == "delete":
            return _Act("destroy", ("delete",), [target], "deleting a GitHub repository")
        return _Act("outbound", ("send", "git", "edit", "run"), [target | {"place": True}], "posting to GitHub as you")
    if why == "sending data to a website":
        m = re.search(r"https?://([^/\s\"']+)", stmt)
        host = [b for b in (m.group(1).split(":")[0].split(".") if m else []) if b not in _GENERIC]
        return _Act("outbound", ("send",), [_name_target(host[-1]) | {"place": True}] if host else [],
                    "sending data to a website")
    if why == "sending a message as you":
        return _Act("outbound", ("send",), [], "sending a message as you")
    if why == "changing file permissions":
        ops = [o for o in _operands(rest) if not o.startswith("/")]
        path = _flag_value(rest, "/f") or (ops[0] if ops else "")
        return _Act("system", ("edit",), [_path_target(path, cwd)] if path else [], "changing file permissions")
    return _Act("never")


_SECRETISH = re.compile(next(p for p, why in ASK_SHELL if why == "touching a secrets file"), re.I)


# ---- simple variables inside one command (`$p = "C:\...\x.txt"; Remove-Item $p`) ------------------------------

_PS_ASSIGN = re.compile(r"^\$([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", re.S)
_SH_ASSIGN = re.compile(r"^([A-Za-z_]\w*)=(\S.*)?$", re.S)
# Ways to set a variable other than one plain assignment. Command words only count as words (not inside a path such
# as AppData\Local), so they are matched where a command can start or as a whole word between spaces/quotes/(.
_W = r"(?<![\w/\\.:-])"
_VAR_TRICKS = re.compile(rf"(?i){_W}(set-variable|new-variable|get-variable|clear-variable|remove-variable|sv|nv|"
                         rf"declare|typeset|local|export|readonly|eval|invoke-expression|iex|read|mapfile|"
                         rf"readarray|source)(?![\w/\\.:-])|\s-(outvariable|ov|errorvariable|ev|pipelinevariable|pv|"
                         rf"warningvariable|wv|informationvariable|iv)\b|\[ref\]|variable:|"
                         rf"(?<![\w/\\])\.\s+\S")  # `. script.ps1` / `. file.sh` runs code in this scope
_ARG = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s"\']+')


def _known_folder(name):
    """[Environment]::GetFolderPath('Desktop') and friends, from the user's shell folders (OneDrive moves them)."""
    reg = {"desktop": "Desktop", "desktopdirectory": "Desktop", "mydocuments": "Personal", "personal": "Personal",
           "mypictures": "My Pictures", "mymusic": "My Music", "myvideos": "My Video"}.get(name.lower())
    if not reg:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            v = winreg.QueryValueEx(k, reg)[0]
        return guard.expand(str(v))
    except Exception:
        return None


def _subst(text, known):
    """Replace $name / ${name} (not $env:) of variables assigned once in this command."""
    def rep(m):
        name = (m.group(1) or m.group(2) or "").lower()
        return known.get(name, m.group(0))
    return re.sub(r"\$\{([A-Za-z_]\w*)\}|\$(?!env:)([A-Za-z_]\w*)", rep, text, flags=re.I)


def _value_of(expr, known, depth=0):
    """The literal value of a simple right-hand side, or None when it is anything else."""
    expr = expr.strip().rstrip(";").strip()
    if not expr or depth > 3:
        return None
    m = re.fullmatch(r'"([^"`$]*(?:\$(?:env:)?\{?[A-Za-z_]\w*\}?[^"`$]*)*)"', expr, re.I)
    if m:
        return _subst(m.group(1), known)
    m = re.fullmatch(r"'([^']*)'", expr)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\[(?:System\.)?Environment\]::GetFolderPath\(\s*(?:\[[\w.]+\]::)?['\"]?(\w+)['\"]?\s*\)",
                     expr, re.I)
    if m:
        return _known_folder(m.group(1))
    m = re.fullmatch(r"(?i)join-path\s+(.+)", expr, re.S)
    if m:
        args = [a for a in _ARG.findall(m.group(1)) if a.lower() not in ("-path", "-childpath")]
        if len(args) == 2:
            a, b = (_value_of(x, known, depth + 1) for x in args)
            if a and b:
                return a.rstrip("/\\") + "/" + b.lstrip("/\\")
        return None
    if re.fullmatch(r"[^\s|;&()`\"'{}@,]+", expr):  # a bare word: $env:USERPROFILE\Desktop\x.txt, $d\x.txt
        v = _subst(expr, known)
        return None if re.search(r"\$(?!env:)", v, re.I) else v
    return None


def _assigned_once(command, name):
    """The command assigns `name` exactly once, by a plain statement (no loops, read, Set-Variable, ...)."""
    n = re.escape(name)
    ps = len(re.findall(rf"\$(?:\w+:)?{n}\s*(?:[-+*/%]|\?\?)?=(?!=)", command, re.I))
    sh = len(re.findall(rf"(?<![\w$.-]){n}\+?=(?!=)", command))
    loops = re.search(rf"(?i)\bforeach\s*\(\s*\${n}\b|\bfor\s+{n}\b|\bselect\s+{n}\b", command)
    return ps + sh == 1 and not loops


def _with_variables(stmts, command):
    """The statements with simple variables replaced by their values (see _value_of); a statement that only
    assigns stays as it is. Anything unusual about how a variable is set leaves it unreplaced, so it still asks."""
    if _VAR_TRICKS.search(command):
        return stmts
    known, out = {}, []
    for st in stmts:
        st0 = st.strip()
        m = _PS_ASSIGN.match(st0)
        sh = None if m else _SH_ASSIGN.match(st0)
        if m or sh:
            name, rhs = (m.group(1), m.group(2)) if m else (sh.group(1), sh.group(2) or "")
            v = _value_of(rhs, known) if _assigned_once(command, name) else None
            if v is not None:
                known[name.lower()] = v
            else:
                known.pop(name.lower(), None)
            out.append(st)
            continue
        out.append(_subst(st, known) if known else st)
    return out


def _shell_acts(tool, command, cwd):
    """One _Act per statement that asks; None when a statement is refused outright."""
    name = (tool or "").lower().split("__")[-1]
    acts = []
    for stmts, _dialect in guard._parse(command, guard.SHELL_DIALECTS.get(name, guard.ALL_DIALECTS)):
        here = cwd or os.getcwd()
        for st in _with_variables(stmts, command):
            st = st.strip()  # the ask patterns anchor at the start of a statement
            action, why = classify_shell(st)
            ctoks, _read = guard._command_tokens(guard._tokens(st))
            verb, rest = guard._verb_info(ctoks)
            if verb in guard.NAV_VERBS and rest and not rest[0].startswith("-"):
                here = guard._lex(guard.expand(rest[0]), here, lower=False)
            if action == DENY:
                return None
            if action == ASK and (_SECRETISH.search(st) or has_secret(st)
                                  or any(is_secret_path(o) for o in _operands(rest))):
                acts.append(_Act("never"))  # secrets always need the user, whatever else the statement does
            elif action == ASK:
                acts.append(_shell_act(verb, rest, why, here, st))
    if not acts and classify_shell(command)[0] == ASK:
        acts.append(_Act("never"))  # it asks as a whole, but no single statement does: no telling what it names
    return acts


_NAME_KEYS = ("name", "title", "path", "file", "file_path", "filepath", "folder", "repo", "repository", "project",
              "routine", "routine_id", "skill", "entity", "target", "label", "subject", "id", "query")


def _arg_targets(args):
    out = []
    for k in _NAME_KEYS:
        v = (args or {}).get(k)
        if isinstance(v, str) and v.strip():
            out.append(_path_target(v, None) if re.search(r"[\\/]", v) and k in ("path", "file", "file_path",
                                                                                  "filepath", "folder")
                       else _name_target(v))
    return out[:1]  # the first identifying argument is what the call acts on


def _emails(args):
    return re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", json.dumps(args or {}, default=str))


def owner_acts(tool_name, args, base, settings=None, cwd=None):
    """What a tool call that asked (base) acts on, as _Acts for covers(); [] or None when nothing can be covered."""
    name = (tool_name or "").lower()
    b = name.split("__")[-1]
    kind = base.kind
    if kind == "shell" or (kind == "unknown" and b in _OTHER_SHELL):  # Hermes's terminal: the same shell rules
        command = str(args.get("command") or args.get("cmd") or "")
        acts = _shell_acts(name, command, args.get("cwd") or args.get("workdir") or cwd)
        if acts == [] and kind == "unknown":
            return [_Act("free", free="the same shell rules as Claude's own shell")]
        return acts
    if kind == "outside":
        path = str(args.get("file_path") or args.get("notebook_path") or args.get("path") or "")
        t = _path_target(path, cwd)
        if _system_location(t["path"]):
            return [_Act("system", ("edit", "move"), [t], "changing a system file")]
        return [_Act("free", free="your own files (full access under your profile)")]
    if name.startswith("mcp__waggle__"):
        if b == "background_task":
            return [_Act("free", free="your request (the job itself runs under the usual rules)")]
        if b == "graph_correct":
            return [_Act("destroy", ("delete", "edit"), _arg_targets(args), "changing what Waggle knows about you")]
        if b == "routine_remove":
            return [_Act("destroy", ("delete",), _arg_targets(args), "deleting a routine")]
        if b == "settings":  # comfort settings only (waggle/comfort.py allowlist); the tool refuses the rest itself
            return [_Act("free", free="your own comfort settings (voice, speed, quiet hours)")]
        if b == "file_proposal":  # only queues a proposal (waggle/proposals.py); nothing merges without the owner's reply
            return [_Act("free", free="your request to fix Waggle's own code")]
        if b == "skill_install":
            return [_Act("system", ("run",), _arg_targets(args), "installing a community skill", verbs=("install",))]
        return [_Act("never")]
    if kind == "outbound":
        who = _recipients(args) + [str(args.get(k)) for k in ("chat", "channel", "user", "username", "chat_id")
                                   if isinstance(args.get(k), str)]
        return [_Act("outbound", ("send",), [_person_target(w) for w in who], f"{b.replace('_', ' ')} to "
                     + (", ".join(who) or "someone"))]
    if kind in ("invite", "share"):
        who = _emails(args)
        return [_Act("outbound", ("send", "edit"), [_person_target(w) for w in who],
                     "inviting " if kind == "invite" else "sharing with " + (", ".join(who) or "someone"))]
    if kind == "destroy":
        return [_Act("destroy", ("delete", "clean"), _arg_targets(args), f"{b.replace('_', ' ')}")]
    if kind == "system":
        what = re.sub(r"^(set|manage|setup|close|desktop)_", "", b).replace("_", " ")
        return [_Act("system", ("edit", "run"), [_name_target(what)], f"{b.replace('_', ' ')}")]
    return [_Act("never")]


# Inside the profile but run at start-up or by every shell, git or Hermes start: treated like system locations.
_STARTUP = re.compile(r"^/(ntuser\.|appdata/roaming/microsoft/windows/start menu/|"
                      r"\.(bashrc|bash_profile|bash_login|profile|zshrc|zprofile|gitconfig)$|\.config/git/|"
                      r"documents/(windows)?powershell/[^/]*profile[^/]*\.ps1$|\.hermes/(plugins/|config\.ya?ml$))")


def _system_location(path):
    """Outside the user's profile (Windows, Program Files, other users' folders), or a start-up entry inside it."""
    p = guard._lex(path, "/")
    home = guard._lex(guard.USER_HOME, "/")
    if not (p == home or p.startswith(home + "/")):
        return True
    return bool(_STARTUP.search(p[len(home):]))


def _said(text, limit=160):
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit - 1] + "\u2026"


def owner_rule(tool_name, args, base, turn, settings=None, cwd=None):
    """In the owner's own turn: ALLOW ("owner instruction: <matched words>") when their message covers every part of
    an action that asked; an ASK reworded for the owner (a question about what was heard, or "beyond what you
    asked") when it does not; None when the rule does not apply (not the owner's turn, rule switched off, or an ask
    that always stays: the guard, secrets, money, screen, payment/login pages, unknown tools)."""
    if not (isinstance(turn, dict) and turn.get("owner")) or base.kind in OWNER_NEVER:
        return None
    if not guard.owner_rule_enabled():
        return None
    if (tool_name or "").lower() == "mcp__waggle__hermes_tool":
        tool_name, args = str(args.get("name", "")), args.get("args") or {}
    try:
        acts = owner_acts(tool_name, args if isinstance(args, dict) else {}, base, settings, cwd)
        if not acts or any(a.kind == "never" for a in acts):
            return None
        text = str(turn.get("text") or "")
        out = []
        for a in acts:
            if a.kind in ("destroy", "outbound") and not turn.get("confident", True):
                return Decision(ASK, f'Did you say: "{_said(text)}"? Speech recognition was not sure, and this is '
                                     f"{a.what}", kind="hearing")
            ok, why = covers(text, a, settings)
            if not ok:
                hidden = [t.get("label") for t in (a.targets or []) if "$" in str(t.get("label") or "")
                          or "$" in str(t.get("path") or "")]
                if hidden and a.kind in ("destroy", "outbound"):  # not a permission question: name it plainly
                    return Decision(ASK, f"{a.what}: the command names {hidden[0]} through a variable, so it can't "
                                         f"be checked against the owner's words", kind="unresolved")
                if a.kind in ("destroy", "outbound"):
                    return Decision(ASK, f"{a.what}: this goes beyond what you asked ({why})", kind="beyond")
                return Decision(ASK, f"{base.reason} ({why})", kind=base.kind)
            out.append(why)
    except Exception:  # a bug here must never allow: today's ask stands
        return None
    return Decision(ALLOW, "owner instruction: " + "; ".join(dict.fromkeys(out)), kind="owner")


# ---- approvals + audit -------------------------------------------------------------------------

APPROVAL_TTL = 30 * 60
BACKGROUND_TTL = 24 * 3600  # objectives/routines may wait overnight for the user's yes
CODE_CHARS = "ACDEFHJKMNPRTVWXY34679"  # no 0/O, 1/I/L, 2/Z, 5/S, 8/B look-alikes


def _ttl(item):
    return item.get("ttl") or APPROVAL_TTL


def _fingerprint(tool, args):
    body = json.dumps(_key_args(tool, args), sort_keys=True, default=str)
    return hashlib.sha256(f"{(tool or '').lower()}|{body}".encode()).hexdigest()[:16]


def _approval_id(tool, args, session):
    """One approval = one exact call in one session: a yes for objective A never unlocks the same call elsewhere."""
    return hashlib.sha256(f"{_fingerprint(tool, args)}|{session or ''}".encode()).hexdigest()[:16]


def _load(name):
    p = data_path(name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _save(name, data):
    data_path(name).write_text(json.dumps(data, indent=1), encoding="utf-8")


def _adb():
    """Approvals live in SQLite: the agent hook, the gateway and objective workers are separate processes, and a
    JSON file rewritten by each of them could lose a request or a grant (last writer wins)."""
    import sqlite3
    con = sqlite3.connect(data_path("approvals.db"), timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, code TEXT, tool TEXT, summary TEXT, "
                "reason TEXT, session TEXT, created REAL, ttl REAL, state TEXT, decided_at REAL, used_at REAL)")
    old = data_path("approvals.json")
    if old.exists():  # one-time import of the old store (pending, unexpired items only)
        now = time.time()
        for k, v in _load("approvals.json").items():
            if not v.get("granted") and now - v.get("created", 0) < _ttl(v):
                con.execute("INSERT OR IGNORE INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (k, _new_code(con), v.get("tool"), v.get("summary"), v.get("reason"), v.get("session", ""),
                             v.get("created", now), _ttl(v), "pending", None, None))
        try:
            old.replace(old.with_suffix(".json.migrated"))
        except OSError:
            pass
    return con


def _new_code(con):
    import secrets
    taken = {r[0] for r in con.execute("SELECT code FROM approvals WHERE state IN ('pending','granted')")}
    while True:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
        if code not in taken:
            return code


def _row(r):
    d = dict(r)
    d["granted"] = d["state"] in ("granted", "used")
    d["used"] = d["state"] == "used"
    d["expires"] = d["created"] + d["ttl"]
    return d


def request_approval(tool, args, reason, session=""):
    """Record a pending approval; returns its id. The same call asked again while still pending keeps its code."""
    aid = _approval_id(tool, args, session)
    now = time.time()
    ttl = BACKGROUND_TTL if str(session).startswith(("objective-", "routine-")) else APPROVAL_TTL
    con = _adb()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not (r and r["state"] == "pending" and now - r["created"] < r["ttl"]):
            con.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, _new_code(con), tool, _short(tool, args), reason, session or "", now, ttl, "pending",
                         None, None))
        con.execute("COMMIT")
    finally:
        con.close()
    return aid


def pending(session=None, since=0.0):
    """Unexpired approvals still waiting for the user (for one session, or all when session is None)."""
    now = time.time()
    con = _adb()
    try:
        rows = con.execute("SELECT * FROM approvals WHERE state='pending' AND created>=? ORDER BY created",
                           (since,)).fetchall()
    finally:
        con.close()
    return [dict(_row(r), id=r["id"]) for r in rows if now - r["created"] < r["ttl"]
            and (session is None or r["session"] in ("", session))]


def get(key):
    """One approval by id or its 4-letter code (any state)."""
    con = _adb()
    try:
        r = con.execute("SELECT * FROM approvals WHERE id=? OR code=? ORDER BY created DESC",
                        (key, str(key or "").upper())).fetchone()
    finally:
        con.close()
    return _row(r) if r else None


def _decide(ids, state):
    now = time.time()
    done = []
    con = _adb()
    try:
        for i in ids:
            r = con.execute("SELECT * FROM approvals WHERE id=?", (i,)).fetchone()
            if not r or r["state"] != "pending" or now - r["created"] >= r["ttl"]:
                continue  # expired, already decided or used: a yes can never revive it
            if con.execute("UPDATE approvals SET state=?, decided_at=? WHERE id=? AND state='pending'",
                           (state, now, i)).rowcount:
                done.append(dict(_row(r), state=state, id=i))
    finally:
        con.close()
    return done


def grant(approval_id=None, session=None, code=None):
    """Grant one pending approval (by id or code), or every pending one for this session when neither is given.
    Session-wide grants are only for the chat that was just asked (the agent's own approval message listed them)."""
    if approval_id or code:
        item = get(approval_id or code)
        return _decide([item["id"]] if item else [], "granted")
    if not session:
        return []  # never "grant everything everywhere"
    return _decide([p["id"] for p in pending(session) if p["session"] == session], "granted")


def deny(approval_id=None, code=None):
    item = get(approval_id or code)
    return _decide([item["id"]] if item else [], "denied")


def _consume(tool, args, session=""):
    """A granted approval allows exactly one matching call in the same session, then it is used up."""
    now = time.time()
    con = _adb()
    try:
        for aid in dict.fromkeys([_approval_id(tool, args, session), _approval_id(tool, args, "")]):
            r = con.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
            if r and r["state"] == "granted" and now - (r["decided_at"] or 0) < r["ttl"]:
                if con.execute("UPDATE approvals SET state='used', used_at=? WHERE id=? AND state='granted'",
                               (now, aid)).rowcount:
                    return True
    finally:
        con.close()
    return False


def describe(item):
    """One line the user can act on: what, why, which code, how long it stays open."""
    left = max(0, int((item["created"] + item["ttl"] - time.time()) / 60))
    when = f"{left // 60} h" if left >= 120 else f"{left} min"
    return f"[{item['code']}] {item['summary']} ({item['reason']}); expires in {when}"


def audit(tool, args, decision, source="agent"):
    row = {"time": time.time(), "source": source, "tool": tool, "decision": decision.action,
           "reason": decision.reason, "call": _short(tool, args)}
    with open(data_path("audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


_FROM_ENV = object()


def check(tool, args=None, session="", source="agent", settings=None, cwd=None, turn=_FROM_ENV):
    """Full check: policy, then one-time approvals; logs to the audit trail. Returns a Decision. turn: the running
    turn's state, read from this process's environment by default (guard.read_turn_state: None outside a chat
    turn, so background agents, objectives and every other caller keep today's rules)."""
    if turn is _FROM_ENV:
        turn = guard.read_turn_state()
    d = decide(tool, args, settings, cwd, turn=turn)
    if d.action == ASK:
        if _consume(tool, args, session):
            d = Decision(ALLOW, "approved by the user")
        else:
            d.approval_id = request_approval(tool, args, d.reason, session)
    audit(tool, args, d, source)
    return d
