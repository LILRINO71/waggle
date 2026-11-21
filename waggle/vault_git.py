"""Every automatic change Waggle makes to the vault is a git commit you can look at and undo.

The vault gets a local git repository (never pushed anywhere, no remote). Writers (the digest, the learning loop,
learn.py's fact files, the purge, intake and the Drop folder) change notes through `vault.write` / `vault.append`
as before; this module records what they did:

  note_write()    called by vault.write / vault.append just BEFORE a write. If the owner changed that note since the
                  last commit (Obsidian), those edits are committed first as "owner edits", so a restore can never
                  lose them. The path is then marked as pending for the writer (the writer is whatever the
                  `recorded()` decorator says, else the calling module's name).
  commit()        called after a writer finishes (the `recorded()` decorator does it): commits that writer's pending
                  notes, plus a readable snapshot of the facts table (`.waggle/facts.jsonl`), with the message
                  "<writer>: <summary>" and the changed files listed. It never raises and never blocks the writer:
                  a failure is logged to the event ledger (kind "vault_git") and the writer carries on.
  constraints     checked on a Waggle change only (never on the owner's edits): a note over the size cap
                  (`vault_git_max_kb`) or one matching `vault_git_readonly` (globs, e.g. "Me/Profile.md") is
                  refused: the file is put back to its last committed content (or removed, if it is new) and the
                  refusal is logged ("vault_git_refused").
  restore()       puts the files of one commit back to what they were before it, as a NEW commit; history is never
                  rewritten. Owner-edit commits cannot be restored (they are yours).

Commands:  python -m waggle.vault_git log [-n 20] | show <sha> | restore <sha> | status | snapshot | gc
           /waggle memory   and   /waggle memory restore <sha>   in the owner's Telegram chat (engine_commands.py)

Off switch: "vault_git": false in ~/.waggle/config.json (the history stays in <vault>/.git until you delete it).

Pending paths are kept in <vault>/.git/waggle-pending.json, so a write made by one process (the dashboard) is
committed with the right name by another (the core). git is always started without a window (CREATE_NO_WINDOW).
Design after nanobot's Dream versioning (MIT) and letta-code's memory constraints (Apache-2.0); no code copied.
"""
import contextvars
import fnmatch
import functools
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import events, vault
from .config import Settings, data_path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FACTS_REL = ".waggle/facts.jsonl"
TRAILER = "Waggle-Writer"
OWNER = "owner"
STALE_PENDING_S = 600.0  # a pending note nobody committed this long is committed under its own writer's name
OWNER_FILE_LIMIT = 8 * 1024 * 1024  # an owner file bigger than this is left out of the history, not committed
GIT_TIMEOUT_S = 40
LOCK_WAIT_S = 8.0
LOCK_STALE_S = 90.0

GITIGNORE = """# Written by Waggle (waggle/vault_git.py). Edit freely: this file is only created when it is missing.
# Files waiting to be filed and the originals that were dropped stay out of the history.
Drop/
Sources/_files/
# Obsidian's window state and caches change constantly
.obsidian/workspace*.json
.obsidian/cache
.trash/
# half-written temp files
*.tmp
.*.tmp
.waggle/*.lock
# big binaries
*.pdf
*.png
*.jpg
*.jpeg
*.gif
*.webp
*.heic
*.bmp
*.tif
*.tiff
*.mp3
*.wav
*.m4a
*.ogg
*.flac
*.mp4
*.mov
*.mkv
*.avi
*.webm
*.zip
*.7z
*.rar
*.gz
*.exe
*.dll
*.iso
*.glb
*.step
*.stp
*.stl
*.docx
*.pptx
*.xlsx
"""

_TLOCK = threading.RLock()
_HELD = {}
_WRITER = contextvars.ContextVar("vault_git_writer", default=None)
_READY = set()  # vaults whose repository this process has checked
_ROOTS = set()  # vault roots this process wrote to (commit(None, ...) with no settings looks at these too)
REFUSED = []  # (rel, reason) refused in this process, newest last (tests and the doctor read it)
LAST_ERROR = [""]


# ---- settings ---------------------------------------------------------------------------------------------

def enabled(s=None):
    s = s or Settings.load()
    return bool(getattr(s, "vault_git", True))


def _max_bytes(s):
    try:
        return max(1, int(float(getattr(s, "vault_git_max_kb", 512)) * 1024))
    except (TypeError, ValueError):
        return 512 * 1024


def _readonly_globs(s):
    g = getattr(s, "vault_git_readonly", None) or []
    return [str(x).strip().replace("\\", "/").lstrip("/") for x in g if str(x).strip()]


def _oops(where, err):
    msg = f"{where}: {type(err).__name__}: {str(err)[:200]}" if isinstance(err, BaseException) else f"{where}: {err}"
    LAST_ERROR[0] = msg
    try:
        events.log("vault_git", action="error", error=msg)
    except Exception:  # noqa: BLE001  (the ledger must never be the thing that breaks a writer)
        pass


# ---- git --------------------------------------------------------------------------------------------------

class Out:
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err

    @property
    def ok(self):
        return self.rc == 0


def _git(root, *args, input=None, timeout=GIT_TIMEOUT_S):
    """Run git in the vault with no window, a fixed identity and no end-of-line rewriting. Never raises."""
    cmd = ["git", "--literal-pathspecs", "-c", "core.quotepath=off", "-c", "core.autocrlf=false",
           "-c", "core.safecrlf=false", "-c", "user.name=Waggle", "-c", "user.email=waggle@localhost",
           "-c", "commit.gpgsign=false", "-c", "i18n.commitEncoding=utf-8", "-C", str(root), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(cmd, input=None if input is None else input.encode("utf-8"), capture_output=True,
                           timeout=timeout, creationflags=NO_WINDOW, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return Out(127, "", f"{type(e).__name__}: {e}")
    return Out(r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"))


@contextmanager
def _lock(root):
    """One writer at a time per vault, across threads and processes (a lock file inside .git). Yields False when
    the lock could not be had in LOCK_WAIT_S: the caller skips its commit (the change stays pending)."""
    key = str(root)
    with _TLOCK:
        if _HELD.get(key):
            yield True
            return
        path = Path(root) / ".git" / "waggle.lock"
        fd, t0 = None, time.time()
        while True:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > LOCK_STALE_S:
                        path.unlink()
                        continue
                except OSError:
                    pass
            except OSError:
                break
            if time.time() - t0 > LOCK_WAIT_S:
                break
            time.sleep(0.05)
        if fd is None:
            yield False
            return
        _HELD[key] = True
        try:
            yield True
        finally:
            _HELD.pop(key, None)
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                path.unlink()
            except OSError:
                pass


# ---- paths ------------------------------------------------------------------------------------------------

def _root(s):
    return Path(vault.root(s))


def _rel(root, path):
    """Vault-relative posix path, or None when the path is not inside the vault."""
    try:
        p = Path(path)
        if not p.is_absolute():
            return None if ".." in p.parts else p.as_posix()
        return p.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _caller():
    """The module (without package) of the first frame outside vault.py / vault_git.py."""
    f = sys._getframe(1)
    while f:
        mod = f.f_globals.get("__name__", "")
        if mod not in ("waggle.vault", "waggle.vault_git", "vault", "vault_git") and not mod.startswith("contextlib"):
            return mod.rsplit(".", 1)[-1] or "waggle"
        f = f.f_back
    return "waggle"


def _pending_path(root):
    return Path(root) / ".git" / "waggle-pending.json"


def _load_pending(root):
    try:
        d = json.loads(_pending_path(root).read_text(encoding="utf-8"))
        return {k: v for k, v in d.items() if isinstance(v, list) and len(v) == 2} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pending(root, pend):
    try:
        _pending_path(root).write_text(json.dumps(pend), encoding="utf-8")
    except OSError as e:
        _oops("pending", e)


# ---- the repository --------------------------------------------------------------------------------------

def _repo(root):
    return (Path(root) / ".git" / "HEAD").exists()


def has_repo(s=None):
    return _repo(_root(s or Settings.load()))


def _dirty(root, rels):
    """[(XY, path)] for the given paths that differ from HEAD (new, changed or deleted; ignored files never show)."""
    out, batch, size = [], [], 0

    def flush():
        if not batch:
            return
        r = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", "--", *batch)
        for item in r.out.split("\0"):
            if len(item) > 3 and item[2] == " ":
                out.append((item[:2], item[3:]))
        batch.clear()

    for rel in rels:
        batch.append(rel)
        size += len(rel) + 1
        if size > 6000:
            flush()
            size = 0
    flush()
    return out


def _dirty_all(root):
    r = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")
    return [(i[:2], i[3:]) for i in r.out.split("\0") if len(i) > 3 and i[2] == " "]


def _stage(root, rels):
    if not rels:
        return True
    return _git(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(rels)).ok


def _commit_staged(root, subject, writer, names):
    """Commit what is staged. Returns the short sha, or None when nothing was staged."""
    if _git(root, "diff", "--cached", "--quiet").rc == 0:
        return None
    body = "\n".join(names[:60]) + (f"\n... and {len(names) - 60} more" if len(names) > 60 else "")
    msg = f"{subject[:110]}\n\n{body}\n\n{TRAILER}: {writer}\n"
    r = _git(root, "commit", "-q", "-F", "-", input=msg)
    if not r.ok:
        _oops("commit", r.err.strip() or r.out.strip())
        return None
    return _git(root, "rev-parse", "--short", "HEAD").out.strip() or None


def _staged_names(root):
    r = _git(root, "diff", "--cached", "--name-status", "--no-renames", "-z")
    parts = r.out.split("\0")
    return [f"{parts[i]} {parts[i + 1]}" for i in range(0, len(parts) - 1, 2) if parts[i]]


def _ensure(root, s):
    """Create the repository on first use (idempotent): .gitignore, a facts snapshot and one commit of what the
    vault holds now, written as the owner's (everything in it is theirs until Waggle changes it)."""
    key = str(root)
    if key in _READY and _repo(root):
        return True
    if _repo(root) and _git(root, "rev-parse", "--verify", "-q", "HEAD").ok:
        _READY.add(key)
        return True
    if not _repo(root):
        r = _git(root, "init", "-q")
        if not r.ok:
            _oops("init", r.err)
            return False
        _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    ignore = Path(root) / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text(GITIGNORE, encoding="utf-8")
        except OSError as e:
            _oops("gitignore", e)
    _export_facts(root, s)
    rels = _owner_candidates(root, _dirty_all(root), set())
    _stage(root, rels)
    names = _staged_names(root)
    if names:
        _commit_staged(root, f"{OWNER} edits: first snapshot of the vault ({len(names)} files)", OWNER, names)
    else:
        _git(root, "commit", "-q", "--allow-empty", "-F", "-",
             input=f"{OWNER} edits: first snapshot of the vault (empty)\n\n{TRAILER}: {OWNER}\n")
    ok = _git(root, "rev-parse", "--verify", "-q", "HEAD").ok
    if ok:
        _READY.add(key)
    return ok


def _owner_candidates(root, dirty, exclude):
    out = []
    for xy, p in dirty:
        if p in exclude or p == FACTS_REL:
            continue
        try:
            if (Path(root) / p).stat().st_size > OWNER_FILE_LIMIT:
                continue
        except OSError:
            pass
        out.append(p)
    return out


def _owner_commit(root, rels):
    if not rels:
        return None
    _stage(root, rels)
    names = _staged_names(root)
    if not names:
        return None
    files = [n.split(" ", 1)[1] for n in names]
    what = files[0] if len(files) == 1 else f"{len(files)} files ({', '.join(files[:3])}{', ...' if len(files) > 3 else ''})"
    return _commit_staged(root, f"{OWNER} edits: {what}", OWNER, names)


def checkpoint(settings=None):
    """Commit everything the owner changed (and Waggle has not marked as its own) as "owner edits". Call before a
    writer that does not go through vault.write (the purge). Returns the short sha or None. Never raises."""
    try:
        s = settings or Settings.load()
        if not enabled(s) or not _root(s).is_dir():
            return None
        root = _root(s)
        _ROOTS.add(str(root))
        with _lock(root) as got:
            if not got or not _ensure(root, s):
                return None
            return _owner_commit(root, _owner_candidates(root, _dirty_all(root), set(_load_pending(root))))
    except Exception as e:  # noqa: BLE001
        _oops("checkpoint", e)
        return None


snapshot_owner = checkpoint


def note_write(path, settings=None):
    """vault.write / vault.append call this before they touch a note. Never raises."""
    try:
        s = settings or Settings.load()
        if not enabled(s):
            return
        root = _root(s)
        if not root.is_dir():
            return
        rel = _rel(root, path)
        if not rel or rel == FACTS_REL:
            return
        who = _WRITER.get() or _caller()
        _ROOTS.add(str(root))
        with _lock(root) as got:
            if not got or not _ensure(root, s):
                return
            pend = _load_pending(root)
            if rel not in pend:
                dirty = _dirty(root, [rel])
                if dirty:
                    _owner_commit(root, [p for _, p in dirty])
            pend[rel] = [who, time.time()]
            _save_pending(root, pend)
    except Exception as e:  # noqa: BLE001
        _oops("note_write", e)


# ---- facts snapshot ---------------------------------------------------------------------------------------

def _export_facts(root, s=None):
    """Write the facts table as one JSON object per line to <vault>/.waggle/facts.jsonl (sorted, so a git diff
    shows exactly which fact was added, replaced or closed). True when the file changed."""
    db = data_path("knowledge.db")
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute("SELECT * FROM facts ORDER BY id")]
        finally:
            con.close()
    except sqlite3.Error:
        return False
    target = Path(root) / FACTS_REL
    if not rows and not target.exists():
        return False
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows)
    data = text.encode("utf-8")
    try:
        if target.exists() and target.read_bytes() == data:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return True
    except OSError as e:
        _oops("facts", e)
        return False


# ---- constraints ------------------------------------------------------------------------------------------

def _violation(root, rel, xy, s):
    """Why a Waggle change to this note is refused, or ''."""
    low = rel.lower()
    for pat in _readonly_globs(s):
        p = pat.lower()
        if fnmatch.fnmatchcase(low, p) or ("/" not in p and fnmatch.fnmatchcase(low.rsplit("/", 1)[-1], p)) \
                or (p.endswith("/") and low.startswith(p)):
            return f"read-only ({pat})"
    if "D" not in xy:
        try:
            size = (Path(root) / rel).stat().st_size
        except OSError:
            return ""
        if size > _max_bytes(s):
            return f"over the size cap ({size // 1024} KB > {_max_bytes(s) // 1024} KB)"
    return ""


def _revert_file(root, rel):
    """Put one note back to its last committed content (a new note is removed)."""
    if _git(root, "cat-file", "-e", f"HEAD:{rel}").ok:
        return _git(root, "checkout", "HEAD", "--", rel).ok
    try:
        (Path(root) / rel).unlink()
        return True
    except OSError:
        return False


def _refuse(root, rel, why, writer):
    ok = _revert_file(root, rel)
    REFUSED.append((rel, why))
    del REFUSED[:-50]
    try:
        events.log("vault_git_refused", path=rel, reason=why, writer=writer, reverted=ok)
    except Exception:  # noqa: BLE001
        pass


# ---- committing ------------------------------------------------------------------------------------------

def _roots_for(settings):
    if settings is not None:
        return [_root(settings)]
    roots = [Path(r) for r in _ROOTS]
    try:
        r = _root(Settings.load())
        if r not in roots:
            roots.append(r)
    except Exception:  # noqa: BLE001
        pass
    return [r for r in roots if r.is_dir()]


def commit(paths, writer, summary, settings=None):
    """Commit a writer's change to the vault. `paths`: notes (vault-relative or absolute), or None for every note
    marked pending for `writer` (what `recorded()` uses). Returns the short sha, or None (nothing changed, the
    feature is off, or the change was refused). Never raises."""
    sha = None
    try:
        s = settings or Settings.load()
        if not enabled(s):
            return None
        for root in _roots_for(settings):
            got = _commit_root(root, s, paths, writer, summary)
            sha = sha or got
    except Exception as e:  # noqa: BLE001
        _oops("commit", e)
    return sha


def _commit_root(root, s, paths, writer, summary):
    with _lock(root) as got:
        if not got:
            _oops("commit", "lock busy; the change stays pending")
            return None
        if not _ensure(root, s):
            return None
        pend = _load_pending(root)
        if paths is None:
            mine = sorted(r for r, (w, _) in pend.items() if w == writer)
        else:
            mine = sorted({r for r in (_rel(root, p) for p in paths) if r})
        facts_changed = _export_facts(root, s)
        stale = [r for r, (w, t) in pend.items() if time.time() - t > STALE_PENDING_S and r not in mine]
        if not mine and not facts_changed and not stale:
            return None
        _owner_commit(root, _owner_candidates(root, _dirty_all(root), set(pend) | set(mine)))
        sha = _commit_group(root, s, mine, writer, summary, facts=True)
        for r in mine:
            pend.pop(r, None)
        # notes some other writer left pending for a while (nobody called commit for them): commit them as theirs
        by = {}
        for r in stale:
            by.setdefault(pend[r][0], []).append(r)
        for w, rels in by.items():
            _commit_group(root, s, rels, w, f"{len(rels)} note(s) written outside a recorded run", facts=False)
            for r in rels:
                pend.pop(r, None)
        _save_pending(root, pend)
        return sha


def _commit_group(root, s, rels, writer, summary, facts):
    dirty = _dirty(root, rels) if rels else []
    keep = []
    for xy, rel in dirty:
        why = _violation(root, rel, xy, s)
        if why:
            _refuse(root, rel, why, writer)
        else:
            keep.append(rel)
    if facts and _dirty(root, [FACTS_REL]):
        keep.append(FACTS_REL)
    if not keep:
        return None
    _stage(root, keep)
    names = _staged_names(root)
    return _commit_staged(root, f"{writer}: {summary}", writer, names)


def recorded(writer, summary=None):
    """Decorator: whatever the function writes to the vault (through vault.write / vault.append) is committed when
    it returns, named for `writer`. `summary` is a string or a callable (result, args, kwargs) -> str. Inside
    another recorded function the outer writer's name is used and each inner call still commits on its own, so
    one learning-loop pass leaves one commit per fact."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            outer = _WRITER.get()
            tok = _WRITER.set(outer or writer)
            result = None
            try:
                result = fn(*a, **k)
                return result
            finally:
                _WRITER.reset(tok)
                try:
                    text = summary(result, a, k) if callable(summary) else summary
                except Exception:  # noqa: BLE001
                    text = None
                commit(None, outer or writer, str(text or fn.__name__.replace("_", " ")))
        return wrapper
    return deco


# ---- reading history -------------------------------------------------------------------------------------

def _writer_of(subject, body=""):
    m = re.search(rf"^{TRAILER}: (.+)$", body or "", re.M)
    if m:
        return m.group(1).strip()
    return subject.split(":", 1)[0].strip() if ":" in subject else ""


def log(n=20, settings=None):
    """Newest first: [{sha, time, writer, subject, files}]."""
    s = settings or Settings.load()
    root = _root(s)
    if not _repo(root):
        return []
    r = _git(root, "log", f"-n{max(1, int(n))}", "--no-renames", "--name-only",
             "--format=%x01%h%x1f%ct%x1f%s%x1f%b%x02")
    rows = []
    for chunk in r.out.split("\x01")[1:]:
        head, _, files = chunk.partition("\x02")
        sha, ts, subject, body = (head.split("\x1f") + ["", "", "", ""])[:4]
        rows.append({"sha": sha, "time": float(ts or 0), "writer": _writer_of(subject, body), "subject": subject,
                     "files": [f for f in files.splitlines() if f.strip()]})
    return rows


def _resolve(root, sha):
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha or ""):
        return None
    r = _git(root, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}")
    return r.out.strip() if r.ok else None


def show(sha, settings=None, max_diff=3000):
    s = settings or Settings.load()
    root = _root(s)
    full = _resolve(root, sha) if _repo(root) else None
    if not full:
        return None
    meta = _git(root, "log", "-1", "--format=%h%x1f%ct%x1f%s%x1f%b", full).out.split("\x1f")
    stat = _git(root, "show", "--no-renames", "--numstat", "--format=", full).out
    files = []
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files.append({"added": parts[0], "removed": parts[1], "path": parts[2]})
    diff = _git(root, "show", "--no-renames", "--format=", "--unified=1", full).out
    return {"sha": meta[0], "time": float(meta[1] or 0), "subject": meta[2], "writer": _writer_of(meta[2], meta[3]),
            "files": files, "diff": diff[:max_diff] + ("\n..." if len(diff) > max_diff else "")}


def status(settings=None):
    s = settings or Settings.load()
    root = _root(s)
    d = {"enabled": enabled(s), "vault": str(root), "repo": _repo(root),
         "readonly": _readonly_globs(s), "max_kb": _max_bytes(s) // 1024}
    if d["repo"]:
        d["commits"] = int(_git(root, "rev-list", "--count", "HEAD").out.strip() or 0)
        d["pending"] = len(_load_pending(root))
        d["uncommitted"] = len(_dirty_all(root))
    return d


# ---- restore ---------------------------------------------------------------------------------------------

def restore(sha, settings=None):
    """Put the notes one commit changed back to what they were before it, as a new commit. Returns a dict:
    {ok, message, sha (the new commit), files, later (notes a later commit also changed: those later changes
    are undone too)}. Never raises; never rewrites history. Facts in knowledge.db are not rolled back (the
    snapshot file is left alone and the next commit exports the database again)."""
    try:
        s = settings or Settings.load()
        root = _root(s)
        if not enabled(s):
            return {"ok": False, "message": "Memory history is off (vault_git is false)."}
        if not _repo(root):
            return {"ok": False, "message": "There is no memory history yet."}
        full = _resolve(root, sha)
        if not full:
            return {"ok": False, "message": f"I don't know the change {str(sha)[:12]}."}
        with _lock(root) as got:
            if not got:
                return {"ok": False, "message": "The history is busy; try again in a minute."}
            parents = _git(root, "rev-list", "--parents", "-n1", full).out.split()[1:]
            meta = _git(root, "log", "-1", "--format=%h%x1f%s%x1f%b", full).out.split("\x1f")
            short, subject, body = meta[0], meta[1], meta[2] if len(meta) > 2 else ""
            who = _writer_of(subject, body)
            if len(parents) != 1:
                return {"ok": False, "message": f"{short} is the first snapshot or a merge; there is nothing before it."}
            if who == OWNER:
                return {"ok": False, "message": f"{short} is your own edit ({subject}); I never undo those."}
            parent = parents[0]
            _owner_commit(root, _owner_candidates(root, _dirty_all(root), set(_load_pending(root))))
            ns = _git(root, "diff-tree", "--no-commit-id", "--name-status", "--no-renames", "-r", "-z", full).out.split("\0")
            changes = [(ns[i], ns[i + 1]) for i in range(0, len(ns) - 1, 2) if ns[i]]
            changes = [(st, p) for st, p in changes if p != FACTS_REL]
            if not changes:
                return {"ok": False, "message": f"{short} changed no notes, only the facts snapshot."}
            later = [p for _, p in changes if _git(root, "rev-list", "--count", f"{full}..HEAD", "--", p).out.strip() not in ("", "0")]
            back = [p for st, p in changes if st != "A"]
            gone = [p for st, p in changes if st == "A"]
            if back:
                r = _git(root, "checkout", parent, "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(back))
                if not r.ok:
                    return {"ok": False, "message": f"Could not restore: {r.err.strip()[:150]}"}
            for p in gone:
                try:
                    (Path(root) / p).unlink()
                except OSError:
                    pass
            _stage(root, back + gone)
            files = [p for _, p in changes]
            names = _staged_names(root)
            new = _commit_staged(root, f"restore: undid {who}'s change {short} ({subject.split(':', 1)[-1].strip()[:60]})",
                                 "restore", names)
            if not new:
                return {"ok": True, "message": f"Nothing to undo: those notes already look as they did before {short}.",
                        "sha": None, "files": files, "later": later}
            msg = f"Restored {len(files)} note(s) to before {short} ({who}: {subject.split(':', 1)[-1].strip()[:60]}). New commit {new}."
            if later:
                msg += f" Changes made to {', '.join(later[:3])} after {short} were undone too."
            return {"ok": True, "message": msg, "sha": new, "files": files, "later": later}
    except Exception as e:  # noqa: BLE001
        _oops("restore", e)
        return {"ok": False, "message": f"The restore failed: {type(e).__name__}: {str(e)[:120]}"}


# ---- text for the owner -----------------------------------------------------------------------------------

def _when(ts):
    return time.strftime("%a %d %b %H:%M", time.localtime(ts)) if ts else "?"


def format_log(rows):
    if not rows:
        return "No memory history yet."
    return "\n".join(f"{r['sha']}  {_when(r['time'])}  {r['subject'][:90]}" for r in rows)


def format_show(d, diff=True):
    if not d:
        return "I don't know that change."
    lines = [f"{d['sha']}  {_when(d['time'])}", d["subject"], ""]
    lines += [f"  +{f['added']} -{f['removed']}  {f['path']}" for f in d["files"][:30]]
    if diff and d["diff"].strip():
        lines += ["", d["diff"].rstrip()]
    return "\n".join(lines)


def gc(settings=None):
    """git gc --auto (git also runs it after commits by itself; this is for a manual tidy)."""
    s = settings or Settings.load()
    root = _root(s)
    if not _repo(root):
        return "no repository"
    with _lock(root) as got:
        if not got:
            return "busy"
        r = _git(root, "gc", "--auto", "--quiet", timeout=120)
        return "ok" if r.ok else r.err.strip()[:200]


# ---- command line -----------------------------------------------------------------------------------------

def main(argv=None):
    import argparse
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m waggle.vault_git", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("log", help="recent changes, newest first")
    p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("show", help="what one change did")
    p.add_argument("sha")
    p = sub.add_parser("restore", help="undo one change (as a new commit)")
    p.add_argument("sha")
    sub.add_parser("status", help="repository state and constraints")
    sub.add_parser("snapshot", help="commit your own edits now")
    sub.add_parser("gc", help="git gc --auto")
    a = ap.parse_args(argv)
    if a.cmd == "log":
        print(format_log(log(a.n)))
    elif a.cmd == "show":
        print(format_show(show(a.sha)))
    elif a.cmd == "restore":
        r = restore(a.sha)
        print(r["message"])
        return 0 if r["ok"] else 1
    elif a.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif a.cmd == "snapshot":
        print(checkpoint() or "nothing to commit")
    elif a.cmd == "gc":
        print(gc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
