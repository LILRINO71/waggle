"""One permission policy for every tool call Waggle's Claude agents make.

Enforced by a Claude Code PreToolUse hook (`waggle.agent_hook`), so a prompt injection cannot argue its
way past it. The user's choice (2026-11-18, "full auto in projects"):
- runs on its own: reading anywhere (except secret files), web search/fetch, file edits, scripts and
  package installs inside the project roots (Claude folder, vault, Waggle's own folders);
- needs the user's yes: money, passwords/secrets, messaging or posting as the user, deleting the user's
  data, security/system settings, writing outside the roots, system-wide installs;
- never: disabling security, piping downloads straight into a shell, reading credential stores.

"ask" cannot pop up a dialog in a headless run, so it becomes a one-time approval request: the call is
denied with an id, Waggle tells the user, and "yes" grants exactly that call once (stored by hash).
"""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, data_path

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass
class Decision:
    action: str
    reason: str = ""
    approval_id: str = ""

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
WAGGLE_SAFE = {"vault_search", "remember", "project_status", "project_update", "build_status", "waggle_status",
               "weather", "look_at_screen", "skill_search", "skill_load", "skill_list", "recall", "routine_list",
               "routine_add", "routine_pause", "routine_resume", "delegate", "agent_list", "jobs_list",
               "hermes_tools", "model_catalog", "start_build", "notify_me", "rate_finding", "skill_create",
               "skill_update", "skill_hub_search", "agent_status", "learning_report", "memory_status",
               "agent_note", "routine_add", "routine_list", "routine_pause", "routine_resume", "memory_review"}


def _short(tool, args):
    try:
        body = json.dumps(args, sort_keys=True, default=str)
    except TypeError:
        body = str(args)
    return redact(f"{tool} {body}")[:300]


def decide(tool_name, args=None, settings=None):
    """Decision for one tool call (before approvals are consulted)."""
    args = args or {}
    raw = tool_name or ""
    name = raw.lower()
    base = name.split("__")[-1]  # mcp__server__tool -> tool
    if name in READ_TOOLS:
        path = args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
        if path and is_secret_path(path):
            return Decision(ASK, f"reading a secrets file ({Path(str(path)).name})")
        return Decision(ALLOW)
    if name in WRITE_TOOLS:
        path = args.get("file_path") or args.get("notebook_path") or ""
        if is_secret_path(path):
            return Decision(ASK, f"changing a secrets file ({Path(str(path)).name})")
        content = str(args.get("content") or args.get("new_string") or "")
        if has_secret(content):
            return Decision(ASK, "writing what looks like a password or key into a file")
        if under(path, write_roots(settings)):
            return Decision(ALLOW)
        return Decision(ASK, f"changing a file outside your project folders ({path})")
    if name in SHELL_TOOLS:
        action, why = classify_shell(str(args.get("command") or args.get("cmd") or ""))
        return Decision(action, why)
    if name.startswith("mcp__waggle__"):
        if base == "hermes_tool":
            return decide(str(args.get("name", "")), args.get("args") or {}, settings)
        return Decision(ALLOW) if base in WAGGLE_SAFE else Decision(ASK, f"Waggle tool {base}")
    if name == "computer_use":
        from .security import screen_risk
        why = screen_risk(args)
        return Decision(ASK, f"controlling your screen ({why})") if why else Decision(ALLOW)
    if name in {"send_message", "send_email", "telegram_send", "discord_send", "sms_send"} or OUTWARD.search(base):
        if base == "create_draft":
            return Decision(ALLOW)
        return Decision(ASK, f"{base.replace('_', ' ')}: this reaches other people or the outside world")
    if DESTROY.search(base):
        return Decision(ASK, f"{base.replace('_', ' ')}: deletes or removes your data")
    if MONEY.search(base):
        return Decision(ASK, f"{base}: involves money")
    if name.startswith("mcp__claude_ai_") or name.startswith("mcp__"):
        attendees = args.get("attendees") or args.get("guests")
        if attendees:
            return Decision(ASK, "this would invite other people")
        if re.search(r"permission|sharing|acl", base):
            return Decision(ASK, "changes who can see your files")
        return Decision(ALLOW)
    if name in {"task", "agent"}:
        return Decision(ALLOW)
    # Hermes tools reached through the bridge use their plain names.
    if name in {"web_search", "web_extract", "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
                "browser_scroll", "browser_back", "browser_press", "browser_get_images", "browser_vision",
                "browser_console", "vision_analyze", "text_to_speech", "skills_list", "skill_view", "read_file",
                "search_files", "list_directory", "cronjob_list", "session_search", "clarify", "todo"}:
        return Decision(ALLOW)
    if name in {"write_file", "patch"}:
        return decide("write", {"file_path": args.get("path") or args.get("file_path") or "",
                                "content": args.get("content", "")}, settings)
    if name in {"cronjob", "cronjob_manage"}:
        return Decision(ALLOW)
    return Decision(ASK, f"an unfamiliar tool ({raw})")


# ---- approvals + audit -------------------------------------------------------------------------

APPROVAL_TTL = 30 * 60


def _fingerprint(tool, args):
    body = json.dumps(args or {}, sort_keys=True, default=str)
    return hashlib.sha256(f"{(tool or '').lower()}|{body}".encode()).hexdigest()[:16]


def _load(name):
    p = data_path(name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _save(name, data):
    data_path(name).write_text(json.dumps(data, indent=1), encoding="utf-8")


def request_approval(tool, args, reason, session=""):
    """Record a pending approval; returns its id."""
    fp = _fingerprint(tool, args)
    pend = _load("approvals.json")
    now = time.time()
    pend = {k: v for k, v in pend.items() if now - v.get("created", 0) < APPROVAL_TTL or v.get("granted")}
    pend[fp] = {"tool": tool, "summary": _short(tool, args), "reason": reason, "created": now,
                "session": session, "granted": False, "used": False}
    _save("approvals.json", pend)
    return fp


def pending(session=None, since=0.0):
    now = time.time()
    return [dict(v, id=k) for k, v in _load("approvals.json").items()
            if not v.get("granted") and now - v.get("created", 0) < APPROVAL_TTL and v.get("created", 0) >= since
            and (session is None or v.get("session") in ("", session))]


def grant(approval_id=None, session=None):
    """Grant one pending approval (or every pending one for this session when id is None)."""
    pend = _load("approvals.json")
    ids = [approval_id] if approval_id else [p["id"] for p in pending(session)]
    done = []
    for i in ids:
        if i in pend:
            pend[i]["granted"], pend[i]["granted_at"] = True, time.time()
            done.append(pend[i])
    _save("approvals.json", pend)
    return done


def _consume(tool, args):
    fp = _fingerprint(tool, args)
    pend = _load("approvals.json")
    item = pend.get(fp)
    if item and item.get("granted") and not item.get("used") and time.time() - item.get("granted_at", 0) < APPROVAL_TTL:
        item["used"] = True
        _save("approvals.json", pend)
        return True
    return False


def audit(tool, args, decision, source="agent"):
    row = {"time": time.time(), "source": source, "tool": tool, "decision": decision.action,
           "reason": decision.reason, "call": _short(tool, args)}
    with open(data_path("audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def check(tool, args=None, session="", source="agent", settings=None):
    """Full check: policy, then one-time approvals; logs to the audit trail. Returns a Decision."""
    d = decide(tool, args, settings)
    if d.action == ASK:
        if _consume(tool, args):
            d = Decision(ALLOW, "approved by the user")
        else:
            d.approval_id = request_approval(tool, args, d.reason, session)
    audit(tool, args, d, source)
    return d
