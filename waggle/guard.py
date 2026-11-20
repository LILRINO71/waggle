"""Tamper guard: keeps agents from rewriting the files that restrict them.

Three layers share this module's data:
- prevent: `policy.decide` asks `write_hit` / `shell_hit` whether a tool call touches the protected set and, if so,
  turns it into a one-time approval (the user's yes) instead of letting the write-roots rule allow it;
- fail closed: `agent_hook` hashes the files in HOOK_CHECKED against the manifest on every call;
- detect: `verify()` compares the whole protected set with the manifest (for the future core process), and
  `python -m waggle.guard status|verify|approve` is the user's command line.

The protected set is data (REPO_FILES, WAGGLE_HOME_PATHS, USER_HOME_PATHS, HOLDOUT_*). Add an entry there and it is
path-protected, hashed and verified with no other change. There is deliberately no environment variable that moves
the guard folder: an agent could set it. Tests monkeypatch GUARD_DIR / PACKAGE_ROOT instead.
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

GUARD_DIR = Path.home() / ".waggle-guard"
# The checkout this code was imported from, so the live copy is protected wherever it lives.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "manifest.json"
VIOLATION_NAME = "violation.json"

REPO_FILES = (
    "waggle/__init__.py",  # runs before policy on import, so it can patch policy
    "waggle/policy.py", "waggle/security.py", "waggle/agent_hook.py", "waggle/budget.py", "waggle/quota.py",
    "waggle/guard.py", "hermes_plugin/waggle/__init__.py", "tests/test_trust.py", "tests/test_guard.py",
)
# What agent_hook re-hashes on every call (it carries its own copy of this tuple: it must not trust guard.py).
HOOK_CHECKED = ("waggle/__init__.py", "waggle/policy.py", "waggle/agent_hook.py", "waggle/guard.py",
                "waggle/security.py")

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

_ENV_RE = re.compile(r"%([A-Za-z_]\w*)%|\$\{(?:env:)?([A-Za-z_]\w*)\}|\$env:([A-Za-z_]\w*)|\$([A-Za-z_]\w*)", re.I)


def _env(name):
    if name.upper() in ("USERPROFILE", "HOME"):  # Windows has no $HOME, agents use it anyway
        return str(Path.home())
    return os.environ.get(name)


def expand(text):
    """%VAR%, $VAR, ${VAR}, $env:VAR; unknown names stay as written."""
    return _ENV_RE.sub(lambda m: _env(next(g for g in m.groups() if g)) or m.group(0), text)


def _lex(raw, cwd, lower=True):
    """Lexical absolute form: forward slashes, no '..', no trailing dots/spaces, no alternate data stream."""
    s = str(raw).strip().strip("\"'").strip()
    if s == "~" or s[:2] in ("~/", "~\\"):
        s = str(Path.home()) + s[1:]
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
    for h in (Path.home() / ".waggle", config.HOME):
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
        out.append(Entry(kind, Path.home() / rel, "~/" + rel))
    out.append(Entry("dir", GUARD_DIR, "~/" + GUARD_DIR.name))
    return out


def _hit(e, f, ancestors, wild):
    if wild:
        d = f.rsplit("/", 1)[0]
        if e.kind in ("dir", "holdout"):
            return any(d == p or d.startswith(p + "/") or (ancestors and p.startswith(d + "/")) for p in e.forms)
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


def match(raw, cwd=None, ancestors=False, _entries=None):
    """Label of the protected entry `raw` names (resolving ~, env vars, relative paths against cwd, '..', case,
    trailing dots, links), or None. With ancestors=True a folder that contains a protected file counts too."""
    raw = str(raw or "").strip()
    if not raw or len(raw) > 1024 or "\x00" in raw:
        return None
    cwd = str(cwd or os.getcwd())
    s = expand(raw)
    wild = any(c in s for c in "*?")
    ents = _entries if _entries is not None else entries()
    forms = [_lex(s, cwd)]
    for stage in (0, 1):
        for e in ents:
            if any(_hit(e, f, ancestors, wild) for f in forms):
                return e.label
        if stage == 0 and not wild:
            forms = [_lex(_real(_lex(s, cwd, lower=False).replace("/", os.sep)), cwd)]
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


def write_hit(args, cwd=None, patch=False):
    """Reason string when a write/edit call's target is protected, else None."""
    if not isinstance(args, dict):
        return None
    cwd = args.get("cwd") or args.get("workdir") or cwd
    ents = entries()
    cands = [v for k in PATH_KEYS for v in [args.get(k)] if isinstance(v, str)]
    if patch:
        for k in ("patch", "diff", "content", "input"):
            if isinstance(args.get(k), str):
                cands += [a or b for a, b in _PATCH_FILE.findall(args[k])]
    for c in cands:
        label = match(c, cwd, _entries=ents)
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
WRAPPERS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh"}
WRAP_FLAGS = {"-c", "-command", "/c", "/k", "-lc", "-ic"}
PYTHONS = {"python", "python3", "py", "pythonw"}
GUARD_CMD = re.compile(r"waggle[. ]guard\b", re.I)
_REDIRECT = re.compile(r"(?<![<>])\d*>{1,2}\|?\s*(\"[^\"]*\"|'[^']*'|[^\s&|;<>]+)")
_DISTINCTIVE = None


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


def _pieces(tok):
    """A token plus the path-like fragments inside it (python -c "open('x','w')", -Path:x, a=b)."""
    t = re.sub(r"^-[A-Za-z]+[:=]", "", tok)
    found = {tok, t}
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


def _git_dirs(rest):
    return [rest[i + 1] for i, t in enumerate(rest[:-1]) if t in ("-C", "--work-tree")]


def _unresolvable(t):
    return not t or t == "-" or any(c in t for c in "$%`(")


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

    def match(self, piece, cwd, ancestors=False):
        key = (piece, cwd, ancestors)
        if key not in self.memo:
            self.memo[key] = match(piece, cwd, ancestors, _entries=self.ents)
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
        if verb in WRAPPERS:
            idx = next((i for i, t in enumerate(rest) if t.lower() in WRAP_FLAGS), None)
            if idx is not None and depth < 3:
                payload = " ".join(rest[idx + 1:])
                sub = _analyze(_split(payload, True) + _split(payload, False), vcwd or os.getcwd(), ctx, depth + 1)
        is_git = verb == "git"
        if sub is not None:
            read = not sub.nonread
        elif is_git:
            read = _git_sub(rest) in BENIGN_GIT and not any(t.startswith("--output") for t in rest)
        else:
            read = verb in READ_VERBS
        if not read:
            res.nonread = True
        if sub is not None:
            res.merge(sub)
        ancestors = verb in ANCESTOR_VERBS or (is_git and not read) or (verb in COPY_VERBS and any(
            re.match(r"^-(r|R|a|recurse)$|^/[es]$", t) for t in rest))
        names = []
        for tok in tokens:
            for piece in _pieces(tok):
                if len(piece) < 2 or (piece.startswith("-") and "/" not in piece and "\\" not in piece):
                    continue
                if vcwd is not None:
                    label = ctx.match(piece, vcwd, ancestors)
                else:
                    label = None if read else _loose_name(piece)
                if label:
                    names.append(label)
        if is_git and not read:
            base = vcwd or os.getcwd()
            dirs = _git_dirs(rest) + [base]
            if any(_inside_package(_lex(expand(d), base, lower=False)) for d in dirs):
                names.append("the Waggle checkout")
        if names:
            res.named += names
            if not read:
                res.hit = res.hit or names[0]
    return res


def _loose_name(piece):
    """When the working folder is unknown (cd into a variable), a bare protected file name still counts."""
    global _DISTINCTIVE
    if _DISTINCTIVE is None:
        _DISTINCTIVE = ({os.path.basename(r).lower() for r in REPO_FILES}
                        | {n for n, k in WAGGLE_HOME_PATHS if n != "config.json"}
                        | {MANIFEST_NAME, VIOLATION_NAME})
    base = piece.replace("\\", "/").rstrip("/. ").rsplit("/", 1)[-1].lower()
    return base if base in _DISTINCTIVE or base.startswith(HOLDOUT_PREFIX) else None


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
    res = _Result()
    for variant in dict.fromkeys((text, command)):
        for aware in (True, False):
            res.merge(_analyze(_split(variant, aware), cwd, ctx))
    if res.hit:
        return REASON.format(res.hit)
    if res.named and res.nonread:
        return REASON.format(res.named[0])
    if res.nonread:
        low = text.lower().replace("\\", "/")
        if GUARD_DIR.name.lower() in low:
            return REASON.format("~/" + GUARD_DIR.name)
        if ".waggle" in low and re.search(r"approvals\.db|budget\.db|config\.json|holdout|/eval\b", low):
            return REASON.format("a file in the Waggle data folder")
        if re.search(r"\b(import|from)\s+waggle|waggle\.(policy|security|agent_hook|budget|quota|guard)\b", low) \
                and re.search(r"open\(|write_|__file__|shutil|\.replace\(|unlink|\.remove\(|rename|approve", low):
            return REASON.format("Waggle's code, through a script")
    return None


# ---- manifest ----------------------------------------------------------------------------------

def _sha_file(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
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
            snap["user:" + rel] = _sha_file(Path.home() / rel)
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


def verify():
    """Violations over the whole protected set: [{path, kind, expected, actual}], kind = missing | changed | added
    (a new holdout file) | no_manifest. An empty list means clean."""
    m = load_manifest()
    if m is None:
        return [{"path": str(manifest_path()), "kind": "no_manifest", "expected": None, "actual": None}]
    return _diff(m["files"], snapshot())


def write_manifest(snap=None):
    snap = snapshot() if snap is None else snap
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "created": time.time(), "root": str(PACKAGE_ROOT), "files": snap}
    tmp = GUARD_DIR / f"{MANIFEST_NAME}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, manifest_path())
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
