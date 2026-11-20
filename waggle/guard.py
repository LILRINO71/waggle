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
MANIFEST_VERSION = 2  # 2 = line-ending-normalised hashes + the Waggle home it was approved for

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
WAGGLE_HOME_PATHS = (("config.json", "file"), ("approvals.db", "prefix"), ("budget.db", "prefix"))
HOLDOUT_DIR, HOLDOUT_PREFIX = "eval", "holdout"
# Under the user's home. install.py copies the Hermes plugin here, so this copy is the one that runs.
USER_HOME_PATHS = ((".waggle-guard", "dir"), (".hermes/plugins/waggle/__init__.py", "file"))
# Hashed but only some of these settings count: the budget/limit keys, plus the settings that decide where agents
# may write without asking and whose mail needs no approval (policy.write_roots, policy._only_to_self).
CONFIG_KEYS = (
    "waggle_share", "stop_at", "window_units_estimate", "window_hours",
    "background_daily_units", "background_stop_at", "background_max_workers", "background_reset_hour",
    "background_downgrade_at", "interactive_yield_s", "max_verifications_per_day", "max_calls_per_day",
    "max_selfbuilds_per_day",
    "extra_write_roots", "vault", "jobs_dir", "self_emails",
)
CONFIG_KEY = "home:config.json#keys"


# ---- paths -------------------------------------------------------------------------------------

_ENV_RE = re.compile(r"%([A-Za-z_]\w*)%|!([A-Za-z_]\w*)!|\$\{(?:env:)?([A-Za-z_]\w*)\}|\$env:([A-Za-z_]\w*)|"
                     r"\$([A-Za-z_]\w*)", re.I)


def _env(name):
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


def _hit(e, f, ancestors, wild):
    if wild:
        d = f.rsplit("/", 1)[0]
        if e.kind in ("dir", "holdout"):
            return any(d == p or d.startswith(p + "/") or fnmatch.fnmatchcase(p, d) or (ancestors and p.startswith(
                d + "/")) for p in e.forms)
        return any(fnmatch.fnmatchcase(p, f) for p in e.forms)
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


def match(raw, cwd=None, ancestors=False, _entries=None, _idmap=None):
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
            if any(_hit(e, f, ancestors, wild) for f in forms):
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
    "get-filehash", "file", "stat", "get-item", "gi", "get-itemproperty", "test-path", "resolve-path", "diff", "fc",
    "cmp", "compare-object", "echo", "write-output", "write-host", "printf", "pwd", "get-location", "where", "which",
    "get-command", "gcm", "basename", "dirname", "realpath", "readlink", "true", "pytest",
}
NAV_VERBS = {"cd", "chdir", "sl", "set-location", "pushd", "push-location", "popd", "pop-location"}
BENIGN_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "rev-parse", "rev-list", "grep", "describe",
              "shortlog", "cat-file", "ls-tree", "remote", "fetch", "add", "commit", "push", "tag", "config",
              "branch", "worktree", "reflog", "diff-tree", "show-ref", "for-each-ref"}
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
    if not aware:
        return [p for p in re.split(r"[;|&\n\r]", cmd) if p.strip()]
    out, cur, q = [], [], None
    for c in cmd:
        if q:
            cur.append(c)
            q = None if c == q else q
        elif c in "\"'":
            q = c
            cur.append(c)
        elif c in ";|&\n\r":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    out.append("".join(cur))
    return [p for p in out if p.strip()]


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


def _verb_info(tokens):
    i = 0
    while i < len(tokens) and (tokens[i] in ("&", ".", "call", "sudo", "time", "nohup", "command", "exec", "env")
                               or re.match(r"^[A-Za-z_]\w*=", tokens[i])):
        i += 1
    if i >= len(tokens):
        return "", []
    verb = os.path.basename(tokens[i].lstrip("({!@$").replace("\\", "/")).lower()
    verb = re.sub(r"\.exe$", "", verb).rstrip(")")
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
    if sub == "worktree":  # add / move / remove / repair write or delete whole folders
        low = [t.lower() for t in rest]
        action = next((t for t in low[low.index("worktree") + 1:] if not t.startswith("-")), "")
        return action in ("", "list", "prune")
    return True


def _git_dirs(rest):
    out = [rest[i + 1] for i, t in enumerate(rest[:-1]) if t in ("-C", "--work-tree", "--git-dir")]
    out += [t.split("=", 1)[1] for t in rest if t.startswith(("--work-tree=", "--git-dir="))]
    return out


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

    @property
    def ids(self):
        if self._ids is None:
            self._ids = _ids(self.ents)
        return self._ids

    def match(self, piece, cwd, ancestors=False):
        key = (piece, cwd, ancestors)
        if key not in self.memo:
            self.memo[key] = match(piece, cwd, ancestors, _entries=self.ents, _idmap=self.ids)
        return self.memo[key]


def _analyze(stmts, cwd, ctx, depth=0):
    res = _Result()
    vcwd = str(cwd)
    for s in stmts:
        tokens = _tokens(s)
        verb, rest = _verb_info(tokens)
        for m in _REDIRECT.finditer(s):
            target = m.group(1)
            label = ctx.match(target, vcwd) if vcwd is not None else _loose_name(target)
            if label:
                res.hit = res.hit or label
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
            if idx is not None:
                payload = " ".join(rest[idx + 1:])
                sub = _analyze(_split(payload, True) + _split(payload, False), vcwd or os.getcwd(), ctx, depth + 1)
            elif enc is not None and enc + 1 < len(rest) and verb in ("powershell", "pwsh"):
                payload = _b64text(rest[enc + 1]) or ""
                sub = _analyze(_split(payload, True) + _split(payload, False), vcwd or os.getcwd(), ctx, depth + 1)
                sub.nonread = True  # an encoded command is never a plain read
        is_git = verb == "git"
        if sub is not None:
            read = not sub.nonread
        elif is_git:
            read = _git_read(rest)
        else:
            read = verb in READ_VERBS
        if not read:
            res.nonread = True
        if sub is not None:
            res.merge(sub)
        extract = _extracts(verb, rest)
        ancestors = verb in ANCESTOR_VERBS or extract or (is_git and not read) or (verb in COPY_VERBS and any(
            re.match(r"^-(r|R|a|recurse)$|^/[es]$", t) for t in rest))
        names = []
        for tok in tokens:
            unres = not read and any(c in expand(tok) for c in "$%`")
            for piece in _pieces(tok):
                if len(piece) < 2 or (piece.startswith("-") and "/" not in piece and "\\" not in piece):
                    continue
                if vcwd is not None:
                    label = ctx.match(piece, vcwd, ancestors) or (_loose_name(piece) if unres else None)
                else:
                    label = None if read else _loose_name(piece)
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
            script = _script_file(verb, tokens[len(tokens) - len(rest) - 1], rest, vcwd)
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
_ASSIGN = re.compile(r"(?:^|[\s;&|(\"'])(?:set\s+|export\s+|\$)?([A-Za-z_]\w*)\s*=\s*(" + _LIT + r"(?:\s*\+\s*" + _LIT +
                     r")*|[^\s;&|\"'()]+)", re.I)


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


def _variants(text):
    """The command as written plus the forms a simple disguise turns back into: 'a' + 'b' joined, $x / %x% replaced
    by the literal assigned to it earlier in the same command, and base64 payloads decoded."""
    out = [text, _join_literals(text)]
    subs = {}
    for name, val in _ASSIGN.findall(text):
        v = _join_literals(val).strip("\"'")
        if v:
            subs[name] = v
    if subs:
        t = out[1]
        for name, v in sorted(subs.items(), key=lambda kv: -len(kv[0])):
            t = re.sub(r"\$\{?" + re.escape(name) + r"\}?\b|%" + re.escape(name) + "%|!" + re.escape(name) + "!|"
                       r"(?<![\w$.])" + re.escape(name) + r"(?![\w=])", lambda _m, v=v: v, t)
        out.append(t)
    for m in list(_B64.finditer(text))[:10]:
        decoded = _b64text(m.group(0))
        if decoded:
            out.append(decoded)
    return list(dict.fromkeys(out))


def shell_hit(command, cwd=None):
    """Reason string when a shell command could change a protected file, else None. Reading one (cat, Get-Content,
    git diff...) is fine: a command that names a protected file is only held when something in it is not a plain
    reader. Deliberately over-asks."""
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
    for variant in variants:
        for aware in (True, False):
            res.merge(_analyze(_split(variant, aware), cwd, ctx))
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
        if re.search(r"\b(import|from)\s+waggle|waggle\.(policy|security|agent_hook|budget|quota|guard|config)\b",
                     low) and re.search(r"open\(|write_|__file__|shutil|\.replace\(|unlink|\.remove\(|rename|approve",
                                        low):
            return REASON.format("Waggle's code, through a script")
    return None


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


def config_keys_hash(home=None):
    """SHA-256 of the canonical JSON of only the protected keys, so unrelated settings may change freely.
    A key that is absent counts as absent (adding "stop_at": 1.0 is a change)."""
    p = Path(home or config.HOME) / "config.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        picked = {k: data[k] for k in CONFIG_KEYS if k in data}
    except (OSError, ValueError, TypeError, AttributeError):
        return "unparseable"
    return hashlib.sha256(json.dumps(picked, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def manifest_path():
    return GUARD_DIR / MANIFEST_NAME


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
    if home and not _same(config.HOME, home):
        out.append({"file": "WAGGLE_HOME", "expected": str(home), "actual": str(config.HOME)})
    return out


def verify():
    """Violations over the whole protected set: [{path, kind, expected, actual}], kind = missing | changed | added
    (a new holdout file) | no_manifest | old_manifest | environment. An empty list means clean."""
    m = load_manifest()
    if m is None:
        return [{"path": str(manifest_path()), "kind": "no_manifest", "expected": None, "actual": None}]
    out = []
    if m.get("version") != MANIFEST_VERSION:
        out.append({"path": str(manifest_path()), "kind": "old_manifest", "expected": str(MANIFEST_VERSION),
                    "actual": str(m.get("version"))})
    out += [{"path": e["file"], "kind": "environment", "expected": e["expected"], "actual": e["actual"]}
            for e in environment_failures(m)]
    out += [{"path": name, "kind": "shadow", "expected": None, "actual": "present"} for name in shadows()]
    return out + _diff(m["files"], snapshot())


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
        bad = []
        if m.get("version") != MANIFEST_VERSION:
            bad.append({"file": "manifest.json", "expected": f"version {MANIFEST_VERSION}",
                        "actual": f"version {m.get('version')}"})
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


def gate_passed():
    """The hook calls this after its own check passed, so the policy it runs next does not hash everything again."""
    _gate_memo.update(at=time.monotonic(), bad=None)


def write_manifest(snap=None):
    snap = snapshot() if snap is None else snap
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    body = {"version": MANIFEST_VERSION, "created": time.time(), "root": str(PACKAGE_ROOT),
            "waggle_home": str(config.HOME), "hashing": "sha256, CRLF->LF for text files", "files": snap}
    tmp = GUARD_DIR / f"{MANIFEST_NAME}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, manifest_path())
    _gate_memo.update(at=-1e9, bad=None)
    try:
        (GUARD_DIR / VIOLATION_NAME).unlink()
    except OSError:
        pass
    return body


# ---- command line ------------------------------------------------------------------------------

def _fmt(v):
    return f"{v['kind']:<11} {v['path']}  expected={(v['expected'] or '-')[:12]} actual={(v['actual'] or '-')[:12]}"


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
    out(f"violation record: {'present' if (GUARD_DIR / VIOLATION_NAME).exists() else 'none'}")
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
