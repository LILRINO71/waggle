"""Tamper guard: keeps agents from rewriting the files that restrict them.

Three layers share this module's data:
- prevent: `policy.decide` asks `write_hit` / `shell_hit` whether a tool call touches the protected set and, if so,
  turns it into a one-time approval (the user's yes) instead of letting the write-roots rule allow it;
- fail closed: `agent_hook` hashes the files in HOOK_CHECKED against the manifest on every call;
- detect: `verify()` compares the whole protected set with the manifest (for the future core process), and
  `python -m waggle.guard status|verify|approve` is the user's command line.

The protected set is data (REPO_FILES, WAGGLE_HOME_PATHS, USER_HOME_PATHS, HOLDOUT_*). Add an entry there and it is
path-protected, hashed and verified with no other change. There is deliberately no environment variable that moves
the guard folder: an agent could set it. The user's home comes from the OS account (`profile_dir`), not from
USERPROFILE / HOME, for the same reason. Tests monkeypatch USER_HOME / GUARD_DIR / PACKAGE_ROOT instead.

Hashes are over a normalised form (CRLF -> LF for text files), so a checkout or merge that only changes line endings
(git autocrlf) does not lock every tool call. Python reads CRLF and LF source identically, so nothing that runs differs.

What this cannot do: an agent that may run its own code as the same Windows user (any allowed script) can rewrite a
protected file AND the manifest together, and the hook cannot tell. Only privilege separation (a manifest the user's
account cannot write) closes that; see tests/test_guard.py "open" tests.
"""
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config


def profile_dir():
    """The user's profile folder as the OS account records it (Windows: GetUserProfileDirectoryW on this process's
    token, which reads the registry, not USERPROFILE). None when it cannot be found."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            fn = ctypes.WinDLL("userenv").GetUserProfileDirectoryW
            fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            fn.restype = wintypes.BOOL
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if fn(wintypes.HANDLE(-4), buf, ctypes.byref(size)) and buf.value:  # -4 = GetCurrentProcessToken()
                return Path(buf.value)
            return None
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return None


USER_HOME = profile_dir() or Path.home()
GUARD_DIR = USER_HOME / ".waggle-guard"
# The checkout this code was imported from, so the live copy is protected wherever it lives.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "manifest.json"
VIOLATION_NAME = "violation.json"
# 3 = one manifest per checkout (~/.waggle-guard/<checkout key>/manifest.json), line-ending-normalised hashes,
# the checkout's resolved path and the Waggle data folder it was approved for.
MANIFEST_VERSION = 3

REPO_FILES = (
    "waggle/__init__.py",  # runs before policy on import, so it can patch policy
    "waggle/policy.py", "waggle/security.py", "waggle/agent_hook.py", "waggle/budget.py", "waggle/quota.py",
    "waggle/guard.py", "hermes_plugin/waggle/__init__.py", "tests/test_trust.py", "tests/test_guard.py",
    "waggle/config.py",  # imported inside the hook before the policy runs; decides the data folder and settings
    "waggle/agent.py",  # writes the hook command and the --settings every tool agent starts with
    "waggle/claude_cli.py",  # connector runs' --settings/--allowedTools and the permission mode of build sessions
    "waggle/bridge.py",  # gives Claude agents Hermes tools, with its own in-process policy.decide check
)
# What agent_hook re-hashes on every call (it carries its own copy of this tuple: it must not trust guard.py).
# Everything in REPO_FILES that runs, so a change to the files that start agents also stops the next tool call.
HOOK_CHECKED = ("waggle/__init__.py", "waggle/policy.py", "waggle/agent_hook.py", "waggle/guard.py",
                "waggle/security.py", "waggle/config.py", "waggle/budget.py", "waggle/quota.py", "waggle/agent.py",
                "waggle/claude_cli.py", "waggle/bridge.py", "hermes_plugin/waggle/__init__.py")
# Claude Code settings files anywhere: a project's .claude/settings.json can set "env" or "disableAllHooks" for the
# next run in that folder. Path-protected (asked about), never hashed (the user edits them).
CLAUDE_SETTINGS = re.compile(r"(^|/)\.claude/settings(\.local)?\.json$")

# Under the Waggle data folder (config.HOME and ~/.waggle). "prefix" also covers SQLite -wal / -journal / -shm.
# turns/ holds the engine's turn state (write_turn_state): what the owner said in the running turn. The engine writes
# it; an agent may not, because owner_instruction coverage is decided from it.
TURN_DIR = "turns"
WAGGLE_HOME_PATHS = (("config.json", "file"), ("approvals.db", "prefix"), ("budget.db", "prefix"), ("autonomy.db", "prefix"),
                     (TURN_DIR, "dir"))
HOLDOUT_DIR, HOLDOUT_PREFIX = "eval", "holdout"
# Under the user's home. install.py copies the Hermes plugin here, so this copy is the one that runs.
USER_HOME_PATHS = ((".waggle-guard", "dir"), (".hermes/plugins/waggle/__init__.py", "file"))
# Hashed but only some of these settings count: the budget/limit keys, plus the settings that decide where agents
# may write without asking and whose mail needs no approval (policy.write_roots, policy._only_to_self). Only keys the
# user sets belong here: Waggle's own code never writes any of them (Settings.save writes only what a caller changed;
# the window size Waggle learns lives in budget.db, bounded by quota.py). tests/test_guard.py runs every code path
# that writes config.json and checks the manifest stays clean.
CONFIG_KEYS = (
    "waggle_share", "stop_at", "window_units_estimate", "window_hours",
    "background_daily_units", "background_stop_at", "background_max_workers", "background_reset_hour",
    "background_downgrade_at", "interactive_yield_s", "max_verifications_per_day", "max_calls_per_day",
    "max_selfbuilds_per_day",
    "extra_write_roots", "vault", "vault_own", "jobs_dir", "self_emails",
    "owner_instructions",  # false turns the owner-instruction rule off (policy.owner_rule)
    "seven_day_stop_at",  # background work defers above this share of the weekly limit (quota/budget)
)
CONFIG_KEY = "home:config.json#keys"


# ---- paths -------------------------------------------------------------------------------------

_ENV_RE = re.compile(r"%([A-Za-z_]\w*)%|!([A-Za-z_]\w*)!|\$\{(?:env:)?([A-Za-z_]\w*)\}|\$env:([A-Za-z_]\w*)|"
                     r"\$([A-Za-z_]\w*)", re.I)


def _env(name):
    if name == "_":  # PowerShell's current pipeline item; Git Bash also exports "_" (last program run)
        return None
    if name.upper() in ("USERPROFILE", "HOME"):  # Windows has no $HOME, agents use it anyway
        return str(USER_HOME)
    return os.environ.get(name)


def expand(text):
    """%VAR%, !VAR!, $VAR, ${VAR}, $env:VAR; unknown names stay as written."""
    return _ENV_RE.sub(lambda m: _env(next(g for g in m.groups() if g)) or m.group(0), text)


def _lex(raw, cwd, lower=True):
    """Lexical absolute form: forward slashes, no '..', no trailing dots/spaces, no alternate data stream."""
    s = str(raw).strip().strip("\"'").strip()
    if s == "~" or s[:2] in ("~/", "~\\"):
        s = str(USER_HOME) + s[1:]
    s = s.replace("\\", "/")
    if s.startswith(("//?/", "//./")):
        s = s[4:]
        if s[:4].lower() == "unc/":
            s = "//" + s[4:]
    m = re.match(r"^//[^/]+/([A-Za-z])\$(/.*)?$", s)  # \\localhost\c$\... is the same file
    if m:
        s = f"{m.group(1)}:{m.group(2) or '/'}"
    else:
        m = re.match(r"^/(?:mnt/)?([A-Za-z])(/.*)?$", s)  # MSYS / WSL drive paths
        if m:
            s = f"{m.group(1)}:{m.group(2) or '/'}"
    unc = s.startswith("//")
    s = re.sub(r"/{2,}", "/", s)
    if unc:
        s = "/" + s
    drive = re.match(r"^([A-Za-z]):(?!/)", s)
    if drive:
        s = f"{drive.group(1)}:/{s[2:]}"
    if not (re.match(r"^[A-Za-z]:/", s) or s.startswith("/")):
        s = str(cwd).replace("\\", "/").rstrip("/") + "/" + s
    elif s.startswith("/") and not unc and re.match(r"^[A-Za-z]:", str(cwd).replace("\\", "/")):
        s = str(cwd)[:2] + s
    parts = []
    for i, seg in enumerate(s.split("/")):
        if seg == "..":
            if len(parts) > 1:
                parts.pop()
            continue
        if seg == "." or seg == "":
            if seg == "" and i == 0:
                parts.append("")
            continue
        if i > 0 and ":" in seg:
            seg = seg.split(":")[0]
        seg = seg.rstrip(" .")
        if seg:
            parts.append(seg)
    out = "/".join(parts)
    return out.lower() if lower else out


class Entry:
    def __init__(self, kind, path, label):
        self.kind, self.label, self.path = kind, label, Path(path)
        self.forms = sorted({_lex(path, "/"), _lex(_real(path), "/")})
        self.parents = sorted({f.rsplit("/", 1)[0] for f in self.forms})
        self.name = self.path.name.lower()


def _real(path):
    try:
        return os.path.realpath(str(path))
    except (OSError, ValueError):
        return str(path)


def waggle_homes():
    homes, seen = [], set()
    for h in (USER_HOME / ".waggle", config.HOME):
        if _lex(h, "/") not in seen:
            seen.add(_lex(h, "/"))
            homes.append(Path(h))
    return homes


def entries():
    out = []
    for rel in REPO_FILES:
        out.append(Entry("file", PACKAGE_ROOT / rel, rel))
    for h in waggle_homes():
        for name, kind in WAGGLE_HOME_PATHS:
            out.append(Entry(kind, h / name, f"{h.name}/{name}"))
        out.append(Entry("holdout", h / HOLDOUT_DIR, f"{h.name}/{HOLDOUT_DIR}/**/{HOLDOUT_PREFIX}*"))
    for rel, kind in USER_HOME_PATHS:
        out.append(Entry(kind, USER_HOME / rel, "~/" + rel))
    out.append(Entry("dir", GUARD_DIR, "~/" + GUARD_DIR.name))
    return out


def _file_id(path):
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino) if st.st_ino else None
    except (OSError, ValueError):
        return None


def _ids(ents):
    """{(volume, file id): label} of the protected files that exist, so a hard link, subst drive, 8.3 name or any
    other alias of the same file is recognised by identity, not by spelling."""
    out = {}
    for e in ents:
        if e.kind in ("file", "prefix"):
            fid = _file_id(e.path)
            if fid:
                out[fid] = e.label
    return out


_GLOB_MEMO = {}


def _glob(path, pattern, cross):
    """Shell wildcard match. A `*` / `?` stays inside one folder (`rm *.py` in a parent folder does not reach
    waggle/policy.py) unless cross=True (recursive deletes and moves, where asking too often is the safe side)."""
    if cross:
        return fnmatch.fnmatchcase(path, pattern)
    rx = _GLOB_MEMO.get(pattern)
    if rx is None:
        out, i = [], 0
        while i < len(pattern):
            c = pattern[i]
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            if c == "*":
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            elif c == "[" and "]" in pattern[i + 2:]:
                j = pattern.index("]", i + 2)
                body = pattern[i + 1:j]
                neg = body[:1] in ("!", "^")
                body = (body[1:] if neg else body).replace("\\", "\\\\").replace("[", "\\[")
                out.append("[" + ("^" if neg else "") + body + "]")
                i = j
            else:
                out.append(re.escape(c))
            i += 1
        try:
            rx = re.compile("".join(out) + r"\Z")
        except re.error:  # bracket text that is no valid character class ("[biobuzz-robot]"): the shell takes it literally too
            rx = re.compile("".join(".*" if t == "**" else "[^/]*" if t == "*" else "[^/]" if t == "?" else re.escape(t)
                                    for t in re.findall(r"\*\*|.", pattern, re.S)) + r"\Z")
        _GLOB_MEMO[pattern] = rx
    return bool(rx.match(path))


def _hit(e, f, ancestors, wild, cross=False):
    if wild:
        d = f.rsplit("/", 1)[0]
        if e.kind in ("dir", "holdout"):
            return any(d == p or d.startswith(p + "/") or _glob(p, d, cross) or (ancestors and p.startswith(
                d + "/")) for p in e.forms)
        return any(_glob(p, f, cross) for p in e.forms)
    if e.kind == "file":
        return f in e.forms or (ancestors and any(p.startswith(f + "/") for p in e.forms))
    if e.kind == "prefix":
        parent, _, base = f.rpartition("/")
        if parent in e.parents and base.startswith(e.name):
            return True
        return ancestors and any(p.startswith(f + "/") for p in e.forms)
    if e.kind == "dir":
        return any(f == p or f.startswith(p + "/") or (ancestors and p.startswith(f + "/")) for p in e.forms)
    if e.kind == "holdout":
        for p in e.forms:
            if f.startswith(p + "/") and any(s.startswith(HOLDOUT_PREFIX) for s in f[len(p) + 1:].split("/")):
                return True
            if ancestors and (f == p or p.startswith(f + "/")):
                return True
    return False


def _settings_hit(f, ancestors):
    if CLAUDE_SETTINGS.search(f):
        return True
    return ancestors and (f.endswith("/.claude") or f == ".claude")


def _settings_glob(f):
    """A wildcard (rm .claude/*, Set-Content .clau?e/settings.json) that could name a Claude Code settings file."""
    d, _, b = f.rpartition("/")
    last = d.rpartition("/")[2]
    return fnmatch.fnmatchcase(".claude", last) and any(fnmatch.fnmatchcase(n, b) for n in
                                                         ("settings.json", "settings.local.json"))


def match(raw, cwd=None, ancestors=False, _entries=None, _idmap=None, _cross=None):
    """Label of the protected entry `raw` names (resolving ~, env vars, relative paths against cwd, '..', case,
    trailing dots, links, hard links), or None. With ancestors=True a folder that contains a protected file counts
    too."""
    raw = str(raw or "").strip()
    if not raw or len(raw) > 1024 or "\x00" in raw:
        return None
    cwd = str(cwd or os.getcwd())
    s = expand(raw)
    wild = any(c in s for c in "*?[")
    ents = _entries if _entries is not None else entries()
    forms = [_lex(s, cwd)]
    real = None
    for stage in (0, 1):
        for e in ents:
            if any(_hit(e, f, ancestors, wild, ancestors if _cross is None else _cross) for f in forms):
                return e.label
        if any(_settings_hit(f, ancestors) or (wild and _settings_glob(f)) for f in forms):
            return "Claude Code settings (.claude/settings.json)"
        shadow = next((x for x in map(_shadow_hit, forms) if x), None)
        if shadow:
            return shadow
        if stage == 0 and not wild:
            real = _real(_lex(s, cwd, lower=False).replace("/", os.sep))
            forms = [_lex(real, cwd)]
        else:
            break
    if real is not None:
        fid = _file_id(real)
        if fid:
            label = (_idmap if _idmap is not None else _ids(ents)).get(fid)
            if label:
                return label
    return None


def _shadow_name(name, is_package=False):
    """The standard-library module a file or folder at the checkout root would replace, or None. Python puts the
    working folder first on sys.path for `python -m ...`, so <root>/json.py runs instead of json for the guard
    command and anything else started from the checkout."""
    import importlib.machinery
    if is_package:
        stem = name
    else:
        stem = next((name[:-len(x)] for x in (".py", ".pyc", *importlib.machinery.EXTENSION_SUFFIXES)
                     if name.lower().endswith(x)), None)
    return stem if stem and stem.lower() in {m.lower() for m in sys.stdlib_module_names} else None


def shadows(root=None):
    """Names at the checkout root that shadow a standard-library module (see _shadow_name)."""
    root = Path(root or PACKAGE_ROOT)
    out = []
    try:
        items = list(os.scandir(root))
    except OSError:
        return out
    for e in items:
        try:
            pkg = e.is_dir() and (Path(e.path) / "__init__.py").is_file()
        except OSError:
            pkg = False
        if _shadow_name(e.name, pkg) and (pkg or not e.is_dir()):
            out.append(e.name)
    return sorted(out)


def _roots():
    return {_lex(PACKAGE_ROOT, "/"), _lex(_real(PACKAGE_ROOT), "/")}


def _shadow_hit(f):
    parent, _, base = f.rpartition("/")
    if parent in _roots() and _shadow_name(base):
        return f"{base} at the checkout root (it would replace Python's own {_shadow_name(base)} module)"
    grand, _, folder = parent.rpartition("/")
    if base == "__init__.py" and grand in _roots() and _shadow_name(folder, True):
        return f"{folder}/__init__.py at the checkout root (it would replace Python's own {folder} package)"
    return None


def _inside_package(cwd):
    c = _lex(cwd, os.getcwd())
    return any(c == p or c.startswith(p + "/") for p in {_lex(PACKAGE_ROOT, "/"), _lex(_real(PACKAGE_ROOT), "/")})


# ---- write tools -------------------------------------------------------------------------------

PATH_KEYS = ("file_path", "notebook_path", "path", "filepath", "file", "filename", "target", "destination", "dest",
             "new_path", "old_path", "source", "src", "output", "output_path")
_PATCH_FILE = re.compile(r"^\*\*\*\s*(?:Update File|Add File|Delete File|Move to):\s*(.+?)\s*$|"
                         r"^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)", re.M)
REASON = "changing Waggle's own safety files ({}): needs your yes"


def _strings(v, depth=0):
    """Every short string inside an argument tree (MCP tools name their path arguments however they like)."""
    if isinstance(v, str):
        if len(v) <= 1024:
            yield v
    elif depth < 4 and isinstance(v, dict):
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif depth < 4 and isinstance(v, (list, tuple)):
        for x in v[:50]:
            yield from _strings(x, depth + 1)


def write_hit(args, cwd=None, patch=False, deep=False):
    """Reason string when a write/edit call's target is protected, else None. deep=True looks at every string in
    the arguments, not only the usual path keys."""
    if not isinstance(args, dict):
        return None
    cwd = args.get("cwd") or args.get("workdir") or cwd
    ctx = _Ctx()
    cands = [v for k in PATH_KEYS for v in [args.get(k)] if isinstance(v, str)]
    if deep:
        cands += [s for s in _strings(args) if s not in cands and "\n" not in s]
    if patch:
        for k in ("patch", "diff", "content", "input"):
            if isinstance(args.get(k), str):
                cands += [a or b for a, b in _PATCH_FILE.findall(args[k])]
    for c in cands:
        label = ctx.match(c, cwd)
        if label:
            return REASON.format(label)
    return None


# ---- shell -------------------------------------------------------------------------------------

READ_VERBS = {
    "cat", "type", "get-content", "gc", "head", "tail", "more", "less", "ls", "dir", "gci", "get-childitem", "tree",
    "select-string", "sls", "grep", "egrep", "fgrep", "rg", "ag", "findstr", "wc", "sha256sum", "sha1sum", "md5sum",
    "get-filehash", "stat", "get-item", "gi", "get-itemproperty", "test-path", "resolve-path", "diff", "fc",
    "cmp", "compare-object", "echo", "write-output", "write-host", "printf", "pwd", "get-location", "where", "which",
    "get-command", "gcm", "basename", "dirname", "realpath", "readlink", "true", "pytest",
    # PowerShell pipeline shaping and tests: they pass values along and print, never write a file.
    "select-object", "select", "sort-object", "format-table", "ft", "format-list", "fl", "format-wide", "fw",
    "measure-object", "measure", "group-object", "out-string", "out-host", "join-path", "split-path", "[", "[[",
    "test", "get-date", "convertto-json", "convertfrom-json", "write-verbose", "write-debug", "write-information",
    "cut", "tr", "sha224sum", "sha384sum", "sha512sum", "b2sum", "cksum", "sum", "shasum", "md5",
}
NAV_VERBS = {"cd", "chdir", "sl", "set-location", "pushd", "push-location", "popd", "pop-location"}
BENIGN_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "rev-parse", "rev-list", "grep", "describe",
              "shortlog", "cat-file", "ls-tree", "remote", "fetch", "add", "commit", "push", "tag", "config",
              "branch", "worktree", "reflog", "diff-tree", "show-ref", "for-each-ref"}
# Deletes that only reach into sub-folders with a recursive flag (rm *.py in a parent folder stays in that folder).
_FLAT_DELETES = {"rm", "remove-item", "ri", "rmdir", "rd", "unlink"}
ANCESTOR_VERBS = {"rm", "del", "erase", "rmdir", "rd", "ri", "remove-item", "mv", "move", "move-item", "mi", "ren",
                  "rename", "rename-item", "rni", "robocopy", "xcopy"}
COPY_VERBS = {"cp", "copy", "copy-item", "cpi"}
# Unpacking an archive writes whatever names the archive holds, into the folder named or the working folder.
EXTRACT_VERBS = {"unzip", "expand-archive", "extrac32", "expand", "7z", "7za", "7zr", "bsdtar", "tar"}
WRAPPERS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh"}
WRAP_FLAGS = {"-c", "-command", "/c", "/k", "-lc", "-ic"}
ENCODED_FLAGS = {"-e", "-ec", "-enc", "-encodedcommand", "-encoded"}
PYTHONS = {"python", "python3", "py", "pythonw"}
# Interpreters whose script file is read before the command is allowed (a script written to a write root and run).
SCRIPT_RUNNERS = PYTHONS | {"node", "deno", "bun", "ruby", "perl", "php", "powershell", "pwsh", "bash", "sh", "zsh",
                            "cmd", "cscript", "wscript"}
SCRIPT_EXT = re.compile(r"\.(py|pyw|js|mjs|cjs|ts|ps1|psm1|sh|bash|bat|cmd|rb|pl|php|vbs)$", re.I)
GUARD_CMD = re.compile(r"waggle[. ]guard\b", re.I)
_REDIRECT = re.compile(r"(?<![<>])\d*>{1,2}\|?\s*(\"[^\"]*\"|'[^']*'|[^\s&|;<>]+)")
_DISTINCTIVE = None
# Text in a script or decoded payload that names the protected set (any slash style).
_MENTION = None


def _split(cmd, aware):
    cmd = re.sub(r"(\d*>+)&(\d)", r"\1\2", cmd)
    # A block brace ends a statement too (`if (x) { Remove-Item y }` runs Remove-Item); ${VAR}, @{...} and a/{b,c}
    # braces are not blocks.
    if not aware:
        return [p for p in re.split(r"[;|&\n\r]|(?:^|(?<=[\s)]))\{|\}(?=[\s;|&)]|$)", cmd) if p.strip()]
    out, cur, q = [], [], None
    for i, c in enumerate(cmd):
        if q:
            cur.append(c)
            q = None if c == q else q
        elif c in "\"'":
            q = c
            cur.append(c)
        elif c in ";|&\n\r" or (c == "{" and (i == 0 or cmd[i - 1] in " \t)")) or (
                c == "}" and (i + 1 == len(cmd) or cmd[i + 1] in " \t\n\r;|&)")):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    out.append("".join(cur))
    return [p for p in out if p.strip()]


_ESC = {"bash": "\\", "powershell": "`", "cmd": "^"}
ALL_DIALECTS = ("bash", "powershell", "cmd")
SHELL_DIALECTS = {"bash": ("bash",), "powershell": ("powershell",)}
_WRAP_DIALECT = {"cmd": ("cmd",), "powershell": ("powershell",), "pwsh": ("powershell",), "bash": ("bash",),
                 "sh": ("bash",), "zsh": ("bash",)}


def _split_dialect(cmd, dialect):
    """Statements as `dialect` (bash, powershell or cmd) separates them, with its quotes and escape character, or
    None when its quotes do not close (then the caller uses the over-asking splits instead). A quoted regex like
    "a|b" stays inside its statement; an escaped quote (bash \\", PowerShell `") does not open a string."""
    cmd = re.sub(r"(\d*>+)&(\d)", r"\1\2", cmd)
    esc = _ESC[dialect]
    out, cur, q, i, n = [], [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            cur.append(c)
            if (q == "$'" and c == "\\") or (q == '"' and dialect != "cmd" and c == esc):
                if i + 1 < n:
                    cur.append(cmd[i + 1])
                i += 2
                continue
            if c == q[-1]:
                if dialect == "powershell" and cmd[i + 1:i + 2] == c:  # '' and "" inside a string
                    cur.append(c)
                    i += 2
                    continue
                q = None
            i += 1
            continue
        if c == esc and i + 1 < n:
            cur += [c, cmd[i + 1]]
            i += 2
            continue
        if dialect == "bash" and c == "$" and cmd[i + 1:i + 2] == "'":
            q = "$'"
            cur += ["$", "'"]
            i += 2
            continue
        if c == '"' or (c == "'" and dialect != "cmd"):
            q = c
            cur.append(c)
        elif c in ";|&\n\r" or (c == "{" and (i == 0 or cmd[i - 1] in " \t)")) or (
                c == "}" and (i + 1 == n or cmd[i + 1] in " \t\n\r;|&)")):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if q:
        return None
    out.append("".join(cur))
    return [p for p in out if p.strip()]


def _substitutions(cmd, dialect):
    """Commands inside $( ), <( ), >( ) (bash and PowerShell, also inside double quotes) and bash `backticks`:
    `cat $(rm x)` runs rm."""
    if dialect == "cmd":
        return []
    out, q, i, n = [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q == "'":
            q = None if c == "'" else q
            i += 1
            continue
        if c == _ESC[dialect] and i + 1 < n:
            i += 2
            continue
        if c == '"':
            q = None if q == '"' else '"'
        elif c == "'" and q is None:
            q = "'"
        elif cmd.startswith(("$(", "<(", ">("), i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            out.append(cmd[i + 2:j - 1 if depth == 0 else j])
            i += 2
            continue
        elif c == "`" and dialect == "bash":
            j = cmd.find("`", i + 1)
            out.append(cmd[i + 1:j if j > 0 else n])
            i = j + 1 if j > 0 else n
            continue
        i += 1
    return [x for x in out if x.strip()]


def _statements(cmd, dialect, depth=0):
    """Statements of cmd plus those of its command substitutions, or None when `dialect` cannot parse it."""
    st = _split_dialect(cmd, dialect)
    if st is None:
        return None
    if depth < 4:
        for inner in _substitutions(cmd, dialect):
            more = _statements(inner, dialect, depth + 1)
            if more is None:
                return None
            st += more
    return st


def _parse(text, dialects):
    """[(statements, dialect)] for each dialect the command may run in; when one of them cannot parse it (an
    unclosed quote), the two older splits that ask too often instead: quote-aware, and blind to quotes."""
    parsed = [(_statements(text, d), d) for d in dialects]
    if all(st is not None for st, _d in parsed):
        return parsed
    return [(_split(text, True), None), (_split(text, False), None)]


def _quoted(s, dialect):
    """Set of indexes of s inside quotes, as `dialect` reads them."""
    inside, q, i, esc = set(), None, 0, _ESC[dialect]
    while i < len(s):
        c = s[i]
        if q:
            inside.add(i)
            if q == '"' and dialect != "cmd" and c == esc:
                inside.add(i + 1)
                i += 2
                continue
            if c == q:
                q = None
        elif c == esc and i + 1 < len(s):
            i += 2
            continue
        elif c == '"' or (c == "'" and dialect != "cmd"):
            q = c
        i += 1
    return inside


def _tokens(stmt):
    out, cur, q, has = [], [], None, False
    for c in stmt:
        if q:
            if c == q:
                q = None
            else:
                cur.append(c)
        elif c in "\"'":
            q, has = c, True
        elif c.isspace():
            if cur or has:
                out.append("".join(cur))
            cur, has = [], False
        else:
            cur.append(c)
    if cur or has:
        out.append("".join(cur))
    return out


_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")


def _pieces(tok):
    """A token plus the path-like fragments inside it (python -c "open('x','w')", -Path:x, a=b, a/{b,c})."""
    toks = {tok}
    m = _BRACE.search(tok)
    if m:
        toks.update(tok[:m.start()] + alt + tok[m.end():] for alt in m.group(1).split(",")[:20])
    found = set()
    for tk in toks:
        t = re.sub(r"^-[A-Za-z]+[:=]", "", tk)
        found.update((tk, t))
        found.update(p for p in re.split(r"[,;()<>|&=\[\]{}'\"`]\s*|\s+", t) if p)
    return found


# Words that only route control or values: what follows them is the real command (or nothing).
_KEYWORDS = {"if", "elseif", "elif", "else", "while", "until", "do", "done", "then", "fi", "try", "finally", "{", "}",
             "(", ")", "foreach-object", "%", "where-object", "?", "!", "time", "begin", "process", "end"}
_METHOD_CALL = re.compile(r"\.\s*\w+\s*\(|::\s*\w+\s*\(")
_MEMBER_PIPES = {"foreach-object", "%"}
_MEMBER = re.compile(r"^[A-Za-z_][\w.]*$")


def _member_only(rest):
    """`ForEach-Object Name` / `% Name` / `ForEach-Object -MemberName Name`: reads one property, runs nothing
    (a method name would need -ArgumentList or a script block, which is not this)."""
    rest = [t for t in rest if t.lower() != "-membername"]
    return len(rest) == 1 and bool(_MEMBER.match(rest[0])) and rest[0].lower() in _READ_MEMBERS


# Properties only: `% Delete` / `% MoveTo` would call a method, so a name not listed here is judged as a command.
_READ_MEMBERS = {"fullname", "name", "basename", "extension", "length", "directory", "directoryname", "parent",
                 "lastwritetime", "creationtime", "lastaccesstime", "mode", "attributes", "path", "pspath", "count",
                 "id", "processname", "line", "linenumber", "filename", "value", "key", "source", "version"}


def _command_tokens(tokens, quoted_first=False):
    """(tokens of the command this statement runs, read) where read is True for a statement that runs nothing
    (a `for x in list` header, `$x = 'literal'`, a bare `$_.Name -match 'y'` filter, a lone "string"), False for an
    expression that calls a method (`$f.Delete()`, `[IO.File]::WriteAllText(...)`), None for an ordinary command."""
    toks = list(tokens)
    if quoted_first and len(toks) == 1:  # PowerShell prints a lone string; bash would run "rm" -rf x (2+ tokens)
        return toks, not _METHOD_CALL.search(toks[0])
    for _ in range(20):
        if not toks:
            return [], True
        t = toks[0].lower()
        if t in ("for", "foreach") and len(toks) >= 3 and toks[2].lower() == "in":
            return [], True
        if t in _KEYWORDS:
            if t in _MEMBER_PIPES and _member_only(toks[1:]):  # ForEach-Object FullName: prints a property
                return [], True
            toks = toks[1:]
        elif len(t) > 1 and t[0] in "{(" and not t.startswith("$("):
            toks[0] = toks[0][1:]
        else:
            break
    if not toks:
        return [], True
    m = re.match(r"^\$[\w:.]+(=(.*))?$", toks[0])
    if m and (m.group(1) is not None or toks[1:2] in (["="], ["+="], ["-="])):  # $x = <rhs>
        rhs = ([m.group(2)] if m.group(2) else []) + toks[1:] if m.group(1) is not None else toks[2:]
        if len(rhs) <= 1:  # a value; a one-word command could not name a file anyway
            return rhs, not (rhs and _METHOD_CALL.search(rhs[0]))
        return _command_tokens(rhs)
    first = toks[0]
    if first[:1] in "$@-" and not first.startswith("$("):  # an expression, not a command
        return toks, not _METHOD_CALL.search(" ".join(toks))
    return toks, None


def _verb_info(tokens):
    i = 0
    while i < len(tokens) and (tokens[i] in ("&", ".", "call", "sudo", "time", "nohup", "command", "exec", "env")
                               or re.match(r"^[A-Za-z_]\w*=", tokens[i])):
        i += 1
    if i >= len(tokens):
        return "", []
    verb = os.path.basename(tokens[i].lstrip("({!@$").replace("\\", "/")).lower()
    verb = re.sub(r"\.exe$", "", re.split(r"[()]", verb)[0] or verb.strip("()"))
    rest = tokens[i + 1:]
    if verb in PYTHONS and "-m" in rest and rest[rest.index("-m") + 1:rest.index("-m") + 2] == ["pytest"]:
        verb = "pytest"
    return verb, rest


def _git_sub(rest):
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-C", "-c", "--git-dir", "--work-tree"):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            return t.lower()
    return ""


def _git_read(rest):
    """git subcommands that cannot change the files of the working tree they run in."""
    sub = _git_sub(rest)
    if sub not in BENIGN_GIT or any(t.startswith("--output") for t in rest):
        return False
    head = rest[:rest.index(next(t for t in rest if t.lower() == sub))] if sub in [t.lower() for t in rest] else rest
    if any(t == "-c" or t.startswith(("--config-env", "--exec-path=")) for t in head):
        return False  # git -c core.fsmonitor=<command> status runs that command
    if sub == "grep" and any(t.startswith(("-O", "--open-files-in-pager")) for t in rest):
        return False  # opens the matches in a program of the agent's choice
    if sub == "worktree":  # add / move / remove / repair write or delete whole folders
        low = [t.lower() for t in rest]
        action = next((t for t in low[low.index("worktree") + 1:] if not t.startswith("-")), "")
        return action in ("", "list", "prune")
    return True


def _git_dirs(rest):
    out = [rest[i + 1] for i, t in enumerate(rest[:-1]) if t in ("-C", "--work-tree", "--git-dir")]
    out += [t.split("=", 1)[1] for t in rest if t.startswith(("--work-tree=", "--git-dir="))]
    return out


_SED_LONG = {"--quiet", "--silent", "--regexp-extended", "--posix", "--debug", "--separate", "--unbuffered",
             "--null-data", "--sandbox"}


def _sed_script_ok(sc, writes=False):
    """A sed script that only prints: addresses, then p P l = n N d D g G h H x z q Q b t T : { } # and s/y with
    printing-only flags. w W e r R a i c, s///w and s///e (and anything not understood) are not.
    writes=True asks only "runs no command": a i c r R w W and s///w are then fine, e and s///e never are."""
    i, n = 0, len(sc)

    def ws(i):
        while i < n and sc[i] in " \t":
            i += 1
        return i

    def delimited(i, d):  # index after the unescaped delimiter d, or None
        while i < n:
            if sc[i] == "\\":
                i += 2
                continue
            if sc[i] == d:
                return i + 1
            i += 1
        return None

    while True:
        while i < n and sc[i] in " \t;\n\r":
            i += 1
        if i >= n:
            return True
        for part in (0, 1):
            if sc[i].isdigit() or (part and sc[i] in "+~"):
                i += 1
                while i < n and (sc[i].isdigit() or sc[i] == "~"):
                    i += 1
            elif sc[i] == "$":
                i += 1
            elif sc[i] == "/" or (sc[i] == "\\" and i + 1 < n):
                d = sc[i] if sc[i] == "/" else sc[i + 1]
                i = delimited(i + (1 if sc[i] == "/" else 2), d)
                if i is None:
                    return False
                while i < n and sc[i] in "IM":
                    i += 1
            i = ws(i)
            if part == 0 and i < n and sc[i] == ",":
                i = ws(i + 1)
                if i >= n:
                    return False
                continue
            break
        if i < n and sc[i] == "!":
            i = ws(i + 1)
        if i >= n:
            return False
        c = sc[i]
        i += 1
        if c in "pPlnNdDgGhHxz={}":
            continue
        if c in "qQ":
            while i < n and sc[i].isdigit():
                i += 1
            continue
        if c in "btT:#":
            while i < n and sc[i] not in ";\n":
                i += 1
            continue
        if writes and c in "aicrRwW":  # text or a file name, to the end of the line
            while i < n and sc[i] != "\n":
                i += 1
            continue
        if c in "sy":
            if i >= n:
                return False
            d = sc[i]
            for _part in (0, 1):
                i = delimited(i + 1 if _part == 0 else i, d)
                if i is None:
                    return False
            flags = ""
            while i < n and sc[i].isalnum():
                flags += sc[i]
                if writes and sc[i] == "w":  # s///w FILE: the rest of the line is the file name
                    break
                i += 1
            if writes and flags.endswith("w"):
                while i < n and sc[i] != "\n":
                    i += 1
                flags = flags[:-1]
            if c == "s" and set(flags) - set("gpiImM0123456789"):
                return False
            continue
        return False


def _sed_scripts(rest):
    """The scripts a sed command runs, in place (-i) or not; None when one comes from a file (-f) or the
    arguments are not understood."""
    scripts, i, explicit = [], 0, False
    while i < len(rest):
        t = rest[i]
        if t in ("-e", "--expression"):
            scripts.append(rest[i + 1] if i + 1 < len(rest) else "")
            explicit, i = True, i + 2
            continue
        if t.startswith("--expression="):
            scripts.append(t.split("=", 1)[1])
            explicit, i = True, i + 1
            continue
        if t.startswith("--"):
            if t == "--in-place" or t.startswith("--in-place=") or t in _SED_LONG or t.startswith("--line-length"):
                i += 1
                continue
            return None  # --file and anything unknown
        if t.startswith("-") and len(t) > 1:
            flags, step = t[1:], 1
            for k, ch in enumerate(flags):
                if ch in "nrEsuz":
                    continue
                if ch == "i":  # -i or -iSUFFIX: the rest of the token is the backup suffix
                    break
                if ch in "el":
                    tail = flags[k + 1:]
                    if ch == "e":
                        scripts.append(tail or (rest[i + 1] if i + 1 < len(rest) else ""))
                        explicit = True
                    step = 1 if tail else 2
                    break
                return None  # -f (script file), unknown
            i += step
            continue
        if not explicit and not scripts:
            scripts.append(t)
        i += 1
    return scripts or None


def _sed_read(rest):
    """sed that prints to stdout: no -i / --in-place, no script file, scripts that only print."""
    scripts, i = [], 0
    explicit = False
    while i < len(rest):
        t = rest[i]
        if t in ("-e", "--expression"):
            scripts.append(rest[i + 1] if i + 1 < len(rest) else "")
            explicit, i = True, i + 2
            continue
        if t.startswith("--expression="):
            scripts.append(t.split("=", 1)[1])
            explicit, i = True, i + 1
            continue
        if t.startswith("--"):
            if t not in _SED_LONG and not t.startswith("--line-length"):
                return False  # --in-place, --file, anything unknown
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            flags, step = t[1:], 1
            for k, ch in enumerate(flags):
                if ch in "nrEsuz":
                    continue
                if ch in "el":
                    tail = flags[k + 1:]
                    if ch == "e":
                        scripts.append(tail or (rest[i + 1] if i + 1 < len(rest) else ""))
                        explicit = True
                    step = 1 if tail else 2
                    break
                return False  # -i (in place), -f (script file), unknown
            i += step
            continue
        if not explicit and not scripts:
            scripts.append(t)
        i += 1
    return bool(scripts) and all(_sed_script_ok(x) for x in scripts)


_AWK_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def _awk_read(rest):
    """awk whose program only prints to stdout: no print/printf > or >> file, no | to or from a command, no
    system(), no program file (-f), no in-place or extensions (-i, -l, @include)."""
    prog, i = None, 0
    while i < len(rest):
        t = rest[i]
        if t in ("-F", "-v", "--field-separator", "--assign"):
            i += 2
            continue
        if t.startswith(("-F", "-v", "--field-separator=", "--assign=")):
            i += 1
            continue
        if t == "--":
            prog = rest[i + 1] if i + 1 < len(rest) else None
            break
        if t.startswith("-"):
            return False
        prog = t
        break
    if prog is None:
        return False
    p = _AWK_STRING.sub('""', prog)
    return not (re.search(r"\bsystem\s*\(", p) or "|" in p.replace("||", "") or "@" in p
                or re.search(r"\bprintf?\b[^;{}\n]*>", p))


def _sort_read(rest):
    for t in rest:
        if t == "--":
            break
        if t.startswith("--"):
            if t.split("=")[0] in ("--output", "--compress-program", "--temporary-directory"):
                return False
        elif t.startswith("-") and len(t) > 1 and ("o" in t[1:] or "T" in t[1:]):
            return False
    return True


def _uniq_read(rest):
    """uniq [INPUT [OUTPUT]]: a second file operand is written."""
    ops, i = [], 0
    while i < len(rest):
        t = rest[i]
        if t in ("-f", "-s", "-w"):
            i += 2
            continue
        if t == "--":
            ops += rest[i + 1:]
            break
        if t.startswith("-") and t != "-":
            i += 1
            continue
        ops.append(t)
        i += 1
    return len(ops) <= 1


_FIND_WRITES = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
CONDITIONAL_READS = {
    "sed": _sed_read, "gsed": _sed_read, "awk": _awk_read, "gawk": _awk_read, "mawk": _awk_read, "nawk": _awk_read,
    "sort": _sort_read, "uniq": _uniq_read, "find": lambda rest: not any(t.lower() in _FIND_WRITES for t in rest),
    "file": lambda rest: not any(t.startswith("-C") or t == "--compile" for t in rest),
    "certutil": lambda rest: [t.lower() for t in rest[:1]] == ["-hashfile"],
}


def _runs_commands(verb, rest):
    """sed's e command or s///e flag and awk's system() or | run commands the guard cannot see (built from the
    input), so they ask whatever they name."""
    text = " ".join(rest)
    if verb in ("sed", "gsed"):  # parsed, not pattern-matched: "s/x/schools section/" ran nothing (2026-11-25)
        scripts = _sed_scripts(rest)
        return scripts is None or not all(_sed_script_ok(x, writes=True) for x in scripts)
    if verb in ("awk", "gawk", "mawk", "nawk"):
        p = _AWK_STRING.sub('""', text)
        return bool(re.search(r"\bsystem\s*\(", p) or "|" in p.replace("||", ""))
    return False


def _env_prefix(tokens):
    """A command started with NAME=value in front (bash): PAGER, GIT_*, LESSOPEN... can make a reader run code."""
    for t in tokens:
        if re.match(r"^[A-Za-z_]\w*=", t):
            return True
        if t not in ("&", ".", "call", "sudo", "time", "nohup", "command", "exec", "env"):
            return False
    return False


def _is_read(verb, rest, ctoks):
    """True when this one statement only reads: a plain reader, or a sed / awk / sort / uniq / find / file /
    certutil in its read-only form, git in a read-only subcommand, never with a NAME=value prefix."""
    if not verb:
        return True
    if _env_prefix(ctoks):
        return False
    if verb == "git":
        return _git_read(rest)
    if verb in CONDITIONAL_READS:
        return CONDITIONAL_READS[verb](rest)
    return verb in READ_VERBS


def _unresolvable(t):
    return not t or t == "-" or any(c in t for c in "$%`(")


def _extracts(verb, rest):
    """True when this is an archive being unpacked (tar -x, unzip, Expand-Archive, 7z x, expand)."""
    if verb not in EXTRACT_VERBS:
        return False
    low = [t.lower() for t in rest]
    if verb in ("tar", "bsdtar"):
        first = low[0] if low else ""
        return any(t in ("--extract", "--get") or re.match(r"^-[a-z]*x[a-z]*$", t) for t in low) or bool(
            re.match(r"^[a-wyz]*x[a-z]*$", first))
    if verb in ("7z", "7za", "7zr"):
        return bool(low) and low[0] in ("x", "e")
    if verb == "unzip":
        return not any(t in ("-l", "-t", "-v", "-z") for t in low)
    return True


_EXTRACT_DEST = re.compile(r"^(-C|--directory(=.*)?|-d|-destinationpath(:.*)?|-o.+)$", re.I)


def _script_file(verb, tok0, rest, cwd):
    """The script file an interpreter (or a script run directly) is about to execute, or None."""
    cand = None
    if SCRIPT_EXT.search(tok0 or "") and verb not in SCRIPT_RUNNERS:
        cand = tok0
    elif verb in PYTHONS:
        low = [t.lower() for t in rest]
        if "-c" in low:
            return None
        if "-m" in low:
            mod = rest[low.index("-m") + 1] if low.index("-m") + 1 < len(rest) else ""
            base = Path(cwd) / mod.replace(".", os.sep)
            for p in (base.with_suffix(".py"), base / "__main__.py"):
                if p.is_file():
                    return p
            return None
        skip = False
        for t in rest:
            if skip:
                skip = False
            elif t in ("-W", "-X", "-Q"):
                skip = True
            elif not t.startswith("-") or t == "-":
                cand = None if t == "-" else t
                break
    elif verb in ("powershell", "pwsh"):
        low = [t.lower() for t in rest]
        for flag in ("-file", "-f"):
            if flag in low and low.index(flag) + 1 < len(rest):
                cand = rest[low.index(flag) + 1]
                break
        else:
            cand = next((t for t in rest if SCRIPT_EXT.search(t)), None)
    elif verb in SCRIPT_RUNNERS and verb not in ("cmd",):
        args = [t for t in rest if not t.startswith("-")]
        if verb == "deno" and args[:1] == ["run"]:
            args = args[1:]
        if not any(t.lower() in WRAP_FLAGS for t in rest):
            cand = args[0] if args else None
    if not cand or _unresolvable(cand):
        return None
    p = Path(_lex(expand(cand), cwd, lower=False))
    try:
        return p if p.is_file() and p.stat().st_size <= 2_000_000 else None
    except OSError:
        return None


def _mentions(text):
    """The protected set named in a script or decoded payload, in any slash style, or None."""
    global _MENTION
    if _MENTION is None:
        names = [re.escape(r).replace("/", r"[\\/]+") for r in REPO_FILES]
        extra = [r"waggle-guard", r"waggle\.guard\b", r"from\s+waggle\s+import\s+[^\n]*\bguard\b",
                 r"\bagent_hook\b", r"\bapprovals\.db\b", r"\bbudget\.db\b", r"hermes[\\/]+plugins[\\/]+waggle",
                 r"\.claude[\\/]+settings(\.local)?\.json", r"\.waggle[\\/]+(config\.json|eval\b)"]
        _MENTION = re.compile("|".join(names + extra), re.I)
    m = _MENTION.search(text or "")
    return m.group(0) if m else None


class _Result:
    def __init__(self):
        self.named, self.nonread, self.hit = [], False, None

    def merge(self, other):
        self.named += other.named
        self.nonread = self.nonread or other.nonread
        self.hit = self.hit or other.hit


class _Ctx:
    """Protected entries plus a memo, so a command that repeats a token pays for its path resolution once."""

    def __init__(self):
        self.ents = entries()
        self.memo = {}
        self._ids = None
        self.git_dirs = []
        self.skip_loose = False  # judge names only by the resolved form (shell_hit, _resolved_form)

    @property
    def ids(self):
        if self._ids is None:
            self._ids = _ids(self.ents)
        return self._ids

    def match(self, piece, cwd, ancestors=False, cross=None):
        key = (piece, cwd, ancestors, cross)
        if key not in self.memo:
            self.memo[key] = match(piece, cwd, ancestors, _entries=self.ents, _idmap=self.ids, _cross=cross)
        return self.memo[key]


def _analyze(stmts, cwd, ctx, depth=0, dialect=None):
    res = _Result()
    vcwd = str(cwd)
    for s in stmts:
        tokens = _tokens(s)
        ctoks, expr_read = _command_tokens(tokens, s.lstrip()[:1] in "\"'")
        verb, rest = _verb_info(ctoks)
        quoted = _quoted(s, dialect) if dialect else ()
        for m in _REDIRECT.finditer(s):
            if m.start() in quoted:  # grep "a > b" file: inside a string, not a redirect
                continue
            target = m.group(1)
            label = ctx.match(target, vcwd) if vcwd is not None else (
                None if ctx.skip_loose else _loose_name(target))
            if label:
                res.hit = res.hit or label
            elif _unresolvable(expand(target.strip("\"'"))) and target.strip("\"'").lower() not in (
                    "$null", "/dev/null", "nul", "-"):
                res.nonread = True  # > "$f": a write to a file named elsewhere in the command
        if verb in NAV_VERBS:
            if verb in ("popd", "pop-location"):
                vcwd = None
            else:
                tgt = next((t for t in rest if t.lower() not in ("/d", "-path", "-literalpath")
                            and not t.startswith("-")), "")
                vcwd = None if _unresolvable(tgt) or vcwd is None else _lex(expand(tgt), vcwd, lower=False)
            continue
        sub = None
        if verb in WRAPPERS and depth < 3:
            low = [t.lower() for t in rest]
            idx = next((i for i, t in enumerate(low) if t in WRAP_FLAGS), None)
            enc = next((i for i, t in enumerate(low) if t in ENCODED_FLAGS), None)
            payload = None
            if idx is not None:
                payload = " ".join(rest[idx + 1:])
            elif enc is not None and enc + 1 < len(rest) and verb in ("powershell", "pwsh"):
                payload = _b64text(rest[enc + 1]) or ""
            if payload is not None:
                sub = _Result()
                for st, d in _parse(payload, _WRAP_DIALECT.get(verb, ALL_DIALECTS)):
                    sub.merge(_analyze(st, vcwd or os.getcwd(), ctx, depth + 1, d))
                if idx is None:
                    sub.nonread = True  # an encoded command is never a plain read
        is_git = verb == "git"
        if sub is not None:
            read = not sub.nonread
        elif expr_read is not None:
            read, is_git = expr_read, False
        elif is_git:
            read = _is_read(verb, rest, ctoks)
        else:
            read = _is_read(verb, rest, ctoks)
        if not read:
            res.nonread = True
        if sub is not None:
            res.merge(sub)
        extract = _extracts(verb, rest)
        recursive = any(t.lower() in ("-recurse", "--recursive", "/s", "/e", "/mir") or (
            re.match(r"^-[A-Za-z]{1,3}$", t) and "r" in t.lower()) for t in rest)
        ancestors = verb in ANCESTOR_VERBS or extract or (is_git and not read) or (verb == "find" and not read) or (
            verb in COPY_VERBS and any(
            re.match(r"^-(r|R|a|recurse)$|^/[es]$", t) for t in rest))
        cross = ancestors and (recursive or verb not in _FLAT_DELETES)
        names = []
        if not read and _runs_commands(verb, rest):
            names.append(f"a {verb} program that runs other commands")
        for tok in tokens:
            unres = not read and any(c in expand(tok) for c in "$%`")
            for piece in _pieces(tok):
                if (len(piece) < 2 and not (ancestors and piece == ".")) or (
                        piece.startswith("-") and "/" not in piece and "\\" not in piece):
                    continue
                if read and _WILD_ONLY.match(piece):  # `***/` in a sed pattern, `*` given to grep: names nothing
                    continue
                if vcwd is not None:
                    label = ctx.match(piece, vcwd, ancestors, cross) or (
                        _loose_name(piece) if unres and not ctx.skip_loose else None)
                else:
                    label = None if read or ctx.skip_loose else _loose_name(piece)
                if label:
                    names.append(label)
        if verb == "find" and not read and vcwd is not None and (not rest or rest[0][:1] in "-(!"):
            label = ctx.match(vcwd, vcwd, ancestors=True, cross=True)  # find with no folder searches this one
            if label:
                names.append(label)
        if extract and vcwd is not None and not any(_EXTRACT_DEST.match(t) for t in rest):
            label = ctx.match(vcwd, vcwd, ancestors=True) or (
                "the Waggle checkout" if _inside_package(vcwd) else None)
            if label:
                names.append(label)
        if is_git and not read:
            base = vcwd or os.getcwd()
            dirs = _git_dirs(rest) + ctx.git_dirs + [base]
            if any(_inside_package(_lex(expand(d), base, lower=False)) for d in dirs):
                names.append("the Waggle checkout")
        if not read and vcwd is not None and verb:
            script = _script_file(verb, ctoks[len(ctoks) - len(rest) - 1], rest, vcwd)
            if script is not None:
                try:
                    said = _mentions(script.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    said = None
                if said:
                    names.append(f"a script that names {said}")
        if names:
            res.named += names
            if not read:
                res.hit = res.hit or names[0]
    return res


_WILD_ONLY = re.compile(r"^[*?./\\]+$")
_PY_IMPORT_WAGGLE = re.compile(r"(?:^|[;\n]|-c\s*['\"]?)\s*(?:from\s+waggle(?:\.\w+)*\s+import\b|import\s+waggle\b"
                               r"|__import__\(\s*['\"]waggle)", re.M)


# Basenames that count on their own when the folder is unknown (cd into a variable). The later additions to
# REPO_FILES (config.py, agent.py...) are too common as names, so only their waggle/<name> form counts.
_LOOSE_BASENAMES = {"__init__.py", "policy.py", "security.py", "agent_hook.py", "budget.py", "quota.py", "guard.py",
                    "test_trust.py", "test_guard.py"}


def _loose_name(piece):
    """When the working folder or part of the path is unknown (cd into a variable, $root/waggle/policy.py), a
    protected file's name still counts."""
    global _DISTINCTIVE
    if _DISTINCTIVE is None:
        _DISTINCTIVE = (_LOOSE_BASENAMES | {n for n, k in WAGGLE_HOME_PATHS if n != "config.json"}
                        | {MANIFEST_NAME, VIOLATION_NAME})
    p = piece.replace("\\", "/").rstrip("/. ").lower()
    for rel in REPO_FILES:
        if p == rel or p.endswith("/" + rel):
            return rel
    base = p.rsplit("/", 1)[-1]
    return base if base in _DISTINCTIVE or base.startswith(HOLDOUT_PREFIX) else None


_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_GIT_ENV = re.compile(r"\bGIT_(?:WORK_TREE|DIR)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s;&|]+)", re.I)
_LIT = r"(?:\"[^\"\n]*\"|'[^'\n]*')"
# $p = 'a' + 'b' (PowerShell / Python literals) and p=value / set P=value / export P=value (bash, cmd: no spaces).
_ASSIGN_LIT = re.compile(r"(?:^|[\s;&|(\"'])\$?([A-Za-z_]\w*)\s*=\s*(" + _LIT + r"(?:\s*\+\s*" + _LIT + r")*)")
_ASSIGN_SH = re.compile(r"(?:^|[\s;&|(])(?:set\s+|export\s+)?([A-Za-z_]\w*)=(" + _LIT + r"|[^\s;&|\"'()]+)", re.I)


def _b64text(tok):
    """Printable text a base64 token decodes to (UTF-16LE for PowerShell -EncodedCommand, else UTF-8), or None."""
    import base64
    import binascii
    t = tok.strip("\"'")
    try:
        raw = base64.b64decode(t + "=" * (-len(t) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    for enc in (("utf-16-le",) if raw[1:2] == b"\0" else ()) + ("utf-8",):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if text and sum(c.isprintable() or c in "\r\n\t" for c in text) >= 0.95 * len(text):
            return text
    return None


def _join_literals(s):
    return re.sub(r"([\"'])\s*\+\s*([\"'])", "", s)


# Ways a variable gets a value other than one plain literal assignment: with any of these in the command, the
# substituted form below cannot be trusted to be the only reading, so names still count on their own.
_VAR_OTHER = re.compile(r"(?i)set-variable|new-variable|clear-variable|remove-variable|(?<![\w-])(sv|nv|set\s+/[ap]|"
                        r"declare|typeset|local|readonly|export\s+-|read|mapfile|readarray|eval|iex|"
                        r"invoke-expression|source|foreach|for|while|until|select)(?![\w-])|variable:|\[ref\]|"
                        r"-(outvariable|ov|errorvariable|ev|pipelinevariable|pv|warningvariable|wv|"
                        r"informationvariable|iv)\b|(?<![\w/\\])\.\s+\S|\$\(|`")


def _resolved_form(text):
    """The command with its variables replaced, when that is the ONLY reading of it: every variable it assigns gets one
    plain literal, once, and nothing else sets variables. Then a name such as "$d\\src\\__init__.py" is judged by the
    path it really is (C:\\...\\autonomy-worktrees\\waggle\\src\\__init__.py), not by its bare file name. None otherwise
    (the as-written forms keep matching protected names on their own)."""
    found = _ASSIGN_LIT.findall(text) + _ASSIGN_SH.findall(text)
    if not found or _VAR_OTHER.search(text):
        return None
    names = [n.lower() for n, _v in found]
    if len(names) != len(set(names)):
        return None
    for _n, val in found:
        v = _join_literals(val).strip("\"'")
        if not v or "$" in v or "%" in v:
            return None
    t = _substituted(text)
    return t if t is not None and t not in (text, _join_literals(text)) else None


def _substituted(text):
    """$x / %x% / !x! replaced by the literal assigned to it earlier in the same command (None: nothing assigned)."""
    subs = {}
    for name, val in _ASSIGN_LIT.findall(text) + _ASSIGN_SH.findall(text):
        v = _join_literals(val).strip("\"'")
        if v and not v.startswith("$"):
            subs[name] = v
    if not subs:
        return None
    t = _join_literals(text)
    for name, v in sorted(subs.items(), key=lambda kv: -len(kv[0])):
        n = re.escape(name)
        t = re.sub(r"\$\{?" + n + r"\}?(?![\w(])(?!\s*=[^=])|%" + n + "%|!" + n + "!|"
                   r"(?<![\w$.-])" + n + r"(?![\w(])(?!\s*=[^=])", lambda _m, v=v: v, t)
    return t


def _variants(text):
    """The command as written plus the forms a simple disguise turns back into: 'a' + 'b' joined, $x / %x% replaced
    by the literal assigned to it earlier in the same command, and base64 payloads decoded."""
    out = [text, _join_literals(text)]
    t = _substituted(text)
    if t is not None:
        out.append(t)
    for m in list(_B64.finditer(text))[:10]:
        decoded = _b64text(m.group(0))
        if decoded:
            out.append(decoded)
    return list(dict.fromkeys(out))


def _qsplit(text, seps):
    """Split on any of seps outside quotes (a regex like 'a|b' inside quotes is not a pipe)."""
    out, cur, q, i = [], [], None, 0
    while i < len(text):
        c = text[i]
        if q:
            q = None if c == q else q
        elif c in "\"'":
            q = c
        else:
            sep = next((s for s in seps if text.startswith(s, i)), None)
            if sep:
                out.append("".join(cur))
                cur, i = [], i + len(sep)
                continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _pipeline_hit(text, cwd, ctx):
    """gci waggle | % { Set-Content $_.FullName x }: an earlier stage lists a folder that holds protected files and a
    later stage of the same pipeline writes or deletes. Each `;` / `&&` chain is looked at on its own."""
    for chain in _qsplit(text, ("&&", "||", ";", "\n", "\r")):
        stages = [st for st in _qsplit(chain, ("|",)) if st.strip()]
        if len(stages) < 2 or not any(_analyze(_split(st, True), cwd, ctx).nonread for st in stages[1:]):
            continue
        for st in stages[:-1]:
            tokens = _tokens(st)
            verb, rest = _verb_info(_command_tokens(tokens)[0])
            if verb in NAV_VERBS:
                continue
            for tok in rest:
                for piece in _pieces(tok):
                    if len(piece) >= 2 and not piece.startswith("-"):
                        label = ctx.match(piece, cwd, ancestors=True, cross=False)
                        if label and not label.startswith("Claude Code settings"):
                            return label
    return None


def code_hit(code, cwd=None):
    """Reason string when a Python snippet (Hermes execute_code) could change a protected file, else None: the shell
    analysis of its statements (open('waggle/policy.py', 'w')...) plus any protected name it mentions."""
    code = str(code or "")
    if not code.strip():
        return None
    said = _mentions(code)
    if said:
        return REASON.format(f"code that names {said}")
    return shell_hit(code, cwd)


def shell_hit(command, cwd=None, shell=None):
    """Reason string when a shell command could change a protected file, else None. Reading one (cat, sed -n, grep,
    Get-Content, git diff...) is fine: a command that names a protected file is only held when something in it is
    not a plain reader. shell: "bash" or "powershell" when the tool says which shell runs the command (statements
    are then split the way that shell splits them); otherwise every shell's reading counts. Deliberately over-asks."""
    command = str(command or "")
    if not command.strip():
        return None
    cwd = str(cwd or os.getcwd())
    text = expand(command)
    if GUARD_CMD.search(text):
        return "running Waggle's guard command (it can re-approve changed safety files)"
    ctx = _Ctx()
    ctx.git_dirs = [g.strip("\"'") for g in _GIT_ENV.findall(text)]
    res = _Result()
    variants = list(dict.fromkeys(_variants(text) + [command]))
    if any(GUARD_CMD.search(v) for v in variants[1:]):
        return "running Waggle's guard command (it can re-approve changed safety files)"
    dialects = SHELL_DIALECTS.get(shell, ALL_DIALECTS)
    resolved = _resolved_form(text)
    raw_forms = {text, _join_literals(text), command}
    for variant in variants:
        # the as-written forms of a command whose variables all resolve: names are judged in the resolved form only
        ctx.skip_loose = resolved is not None and variant in raw_forms
        for st, d in _parse(variant, dialects):
            res.merge(_analyze(st, cwd, ctx, dialect=d))
        res.hit = res.hit or _pipeline_hit(variant, cwd, ctx)
    ctx.skip_loose = False
    if res.hit:
        return REASON.format(res.hit)
    if res.named and res.nonread:
        return REASON.format(res.named[0])
    if res.nonread:
        low = "\n".join(variants).lower().replace("\\", "/")
        if GUARD_DIR.name.lower() in low:
            return REASON.format("~/" + GUARD_DIR.name)
        if ".waggle" in low and re.search(r"approvals\.db|budget\.db|config\.json|holdout|/eval\b", low):
            return REASON.format("a file in the Waggle data folder")
        # an import STATEMENT (line start, after ; or -c), not prose: draft listing text saying "from Waggle ... a
        # parent must approve" held the money objective's build step (2026-11-25)
        if (_PY_IMPORT_WAGGLE.search(low) or re.search(
                r"waggle\.(policy|security|agent_hook|budget|quota|guard|config)\b", low)) and re.search(r"open\(|write_|__file__|shutil|\.replace\(|unlink|\.remove\(|rename|approve",
                                        low):
            return REASON.format("Waggle's code, through a script")
    return None


# ---- the owner's turn ----------------------------------------------------------------------------
# The engine (chat_engine.run_turn) writes one small state file per chat at the start of every turn: who the turn
# is from and, for the owner's own messages, what they said (a voice note: its transcript). The policy reads it in
# the hook (path from the session's environment, fixed per chat) to tell whether the owner's message covers an
# action that would otherwise ask (policy.owner_rule). The folder is in the protected set (path-protected, not
# hashed), so an agent's tools cannot write it without the user's yes; anything wrong with the file (missing,
# unreadable, another chat's, finished, too old, a link) means "no owner instruction", which is today's rules.

TURN_STATE_ENV = "WAGGLE_TURN_STATE"
TURN_STATE_VERSION = 1
TURN_MAX_AGE_S = 3600  # ceiling, whatever the state says: a turn never lasts longer than this


def _turn_name(chat):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(chat or ""))[:80] or "chat"


def turn_state_dir(home=None):
    return Path(home or config.HOME) / TURN_DIR


def turn_state_path(chat, home=None):
    """Where the engine keeps one chat's turn state (fixed per chat, so the chat process's environment carries it)."""
    return turn_state_dir(home) / f"{_turn_name(chat)}.json"


def _write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def write_turn_state(chat, turn_id, origin, text, voice=False, confident=True, max_age_s=900, now=None):
    """Record the turn that is starting (called by the engine process, never by an agent's tools). origin "user" is
    the owner's own message; text is what they said (a voice note: the transcript), never content a tool fetched.
    confident=False: speech recognition was unsure of the words. Never raises: returns the path, or None."""
    try:
        now = time.time() if now is None else float(now)
        state = {"version": TURN_STATE_VERSION, "chat": str(chat), "turn_id": str(turn_id),
                 "origin": str(origin or ""), "text": str(text or "")[:4000], "voice": bool(voice),
                 "confident": confident is not False, "started": now,
                 "max_age_s": float(min(max(float(max_age_s or 900), 1.0), TURN_MAX_AGE_S)), "ended": None,
                 "pid": os.getpid()}
        path = turn_state_path(chat)
        _write_state(path, state)
        return path
    except Exception:
        return None


def end_turn_state(chat, turn_id, now=None):
    """Mark the turn finished, so nothing that runs after it can use what the owner said in it. Never raises."""
    try:
        path = turn_state_path(chat)
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("turn_id") != str(turn_id):
            return False
        state["ended"] = time.time() if now is None else float(now)
        _write_state(path, state)
        return True
    except Exception:
        return False


def _owner_engine_chats():
    """The owner's engine chats as chat keys ("telegram:<id>", settings.engine_chats). Empty on any error."""
    try:
        s = config.Settings.load()
        return {f"telegram:{str(c).strip()}" for c in (getattr(s, "engine_chats", None) or []) if str(c).strip()}
    except Exception:
        return set()


def read_turn_state(env=None, now=None):
    """The running turn's state for the chat process this runs in, or None ("no owner instruction").

    Accepted only when: WAGGLE_TURN_STATE names exactly this chat's file in the Waggle data folder's turns/ (not a
    link, not a folder that resolves elsewhere), the process is the chat brain (WAGGLE_AGENT=chat, so a background
    agent or objective that inherited the variable is not), the file says it is this chat's, the turn has not ended,
    and it started no longer ago than its own limit (TURN_MAX_AGE_S at most). The result carries "owner": True only
    for origin "user" in a chat process started for the user, or origin "canary" (the owner's test-marked message)
    in a chat process started for it, in one of the owner's engine chats: only the owner's account can write there."""
    env = os.environ if env is None else env
    try:
        raw = str(env.get(TURN_STATE_ENV) or "").strip()
        chat = str(env.get("WAGGLE_SESSION") or "")
        if not raw or not chat or env.get("WAGGLE_AGENT") != "chat":
            return None
        expected = turn_state_path(chat)
        if _lex(raw, "/") != _lex(expected, "/") or os.path.islink(expected):
            return None
        real_dir = os.path.normcase(_real(turn_state_dir()))
        if real_dir != os.path.normcase(os.path.join(_real(config.HOME), TURN_DIR)):
            return None  # turns/ is a link or junction to somewhere else
        if os.path.normcase(_real(expected)) != os.path.normcase(os.path.join(real_dir, expected.name)):
            return None
        state = json.loads(expected.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != TURN_STATE_VERSION or state.get("chat") != chat:
            return None
        now = time.time() if now is None else float(now)
        started = float(state.get("started"))
        limit = min(float(state.get("max_age_s") or 0), TURN_MAX_AGE_S)
        if state.get("ended") is not None or started > now + 5 or now - started > limit:
            return None
        proc_origin = str(env.get("WAGGLE_TURN_ORIGIN") or "user").lower()
        origin = state.get("origin")
        if origin == "user":
            state["owner"] = proc_origin == "user" and not env.get("WAGGLE_ORIGIN")
        else:  # a test-marked message the owner sent in their own engine chat (the end-to-end test client)
            state["owner"] = (origin == "canary" and proc_origin == "canary"
                              and str(env.get("WAGGLE_ORIGIN") or "canary").lower() == "canary"
                              and chat in _owner_engine_chats())
        state["text"] = str(state.get("text") or "")
        state["confident"] = state.get("confident") is not False
        return state
    except Exception:
        return None


def current_turn(env=None):
    """The running turn's state for this process (the chat brain's tools: the policy hook and Waggle's own tool
    server, which both inherit WAGGLE_TURN_STATE from the chat session), or None when there is no fresh one (no
    turn running, finished, too old, another chat's, unreadable). Never raises. Fields:
      turn_id (str), origin (str: "user" for the owner's own message, else test / canary / bench / agent / ...),
      text (str: what the owner said; a voice note's transcript; never content a tool fetched),
      voice (bool: the message was a voice note), confident (bool: speech recognition was sure of the words; True
      for typed messages), started (float: epoch seconds when the turn began), chat (str), owner (bool: origin is
      "user" in a chat process started for the user)."""
    try:
        st = read_turn_state(env)
        if not st:
            return None
        return {k: st.get(k) for k in ("turn_id", "origin", "text", "voice", "confident", "started", "chat", "owner")}
    except Exception:
        return None


def owner_rule_enabled(home=None):
    """config.json "owner_instructions" (default on). A protected key: changing it needs `waggle.guard approve`."""
    picked = _config_picked(home)
    return not (isinstance(picked, dict) and picked.get("owner_instructions") is False)


# ---- manifest ----------------------------------------------------------------------------------

def normalise(data):
    """Bytes as hashed: CRLF -> LF for text (no NUL byte), raw bytes for binary files."""
    return data if b"\0" in data else data.replace(b"\r\n", b"\n")


def sha(data):
    return hashlib.sha256(normalise(data)).hexdigest()


def _sha_file(path):
    try:
        return sha(Path(path).read_bytes())
    except OSError:
        return None


def _config_picked(home=None):
    """The protected keys present in config.json, or None when it cannot be read as a JSON object."""
    p = Path(home or config.HOME) / "config.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return {k: data[k] for k in CONFIG_KEYS if k in data}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def config_keys_hash(home=None):
    """SHA-256 of the canonical JSON of only the protected keys, so unrelated settings may change freely.
    A key that is absent counts as absent (adding "stop_at": 1.0 is a change)."""
    picked = _config_picked(home)
    if picked is None:
        return "unparseable"
    return hashlib.sha256(_canon(picked).encode()).hexdigest()


def config_key_hashes(home=None):
    """{key: sha256} per protected key present, kept in the manifest only to say WHICH key changed."""
    picked = _config_picked(home)
    return None if picked is None else {k: hashlib.sha256(_canon(v).encode()).hexdigest() for k, v in picked.items()}


def changed_config_keys(old, new):
    """Names of the protected keys that differ between two config_key_hashes results (added, removed, changed)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return ["config.json (could not be read)"] if new is None else []
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def _holdout_files(home):
    root = Path(home) / HOLDOUT_DIR
    out = {}
    if root.is_dir():
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = Path(dirpath) / f
                rel = p.relative_to(root).as_posix()
                if any(s.lower().startswith(HOLDOUT_PREFIX) for s in rel.split("/")):
                    out["home:eval/" + rel] = _sha_file(p)
    return out


def snapshot():
    """Hash of every hashed item in the protected set right now: {key: sha256 | None}."""
    snap = {rel: _sha_file(PACKAGE_ROOT / rel) for rel in REPO_FILES}
    for rel, kind in USER_HOME_PATHS:
        if kind == "file":
            snap["user:" + rel] = _sha_file(USER_HOME / rel)
    snap[CONFIG_KEY] = config_keys_hash()
    snap.update(_holdout_files(config.HOME))
    return snap


def resolved_root(root=None):
    """The checkout's final path (junctions, symlinks, subst drives and 8.3 names resolved), case-folded."""
    return os.path.normcase(os.path.realpath(str(root or PACKAGE_ROOT)))


def checkout_key(root=None):
    """Which manifest folder belongs to a checkout: a hash of its resolved path. Each checkout is approved on its
    own; approving (or forging) another checkout's folder never changes this one's verdict."""
    return hashlib.sha256(resolved_root(root).encode("utf-8")).hexdigest()[:32]


def checkout_dir():
    return GUARD_DIR / checkout_key()


def manifest_path():
    return checkout_dir() / MANIFEST_NAME


def manifest_failures(m):
    """What makes a parsed manifest unusable for this checkout, as [{file, expected, actual}] (empty = usable)."""
    out = []
    if m.get("version") != MANIFEST_VERSION:
        out.append({"file": "manifest.json", "expected": f"version {MANIFEST_VERSION}",
                    "actual": f"version {m.get('version')}"})
    elif os.path.normcase(str(m.get("root"))) != resolved_root():
        out.append({"file": "manifest.json", "expected": f"approved for {resolved_root()}",
                    "actual": f"approved for {m.get('root')}"})
    return out


def load_manifest():
    try:
        m = json.loads(manifest_path().read_text(encoding="utf-8"))
        return m if isinstance(m.get("files"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def _diff(old, new):
    out = []
    for key in sorted(set(old) | set(new)):
        exp, act = old.get(key), new.get(key)
        if key not in old:
            if act is not None:
                out.append({"path": key, "kind": "added", "expected": None, "actual": act})
        elif exp != act:
            out.append({"path": key, "kind": "missing" if act is None else "changed", "expected": exp,
                        "actual": act})
    return out


def _same(a, b):
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
    except (OSError, ValueError):
        return False


def environment_failures(m):
    """Ways the running process is pointed somewhere other than what was approved: USERPROFILE / HOME not the
    account's real profile (Path.home() feeds the default Waggle folder and the write roots), or a Waggle data folder
    (WAGGLE_HOME) that is not the one the manifest was approved for (approvals.db lives there)."""
    out = []
    if not _same(Path.home(), USER_HOME):
        out.append({"file": "USERPROFILE", "expected": str(USER_HOME), "actual": str(Path.home())})
    home = m.get("waggle_home") if isinstance(m, dict) else None
    if not home or not _same(config.HOME, home):
        out.append({"file": "WAGGLE_HOME", "expected": str(home), "actual": str(config.HOME)})
    return out


NOT_CHECKED = "not checked: this process was started by a test run"


def started_by_test():
    """A process a test started (it inherited pytest's PYTEST_CURRENT_TEST / PYTEST_VERSION) that is not pytest itself,
    where no test asked for the real check (WAGGLE_GUARD_IN_TESTS=1). Used only by verify(), and it can only make
    verify's answer stricter than clean: see verify."""
    return ("_pytest" not in sys.modules and bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION"))
            and os.environ.get("WAGGLE_GUARD_IN_TESTS") != "1")


def verify():
    """Violations over the whole protected set: [{path, kind, expected, actual}], kind = missing | changed | added
    (a new holdout file) | no_manifest | old_manifest | environment. An empty list means clean.

    In a process started by a test run (a core under a temp home) the answer is "not checked" (kind no_manifest, so
    the core records "not approved", never "ok") instead of the environment mismatch every temp home has; a changed
    file is still reported. An agent that sets the marker for a process it starts gains nothing: that process can
    never get a clean verdict, and the hook and the gate do not read the marker at all."""
    if started_by_test():
        found = _verify()
        # under another data folder its config.json and holdout files are not the approved ones either
        other_home = any(v["kind"] == "environment" and v["path"] == "WAGGLE_HOME" for v in found)
        return [{"path": NOT_CHECKED, "kind": "no_manifest", "expected": None, "actual": None}] + [
            v for v in found if v["kind"] not in ("no_manifest", "environment")
            and not (other_home and (v["path"] == CONFIG_KEY or v["path"].startswith("home:")))]
    return _verify()


def _verify():
    m = load_manifest()
    if m is None:
        return [{"path": str(manifest_path()), "kind": "no_manifest", "expected": None, "actual": None}]
    out = [{"path": str(manifest_path()), "kind": "old_manifest" if "version" in f["expected"] else "other_checkout",
            "expected": f["expected"], "actual": f["actual"]} for f in manifest_failures(m)]
    out += [{"path": e["file"], "kind": "environment", "expected": e["expected"], "actual": e["actual"]}
            for e in environment_failures(m)]
    out += [{"path": name, "kind": "shadow", "expected": None, "actual": "present"} for name in shadows()]
    for v in _diff(m["files"], snapshot()):
        if v["path"] == CONFIG_KEY:
            v["keys"] = changed_config_keys(m.get("config_keys"), config_key_hashes())
        out.append(v)
    return out


# ---- the hook's verdict, for long-running processes ---------------------------------------------

GATE_TTL = 3.0
GATE_IN_TESTS = False  # tests that exercise gate() set this; every other test runs policy without a manifest
_gate_memo = {"at": -1e9, "bad": None}


def hook_failure():
    """The verdict agent_hook.integrity_failure gives (same files, same hashing, same environment checks), computed
    here so a process that imported policy long ago can ask for it: None when clean, else [{file, expected, actual}]."""
    try:
        try:
            m = json.loads(manifest_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [{"file": "manifest.json", "expected": "an approved manifest", "actual": "missing"}]
        files = m["files"]
        if not isinstance(files, dict):
            raise ValueError("files is not a table")
        bad = manifest_failures(m)
        for rel in HOOK_CHECKED:
            actual = _sha_file(PACKAGE_ROOT / rel)
            if actual is None or files.get(rel) != actual:
                bad.append({"file": rel, "expected": files.get(rel), "actual": actual})
        bad += [{"file": name, "expected": "no file shadowing a Python module at the checkout root",
                 "actual": "present"} for name in shadows()]
        return (bad + environment_failures(m)) or None
    except Exception as e:
        return [{"file": "manifest.json", "expected": "a readable manifest",
                 "actual": f"check failed ({type(e).__name__})"}]


def gate():
    """Cheap fail-closed check for code that calls the policy inside a long-running process (the gateway, the
    bridge, objective workers): the same verdict as the hook, cached for GATE_TTL seconds. None = clean.

    What it adds: a process that loaded policy.py before an agent changed the files on disk stops allowing tool
    calls within GATE_TTL seconds, like the hook does; and because that process loaded guard.py before the change,
    it also notices a changed agent_hook.py, which the hook cannot honestly report about itself. What it cannot do:
    a process started after policy.py or guard.py was changed runs the changed code, and that code can skip this."""
    if "_pytest" in sys.modules and not GATE_IN_TESTS:
        return None
    now = time.monotonic()
    if now - _gate_memo["at"] < GATE_TTL:
        return _gate_memo["bad"]
    bad = hook_failure()
    _gate_memo.update(at=now, bad=bad)
    return bad


def denial_text(bad):
    """What an agent is told when the safety check fails (the hook's own wording, see agent_hook.denial_text)."""
    try:
        from . import agent_hook
        return agent_hook.denial_text(bad, PACKAGE_ROOT)
    except Exception:  # still a block, with the bare names
        names = ", ".join(str(b.get("file") or b.get("path")) for b in bad)
        return (f"Waggle's safety check failed ({names}), so every action is blocked until the user runs "
                f'`cd "{PACKAGE_ROOT}"; python -m waggle.guard approve`. Do not retry or work around this.')


def gate_passed():
    """The hook calls this after its own check passed, so the policy it runs next does not hash everything again."""
    _gate_memo.update(at=time.monotonic(), bad=None)


def write_manifest(snap=None):
    snap = snapshot() if snap is None else snap
    folder = checkout_dir()
    folder.mkdir(parents=True, exist_ok=True)
    body = {"version": MANIFEST_VERSION, "created": time.time(), "root": resolved_root(),
            "waggle_home": str(config.HOME), "hashing": "sha256, CRLF->LF for text files", "files": snap,
            "config_keys": config_key_hashes()}
    tmp = folder / f"{MANIFEST_NAME}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, manifest_path())
    _gate_memo.update(at=-1e9, bad=None)
    try:
        (folder / VIOLATION_NAME).unlink()
    except OSError:
        pass
    return body


# ---- command line ------------------------------------------------------------------------------

def _fmt(v):
    keys = f"  keys: {', '.join(v['keys'])}" if v.get("keys") else ""
    return (f"{v['kind']:<11} {v['path']}  expected={(v['expected'] or '-')[:12]} actual={(v['actual'] or '-')[:12]}"
            + keys)


def status(out=print):
    m = load_manifest()
    if m is None:
        out(f"manifest: MISSING ({manifest_path()}). Run: python -m waggle.guard approve")
        return 0
    age = int(time.time() - m.get("created", 0))
    bad = _diff(m["files"], snapshot())
    hashed = sum(1 for v in m["files"].values() if v is not None)
    out(f"manifest: present, written {age // 3600} h {age % 3600 // 60} min ago")
    out(f"entries: {len(m['files'])} ({hashed} hashed, {len(m['files']) - hashed} absent when approved)")
    out(f"violations: {len(bad)}" + (" (run: python -m waggle.guard verify)" if bad else ""))
    out(f"violation record: {'present' if (checkout_dir() / VIOLATION_NAME).exists() else 'none'}")
    out(f"checkout: {resolved_root()}  data folder: {m.get('waggle_home')}")
    return 0


def cmd_verify(out=print):
    bad = verify()
    for v in bad:
        out(_fmt(v))
    out("clean" if not bad else f"{len(bad)} violation(s)")
    return 1 if bad else 0


def _interactive():
    """A real terminal. On Windows NUL (stdin=DEVNULL) also says isatty(), so ask the console API."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            mode = ctypes.c_uint()
            return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-10),
                                                              ctypes.byref(mode)))
        return True
    except Exception:
        return False


def approve(yes=False, out=print, interactive=None, ask=input):
    m = load_manifest()
    snap = snapshot()
    changes = _diff(m["files"], snap) if m else [{"path": "(no manifest yet)", "kind": "added", "expected": None,
                                                  "actual": None}]
    for v in changes:
        out(_fmt(v))
    if not changes:
        out("nothing differs from the current manifest")
    if not yes:
        interactive = _interactive() if interactive is None else interactive
        if not interactive:
            out("not approved: run this in a terminal, or pass --yes. Nothing was changed.")
            return 2
        try:
            answer = ask("Write a new manifest from the files as they are now? [y/N] ")
        except EOFError:
            out("not approved: no answer was given. Nothing was changed.")
            return 2
        if answer.strip().lower() != "y":
            out("not approved. Nothing was changed.")
            return 1
    write_manifest(snap)
    out(f"manifest written: {manifest_path()}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        return status()
    if cmd == "verify":
        return cmd_verify()
    if cmd == "approve":
        return approve(yes="--yes" in argv)
    print("usage: python -m waggle.guard status | verify | approve [--yes]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
