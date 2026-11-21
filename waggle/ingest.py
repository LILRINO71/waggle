"""Knowledge intake: read the user's real world into the vault, incrementally and idempotently.

Sources (each a plain idempotent function; run_due() decides what is due from knowledge.db state):
- mirror_claude_memory   Claude Code's memory notes about the user -> Knowledge/Claude Code/ (copied as they are)
- sync_repos             every git repo under watch_roots (+ extra_repos): README, docs headings, new commits ->
                         a marked block in the project note (created if missing), Log lines, tasks ticked by commits
- sync_github            open PRs / issues and the latest CI run per repo (gh CLI) -> the same block
- sync_upcoming          Calendar events and mail waiting for a reply (claude.ai connectors, background budget,
                         at most twice a day) -> facts store -> Knowledge/Upcoming.md
- sync_goals             one note per standing goal (from Profile -> Goals) with linked projects and progress
Then the conversation digest (digest.py), the daily learning loop (learning.py), graph refresh and reindex.

Ingested content only ever lands in clearly marked places: the Knowledge/ folder, Goals/ notes Waggle created, and
blocks between `<!-- waggle:... start/end -->` markers (plus dated Log lines). Hand-written text is never
rewritten. A mirror note the user edited in Obsidian is archived (Archive/), never overwritten silently.

`python -m waggle.ingest` runs whatever is due and prints nothing (Hermes sends cron stdout to the phone).
`python -m waggle.ingest run [job ...]` forces jobs; `status` shows state; `cleanup [--apply]` / `restore <id>` is
the one-time, reversible vault cleanup (moves to Archive/, never deletes)."""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import events, kstore, projects, vault
from .config import CONFIG_FILE, Settings, data_path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash when run under pythonw

MIRROR_DIR = "Knowledge/Claude Code"
UPCOMING_NOTE = "Knowledge/Upcoming"
ARCHIVE = "Archive"
GENERATED = {"node_modules", "dist", "build", "out", ".venv", "venv", "env", "__pycache__", ".git", "target",
             ".next", ".cache", "coverage", "site-packages", ".pytest_cache", "vendor"}
DORMANT_DAYS = 60


def _extra_config():
    """Optional ingest keys in ~/.waggle/config.json (Settings ignores unknown keys):
    claude_memory_dirs: [paths], ingest_skip_repos: [names]."""
    try:
        return json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _norm(text):
    return (text or "").replace("\r\n", "\n")


def _hash(text):
    return hashlib.sha1(_norm(text).encode("utf-8", "replace")).hexdigest()[:16]


def _now(now=None):
    return now or time.time()


def _stamp(now=None):
    return dt.datetime.fromtimestamp(_now(now)).strftime("%Y-%m-%d %H:%M")


# ---- marked blocks ---------------------------------------------------------------------------------

def set_block(text, block, content, heading=None, before="## Log"):
    """Replace the content between `<!-- waggle:{block} start -->` and `... end -->`, or add the block (under
    `heading`, placed before `before` when that section exists). Everything outside the markers is kept as is."""
    start, end = f"<!-- waggle:{block} start -->", f"<!-- waggle:{block} end -->"
    body = f"{start}\n{content.strip()}\n{end}"
    if start in text and end in text:
        a, b = text.index(start), text.index(end) + len(end)
        return text[:a] + body + text[b:]
    chunk = (f"## {heading}\n" if heading else "") + body + "\n"
    if before and re.search(rf"^{re.escape(before)}\s*$", text, re.M):
        m = re.search(rf"^{re.escape(before)}\s*$", text, re.M)
        return text[:m.start()] + chunk + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + chunk


def get_block(text, block):
    m = re.search(rf"<!-- waggle:{re.escape(block)} start -->\n(.*?)\n<!-- waggle:{re.escape(block)} end -->",
                  text or "", re.S)
    return m.group(1) if m else None


def _set_frontmatter(text, key, value):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    line = f"{key}: {value}"
    if not m:
        return f"---\n{line}\n---\n" + text
    fm = m.group(1)
    if re.search(rf"^{re.escape(key)}:", fm, re.M):
        fm = re.sub(rf"^{re.escape(key)}:.*$", line, fm, count=1, flags=re.M)
    else:
        fm += "\n" + line
    return f"---\n{fm}\n---\n" + text[m.end():]


def _archive(rel, s, why, now=None):
    """Move a vault note into Archive/<why>/ (kept, out of the index). Returns the new vault-relative path."""
    src = vault.safe_path(rel, s)
    if not src.exists():
        return None
    stamp = dt.datetime.fromtimestamp(_now(now)).strftime("%Y%m%d-%H%M%S")
    dst = vault.safe_path(f"{ARCHIVE}/{why}/{src.stem}-{stamp}.md", s)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst.relative_to(vault.root(s)).as_posix()


# ---- 1a. Claude Code memory mirror -----------------------------------------------------------------

def claude_project_slug(path):
    """Claude Code's folder name for a project directory: C:\\Users\\me\\Claude -> C--Users-me-Claude."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def memory_dirs(settings=None):
    s = settings or Settings.load()
    extra = _extra_config().get("claude_memory_dirs")
    if extra:
        return [Path(p) for p in extra if Path(p).is_dir()]
    base = Path.home() / ".claude" / "projects"
    return [base / claude_project_slug(r) / "memory" for r in s.watch_roots
            if (base / claude_project_slug(r) / "memory").is_dir()]


MIRROR_README = """---
waggle_index: false
---
# Claude Code memory (mirror)

Copied automatically by Waggle's ingest job from Claude Code's memory folder, so Waggle knows what Claude Code
knows about you. Edit the originals through Claude Code; if you edit a copy here, Waggle keeps your version in
Archive/ before refreshing it.
"""


def mirror_claude_memory(settings=None, dirs=None, now=None):
    """Copy each memory note into Knowledge/Claude Code/ when it is new or changed. Returns counts."""
    s = settings or Settings.load()
    dirs = memory_dirs(s) if dirs is None else [Path(d) for d in dirs]
    state = kstore.get("ingest.memory", {})
    out = {"sources": len(dirs), "notes": 0, "added": 0, "updated": 0, "unchanged": 0, "removed": 0,
           "kept_user_edit": 0}
    seen = set()
    for i, d in enumerate(dirs):
        target = MIRROR_DIR if i == 0 else f"{MIRROR_DIR}/{d.parent.name[-40:]}"
        for src in sorted(d.glob("*.md")):
            rel = f"{target}/{src.name}"
            seen.add(rel)
            out["notes"] += 1
            text = src.read_text(encoding="utf-8", errors="replace")
            h = _hash(text)
            prev = state.get(rel, {})
            dest = vault.safe_path(rel, s)
            mirror_now = dest.read_text(encoding="utf-8", errors="replace") if dest.exists() else None
            if mirror_now is not None and prev.get("src") == h and _hash(mirror_now) == prev.get("out"):
                out["unchanged"] += 1
                continue
            if mirror_now is not None and _hash(mirror_now) == h:  # already identical (e.g. state lost)
                state[rel] = {"src": h, "out": h}
                out["unchanged"] += 1
                continue
            if mirror_now is not None and prev.get("src") == h:
                out["unchanged"] += 1  # the user edited the copy and the original did not change: leave theirs
                continue
            if mirror_now is not None and _hash(mirror_now) != prev.get("out"):
                _archive(rel, s, "Claude Code edits", now)  # their edit is kept, then the copy is refreshed
                out["kept_user_edit"] += 1
            vault.write(rel, _norm(text), s)
            state[rel] = {"src": h, "out": h}
            out["updated" if mirror_now is not None else "added"] += 1
    for rel in [r for r in state if r not in seen]:
        if _archive(rel, s, "Claude Code removed", now):
            out["removed"] += 1
        state.pop(rel, None)
    if dirs and not vault.safe_path(f"{MIRROR_DIR}/README", s).exists():
        vault.write(f"{MIRROR_DIR}/README", MIRROR_README, s)
    kstore.put("ingest.memory", state)
    return out


def memory_notes_mentioning(names, settings=None):
    """Mirrored memory notes (stems) whose text mentions any of `names` (repo / project names, aliases)."""
    s = settings or Settings.load()
    folder = vault.root(s) / MIRROR_DIR
    pats = [re.compile(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])") for n in names if n and len(n) >= 4]
    hits = []
    for p in sorted(folder.glob("*.md")) if folder.exists() else []:
        if p.stem in ("README", "MEMORY"):
            continue
        low = p.read_text(encoding="utf-8", errors="replace").lower()
        if any(pt.search(low) for pt in pats):
            hits.append(p.stem)
    return hits


# ---- 1b. project repos -----------------------------------------------------------------------------

def git(args, cwd, timeout=30):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return r.stdout


def discover_repos(settings=None):
    """Git repos (a real .git folder, so worktrees are skipped) directly under each watch root, plus extra_repos."""
    s = settings or Settings.load()
    skip = set(_extra_config().get("ingest_skip_repos") or [])
    found, seen = [], set()
    cands = []
    for root in s.watch_roots:
        r = Path(root)
        if not r.is_dir():
            continue
        cands.append(r)
        cands += sorted(p for p in r.iterdir() if p.is_dir() and p.name not in GENERATED
                        and not p.name.startswith("."))
    cands += [Path(p) for p in s.extra_repos]
    for p in cands:
        if (p / ".git").is_dir() and p.name not in skip:
            key = str(p.resolve()).lower()
            if key not in seen:
                seen.add(key)
                found.append(p)
    return found


def _readme(path):
    for name in ("README.md", "readme.md", "Readme.md", "README.MD", "README"):
        p = path / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("# ")), "")
            paras = [x.strip() for x in re.split(r"\n\s*\n", text)]
            body = next((x for x in paras if x and not x.startswith(("#", "!", "[![", "<", "```", "|", "---"))
                         and len(x) > 30), "")
            body = re.sub(r"\s+", " ", re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)).strip()
            return title, body[:500]
    return "", ""


def _docs(path, limit=12):
    d = path / "docs"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*.md")):
        if any(part in GENERATED for part in p.relative_to(d).parts) or len(p.relative_to(d).parts) > 2:
            continue
        try:
            first = next((l.lstrip("# ").strip() for l in p.read_text(encoding="utf-8", errors="replace")
                          .splitlines() if l.startswith("# ")), "")
        except OSError:
            continue
        out.append(first or p.stem)
        if len(out) >= limit:
            break
    return out


def _remote(path, runner):
    try:
        url = runner(["remote", "get-url", "origin"], path).strip()
    except Exception:
        return "", ""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return url, (f"{m.group(1)}/{m.group(2)}" if m else "")


def _commits(path, runner, n=40):
    try:
        out = runner(["log", "--branches", f"-n{n}", "--date=short", "--format=%H%x1f%ad%x1f%s"], path)
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            rows.append({"sha": parts[0], "date": parts[1], "subject": parts[2].strip()})
    return rows


def _title(name):
    return " ".join(w if w.isupper() else w.capitalize() for w in re.split(r"[-_ ]+", name) if w)


def match_project(repo_name, settings=None, readme_title=""):
    """The project note for a repo: frontmatter `repo:`, then same slug as the note name, then an alias,
    then the repo name written in the note. None when there is none."""
    s = settings or Settings.load()
    low = repo_name.lower()
    allp = projects.all_projects(s)
    texts = {p["name"]: vault.read(f"Projects/{p['name']}", s) for p in allp}
    for p in allp:
        meta = projects.parse_meta(texts[p["name"]])
        repos = meta.get("repo", [])
        repos = repos if isinstance(repos, list) else [repos]
        if low in [str(r).lower() for r in repos]:
            return p["name"]
    for p in allp:
        if vault.slug(p["name"]) == low or (readme_title and p["name"].lower() == readme_title.lower()):
            return p["name"]
    for p in allp:
        if low in [str(a).lower() for a in p["aliases"]]:
            return p["name"]
    pat = re.compile(rf"(?<![\w-]){re.escape(low)}(?![\w-])")
    hits = [p["name"] for p in allp if pat.search(texts[p["name"]].lower())]
    return hits[0] if len(hits) == 1 else None


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())} - {
        "the", "and", "for", "with", "add", "fix", "update", "from", "into", "this", "that", "make", "use"}


def task_done_by(task, subjects):
    """The commit subject that finishes a task, if one clearly does (most of the task's words, at least 2)."""
    tw = _words(task)
    if len(tw) < 2:
        return None
    for subj in subjects:
        sw = _words(subj)
        if len(tw & sw) >= 2 and len(tw & sw) / len(tw) >= 0.6:
            return subj
    return None


def render_repo_block(info, gh=None, links=None):
    lines = [f"_Kept current by Waggle from the `{info['name']}` repo (last checked {info['checked']}). "
             "Edit outside this block; text inside it is rewritten._", ""]
    if info.get("about"):
        lines.append(f"**What it is:** {info['about']}")
    if info.get("docs"):
        lines.append("**Docs:** " + "; ".join(info["docs"]))
    if info.get("last_commit"):
        lines.append(f"**Last commit:** {info['last_commit']}" + (f" · {info['commits_30d']} commits in 30 days"
                                                                  if info.get("commits_30d") is not None else ""))
    if info.get("recent"):
        lines += ["**Recent commits:**"] + [f"- {c['date']} {c['subject'][:110]}" for c in info["recent"][:6]]
    if gh:
        if gh.get("error"):
            lines.append(f"**GitHub:** not checked ({gh['error'][:80]})")
        else:
            prs = gh.get("prs") or []
            iss = gh.get("issues") or []
            run = gh.get("run") or {}
            lines.append(f"**GitHub ({gh.get('repo')}):** {len(prs)} open PR(s), {len(iss)} open issue(s)"
                         + (f" · last CI: {run.get('workflowName', '')} {run.get('conclusion') or run.get('status')}"
                            f" ({str(run.get('createdAt', ''))[:10]})" if run else " · no CI runs"))
            lines += [f"- PR #{p['number']} {p['title'][:90]}" + (" (draft)" if p.get("isDraft") else "")
                      for p in prs[:5]]
            lines += [f"- Issue #{i['number']} {i['title'][:90]}" for i in iss[:5]]
    if links:
        lines.append("**Linked:** " + " · ".join(links))
    return "\n".join(lines)


def _repo_info(path, runner, now=None):
    title, about = _readme(path)
    commits = _commits(path, runner)
    url, gh = _remote(path, runner)
    cutoff = (dt.date.fromtimestamp(_now(now)) - dt.timedelta(days=30)).isoformat()
    return {"name": path.name, "path": str(path), "title": title, "about": about, "docs": _docs(path),
            "remote": url, "github": gh, "commits": commits,
            "last_commit": f"{commits[0]['date']} {commits[0]['subject'][:90]}" if commits else "",
            "commits_30d": sum(1 for c in commits if c["date"] >= cutoff) if commits else 0,
            "recent": commits[:6], "checked": _stamp(now)}


def _fingerprint(path, runner):
    try:
        refs = runner(["for-each-ref", "refs/heads", "--format=%(objectname)"], path)
    except Exception:
        refs = ""
    extra = ""
    for name in ("README.md", "readme.md"):
        p = path / name
        if p.is_file():
            extra = str(p.stat().st_mtime)
    d = path / "docs"
    if d.is_dir():
        extra += str(max((p.stat().st_mtime for p in d.rglob("*.md")), default=0))
    return _hash(refs + extra)


def update_project_from_repo(path, settings=None, runner=git, now=None, gh_data=None, force=False):
    """One repo -> its project note. Returns a dict of what changed (or {'unchanged': True})."""
    s = settings or Settings.load()
    state = kstore.get("ingest.repos", {})
    key = str(path)
    prev = state.get(key, {})
    fp = _fingerprint(path, runner)
    if not force and prev.get("fp") == fp and gh_data is None and prev.get("note") and \
            vault.safe_path(f"Projects/{prev['note']}", s).exists():
        return {"repo": path.name, "unchanged": True, "note": prev["note"]}
    info = _repo_info(path, runner, now)
    name = prev.get("note") if prev.get("note") and vault.safe_path(f"Projects/{prev['note']}", s).exists() \
        else match_project(path.name, s, info["title"])
    created = False
    if not name:
        name = info["title"] if info["title"] and len(info["title"]) <= 50 and "/" not in info["title"] \
            else _title(path.name)
        name = re.sub(r"[\\/:*?\"<>|#^\[\]]", "-", name).strip() or _title(path.name)
        last = info["commits"][0]["date"] if info["commits"] else ""
        dormant = last and (dt.date.fromtimestamp(_now(now)) - dt.date.fromisoformat(last)).days > DORMANT_DAYS
        projects.create(name, s, status="paused" if dormant else "active", aliases=[path.name],
                        goal=info["about"] or "", resources=[f"Repo: {path.name}" + (f" ({info['remote']})"
                                                                                      if info["remote"] else "")])
        created = True
    rel = f"Projects/{name}"
    text = vault.read(rel, s)
    meta = projects.parse_meta(text)
    repos = meta.get("repo", [])
    repos = repos if isinstance(repos, list) else [repos]
    if path.name not in repos:
        text = _set_frontmatter(text, "repo", "[" + ", ".join([*repos, path.name]) + "]")
    seen = set(prev.get("seen", []))
    new = [c for c in info["commits"] if c["sha"] not in seen] if seen else []
    gh = gh_data if gh_data is not None else prev.get("gh")
    links = [f"[[{m}]]" for m in memory_notes_mentioning([path.name, name, *meta.get("aliases", [])], s)[:6]]
    links += [f"[[{g}]]" for g in goals_for_project(name, s)]
    text = set_block(text, f"repo {path.name}", render_repo_block(info, gh, links), heading=f"From the repo "
                     f"({path.name})")
    vault.write(rel, text, s)
    changed = {"repo": path.name, "note": name, "created": created, "new_commits": len(new), "tasks_done": []}
    if new:
        subj = [c["subject"] for c in new]
        more = f" (+{len(new) - 4} more)" if len(new) > 4 else ""
        projects.add_log(name, f"{len(new)} new commit(s) in {path.name}: " + "; ".join(x[:80] for x in subj[:4])
                         + more, s)
        for t in [t["text"] for t in projects.tasks(name, s) if not t["done"]]:
            hit = task_done_by(t, subj)
            if hit:
                projects.complete_task(name, t, s)
                projects.add_log(name, f"Ticked '{t[:60]}': commit '{hit[:80]}'", s)
                changed["tasks_done"].append(t)
    elif not seen and info["commits"]:
        projects.add_log(name, f"Waggle started following {path.name} (last commit {info['last_commit'][:60]})", s)
    state[key] = {"fp": fp, "note": name, "seen": [c["sha"] for c in info["commits"]][:200],
                  "github": info["github"], "gh": gh, "time": _now(now)}
    kstore.put("ingest.repos", state)
    return changed


def sync_repos(settings=None, runner=git, now=None, repos=None):
    s = settings or Settings.load()
    out = {"repos": 0, "updated": 0, "created": 0, "new_commits": 0, "tasks_done": 0, "errors": []}
    for path in (repos if repos is not None else discover_repos(s)):
        out["repos"] += 1
        try:
            r = update_project_from_repo(Path(path), s, runner, now)
        except Exception as e:  # one broken repo never stops the rest
            out["errors"].append(f"{Path(path).name}: {type(e).__name__}: {e}"[:200])
            continue
        if not r.get("unchanged"):
            out["updated"] += 1
            out["created"] += int(r.get("created", False))
            out["new_commits"] += r.get("new_commits", 0)
            out["tasks_done"] += len(r.get("tasks_done", []))
    return out


# ---- 1c. GitHub ------------------------------------------------------------------------------------

def find_gh():
    p = shutil.which("gh")
    if p:
        return p
    for c in (r"C:\Program Files\GitHub CLI\gh.exe", r"C:\Program Files (x86)\GitHub CLI\gh.exe"):
        if os.path.exists(c):
            return c
    return None


def gh_json(args, timeout=40):
    exe = find_gh()
    if not exe:
        raise RuntimeError("gh CLI not found")
    r = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return json.loads(r.stdout or "null")


def github_status(repo, gh=gh_json):
    """{'repo', 'prs', 'issues', 'run'} for 'owner/name' (raises on failure)."""
    prs = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", "10", "--json", "number,title,isDraft,updatedAt"])
    iss = gh(["issue", "list", "-R", repo, "--state", "open", "--limit", "10", "--json", "number,title,updatedAt"])
    runs = gh(["run", "list", "-R", repo, "--limit", "1", "--json",
               "status,conclusion,workflowName,createdAt,headBranch"])
    return {"repo": repo, "prs": prs or [], "issues": iss or [], "run": (runs or [None])[0]}


def sync_github(settings=None, gh=gh_json, runner=git, now=None, repos=None):
    s = settings or Settings.load()
    out = {"repos": 0, "ok": 0, "errors": []}
    if gh is gh_json and not find_gh():
        return {**out, "skipped": "gh CLI not found"}
    for path in (repos if repos is not None else discover_repos(s)):
        path = Path(path)
        _, slug = _remote(path, runner)
        if not slug:
            continue
        out["repos"] += 1
        try:
            data = github_status(slug, gh)
            out["ok"] += 1
        except Exception as e:
            data = {"repo": slug, "error": f"{type(e).__name__}: {e}"}
            out["errors"].append(f"{slug}: {e}"[:160])
        try:
            update_project_from_repo(path, s, runner, now, gh_data=data, force=True)
        except Exception as e:
            out["errors"].append(f"{path.name}: {e}"[:160])
    return out


# ---- 1d. Upcoming (Calendar + mail through claude.ai connectors) ------------------------------------

UPCOMING_SYSTEM = """You read the user's own Google Calendar and Gmail (read-only) for their personal assistant.
Never send, reply, draft, delete, label, accept or change anything. Return JSON only:
{"events": [{"date": "YYYY-MM-DD", "time": "HH:MM or empty", "title": "...", "where": "...", "kind": "event|deadline"}],
 "replies": [{"date": "YYYY-MM-DD the mail arrived", "from": "sender name", "subject": "...", "why": "what they
 are waiting for, a few words"}]}
events: calendar events and dated deadlines (assignments, forms, registrations) in the next 14 days.
replies: mail from real people, school or the team in the last 7 days that waits for the user's answer
(not newsletters, receipts, notifications or marketing). Absolute dates only. Empty lists when there is nothing."""
UPCOMING_SCHEMA = {"type": "object", "properties": {
    "events": {"type": "array", "items": {"type": "object", "properties": {
        "date": {"type": "string"}, "time": {"type": "string"}, "title": {"type": "string"},
        "where": {"type": "string"}, "kind": {"type": "string"}}, "required": ["date", "title"]}},
    "replies": {"type": "array", "items": {"type": "object", "properties": {
        "date": {"type": "string"}, "from": {"type": "string"}, "subject": {"type": "string"},
        "why": {"type": "string"}}, "required": ["subject"]}}}, "required": ["events", "replies"]}


def sync_upcoming(settings=None, asker=None, connectors=None, now=None, model="haiku"):
    """One background connector call -> dated events and pending replies in the facts store -> Upcoming note.
    Skips quietly (and says why) when connectors or the budget are unavailable."""
    from . import budget, claude_cli, learn
    s = settings or Settings.load()
    try:
        conns = connectors if connectors is not None else claude_cli.available_connectors()
    except Exception as e:
        conns = []
        why = f"connectors unavailable: {e}"
    else:
        why = "no Gmail or Calendar connector"
    conns = [c for c in conns if c in ("Gmail", "Google_Calendar", "Gcal")]
    if not conns:
        learn.render_upcoming(s, now=now)
        return {"skipped": why}
    asker = asker or claude_cli.ask
    today = dt.date.fromtimestamp(_now(now))
    prompt = (f"Today is {today:%A %Y-%m-%d}. Connected: {', '.join(conns)}. List the next 14 days of calendar "
              f"events and deadlines, and mail from the last 7 days waiting for the user's reply.")
    try:
        with budget.spend(model, purpose="ingest-upcoming"):
            res = asker(prompt, model=model, system=UPCOMING_SYSTEM, schema=UPCOMING_SCHEMA,
                        purpose="ingest-upcoming", connectors=conns, timeout=240)
    except budget.Deferred as e:
        learn.render_upcoming(s, now=now)
        return {"skipped": f"budget: {e.reason}"}
    except Exception as e:
        learn.render_upcoming(s, now=now)
        return {"skipped": f"{type(e).__name__}: {str(e)[:160]}"}
    data = res.get("json") or {}
    n = learn.record_upcoming(data.get("events") or [], data.get("replies") or [], s, now=now,
                              sources=conns)
    return {"connectors": conns, **n}


# ---- goals ---------------------------------------------------------------------------------------

GOAL_FOLDER = "Goals"


def profile_goals(settings=None):
    s = settings or Settings.load()
    sec = re.search(r"^## Goals\s*\n(.*?)(?=^## |\Z)", vault.read("Me/Profile.md", s), re.S | re.M)
    return [l.strip()[2:].strip() for l in (sec.group(1).splitlines() if sec else []) if l.strip().startswith("- ")]


def goal_name(text):
    words = re.sub(r"\(.*?\)|[\\/:*?\"<>|#^\[\]]", " ", text).replace("→", " ").split()
    name = " ".join(words[:6]).strip(" ,.;-")
    return name[:60] or "Goal"


def _signature(text, common):
    words = set(re.findall(r"[a-z0-9]{4,}", text.lower())) | {w.lower() for w in re.findall(r"\b[A-Z]{3,}\b", text)}
    return words - common - {"with", "this", "that", "real", "make", "build", "from", "into", "goal", "season"}


def goals_for_project(project, settings=None):
    """Goal notes this project serves: it links the goal, the goal lists it, or they share a distinctive word."""
    s = settings or Settings.load()
    gdir = vault.root(s) / GOAL_FOLDER
    if not gdir.exists():
        return []
    ptext = vault.read(f"Projects/{project}", s)
    sec = projects.sections(project, s)
    pwords = _signature(" ".join([project, sec.get("Goal", ""), sec.get("Current focus", ""),
                                  " ".join(projects.parse_meta(ptext).get("aliases", []))]), set())
    out = []
    for g in sorted(gdir.glob("*.md")):
        gt = g.read_text(encoding="utf-8", errors="replace")
        if f"[[{project}]]" in gt.split("<!-- waggle:")[0] or f"[[{g.stem}]]" in ptext.split("<!-- waggle:")[0]:
            out.append(g.stem)
            continue
        m = re.search(r"^## Goal\s*\n(.+)", gt, re.M)
        if m and pwords & _signature(m.group(1), _COMMON.get(str(s.vault), set())):
            out.append(g.stem)
    return out


_COMMON = {}


def sync_goals(settings=None, now=None):
    """One note per standing goal (Profile -> Goals). Waggle creates missing notes and keeps their
    'Projects and progress' block current; everything else in a goal note is the user's."""
    s = settings or Settings.load()
    goals = profile_goals(s)
    allp = [p for p in projects.all_projects(s)]
    # words used by many projects say nothing about which goal a project serves
    counts = {}
    for p in allp:
        sec = projects.sections(p["name"], s)
        for w in _signature(p["name"] + " " + sec.get("Goal", "") + " " + sec.get("Current focus", ""), set()):
            counts[w] = counts.get(w, 0) + 1
    _COMMON[str(s.vault)] = {w for w, c in counts.items() if c > max(2, len(allp) * 0.35)}
    out = {"goals": len(goals), "created": 0, "updated": 0}
    for g in goals:
        name = goal_name(g)
        rel = f"{GOAL_FOLDER}/{name}"
        if not vault.safe_path(rel, s).exists():
            vault.write(rel, f"---\ntype: goal\nstatus: active\n---\n# {name}\n\n## Goal\n{g}\n\n## Why\n\n"
                             f"## Notes\n\n", s)
            out["created"] += 1
    for gp in sorted((vault.root(s) / GOAL_FOLDER).glob("*.md")) if (vault.root(s) / GOAL_FOLDER).exists() else []:
        linked = [p["name"] for p in allp if gp.stem in goals_for_project(p["name"], s)]
        lines = [f"_Kept current by Waggle ({_stamp(now)}): projects that serve this goal and their latest "
                 "activity. Add [[links]] outside this block to connect more._", ""]
        for pn in linked:
            log = projects.sections(pn, s).get("Log", "").strip().splitlines()
            lines.append(f"- [[{pn}]]" + (f": {log[-1].lstrip('- ').strip()[:120]}" if log else ""))
        try:
            from . import objectives
            for o in objectives.status():
                if o.get("status") in ("cancelled",):
                    continue
                if o.get("project") in linked:
                    lines.append(f"- Objective: {o['title'][:80]} ({o['status']}, {o['done']}/{o['total']} steps)")
        except Exception:
            pass
        if not linked:
            lines.append("- No project linked yet.")
        text = gp.read_text(encoding="utf-8", errors="replace")
        new = set_block(text, "goal progress", "\n".join(lines), heading="Projects and progress", before="## Notes")
        if _norm(new) != _norm(text):
            vault.write(gp.relative_to(vault.root(s)).as_posix(), new, s)
            out["updated"] += 1
    return out


# ---- the scheduler entry point ----------------------------------------------------------------------

def _hour(now):
    return dt.datetime.fromtimestamp(now).hour


def _job_memory(s, now, ctx):
    return mirror_claude_memory(s, now=now)


def _job_repos(s, now, ctx):
    return sync_repos(s, ctx.get("runner", git), now)


def _job_github(s, now, ctx):
    return sync_github(s, ctx.get("gh", gh_json), ctx.get("runner", git), now)


def _job_upcoming(s, now, ctx):
    return sync_upcoming(s, ctx.get("asker"), ctx.get("connectors"), now)


def _job_goals(s, now, ctx):
    return sync_goals(s, now)


def _job_digest(s, now, ctx):
    from . import digest
    return digest.run(s, now=now, asker=ctx.get("asker"))


def _job_learning(s, now, ctx):
    from . import learning
    return learning.daily(s, now=now, embedder=ctx.get("embedder"))


def _job_graph(s, now, ctx):
    from . import graph
    return graph.build_from_vault(s)


def _job_reindex(s, now, ctx):
    from . import memory
    return {"notes_updated": memory.reindex(s, embedder=ctx.get("doc_embedder"))}


# name -> (minimum seconds between runs, allowed hours or None, function). Order matters.
JOBS = {
    "memory": (50 * 60, None, _job_memory),
    "repos": (3 * 3600 - 300, None, _job_repos),
    "github": (3 * 3600 - 300, None, _job_github),
    "upcoming": (8 * 3600, range(6, 22), _job_upcoming),  # at most twice a day, a background Claude call
    "goals": (3 * 3600 - 300, None, _job_goals),
    "digest": (3 * 3600 - 300, None, _job_digest),
    "graph": (3 * 3600 - 300, None, _job_graph),
    "reindex": (0, None, _job_reindex),
    "learning": (20 * 3600, range(19, 24), _job_learning),  # daily, in the evening (after the digest)
}


def due(now=None):
    """[job names] that are due now, from the last-run times in knowledge.db."""
    now = _now(now)
    last = kstore.get("ingest.last_run", {})
    out = []
    for name, (gap, hours, _) in JOBS.items():
        if hours is not None and _hour(now) not in hours:
            continue
        if now - last.get(name, 0) >= gap:
            out.append(name)
    return out


def _lock(now):
    p = data_path("ingest.lock")
    try:
        pid, t = p.read_text(encoding="utf-8").split()
        if now - float(t) < 2 * 3600 and int(pid) != os.getpid():
            from .jobs import _alive
            if _alive(int(pid)):
                return False
    except (OSError, ValueError):
        pass
    p.write_text(f"{os.getpid()} {now}", encoding="utf-8")
    return True


def run_due(now=None, settings=None, force=(), only=None, exclude=(), **ctx):
    """Run every job that is due (or the ones in `force`/`only`). Idempotent: running it twice in a row does
    nothing the second time. ctx: fakes for tests (runner, gh, asker, connectors, embedder, doc_embedder).
    Returns {job: result} and logs one 'ingest' event."""
    s = settings or Settings.load()
    now = _now(now)
    if not _lock(now):
        return {"skipped": "another ingest is running"}
    try:
        names = list(only) if only else sorted(set(due(now)) | set(force), key=list(JOBS).index)
        names = [n for n in names if n not in exclude]  # the core runs the digest and learning on their own timers
        report, last = {}, kstore.get("ingest.last_run", {})
        changed = False
        for name in names:
            if name == "reindex" and not (changed or name in force or only):
                continue
            t0 = time.time()
            try:
                res = JOBS[name][2](s, now, ctx)
                last[name] = now
            except Exception as e:  # logged, retried next run; never stops the other jobs
                res = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            res = res if isinstance(res, dict) else {"result": res}
            res["seconds"] = round(time.time() - t0, 2)
            report[name] = res
            if name not in ("reindex", "learning") and any(
                    res.get(k) for k in ("added", "updated", "created", "removed", "new_commits", "filed",
                                         "written", "events", "ok")):
                changed = True
            kstore.put("ingest.last_run", last)
        if report:
            events.log("ingest", jobs=report)
            kstore.put("ingest.last_report", {"time": now, "report": report})
        return report
    finally:
        try:
            data_path("ingest.lock").unlink()
        except OSError:
            pass


# ---- one-time cleanup (explicit, reversible, never deletes) ----------------------------------------

CLEANUP_PREFIXES = ("Objectives/", "Inbox/", "Jobs/")
TEST_NAME = re.compile(r"(^|[\s_-])(test|smoke|deploy check|approval)([\s_-]|$)", re.I)


def cleanup_plan(settings=None):
    """What `cleanup --apply` would move: Waggle's test objectives/inbox/job notes, and journal days that are only
    route log lines. Hand-written notes are never in the plan (Inbox notes without a test-like name stay)."""
    s = settings or Settings.load()
    root = vault.root(s)
    plan = []
    for p in vault.notes(s):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("Objectives/", "Jobs/")):
            plan.append({"path": rel, "why": "Waggle-written objective/job note"})
        elif rel.startswith("Inbox/") and TEST_NAME.search(p.stem):
            plan.append({"path": rel, "why": "test note in Inbox"})
        elif rel.startswith("Journal/"):
            body = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                    if l.strip() and not l.startswith("# ")]
            if body and all(re.match(r"\s*- \d{1,2}:\d{2} route=", l) for l in body):
                plan.append({"path": rel, "why": "journal of route log lines only"})
    return plan


def cleanup(settings=None, apply=False, now=None):
    """Dry run unless apply=True. Moves files to Archive/cleanup-<stamp>/ keeping their folders, and writes a
    manifest so `restore` can put every file back."""
    s = settings or Settings.load()
    plan = cleanup_plan(s)
    if not apply:
        return {"dry_run": True, "would_move": plan}
    stamp = dt.datetime.fromtimestamp(_now(now)).strftime("%Y%m%d-%H%M%S")
    root = vault.root(s)
    moved = []
    for item in plan:
        src = root / item["path"]
        dst = root / ARCHIVE / f"cleanup-{stamp}" / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append({**item, "to": dst.relative_to(root).as_posix()})
    manifest = data_path(f"cleanup-{stamp}.json")
    manifest.write_text(json.dumps(moved, indent=1), encoding="utf-8")
    events.log("ingest", action="cleanup", moved=len(moved), manifest=str(manifest))
    return {"moved": moved, "id": stamp, "manifest": str(manifest)}


def purge(settings=None, apply=False, phrases=(), **kw):
    """Take everything derived from non-user turns out of the vault, facts, graph and index (waggle/purge.py):
    a dry run unless apply=True, with an undo record under the Waggle home. `python -m waggle.ingest purge`."""
    from . import purge as purge_mod
    return purge_mod.purge(settings, apply=apply, phrases=phrases, **kw)


def restore(cleanup_id, settings=None):
    if str(cleanup_id).startswith("purge-"):  # the id of a purge, not of a vault cleanup
        from . import purge as purge_mod
        return purge_mod.restore(cleanup_id, settings)
    s = settings or Settings.load()
    root = vault.root(s)
    items = json.loads(data_path(f"cleanup-{cleanup_id}.json").read_text(encoding="utf-8"))
    back = 0
    for it in items:
        src, dst = root / it["to"], root / it["path"]
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            back += 1
    return {"restored": back, "of": len(items)}


def status():
    return {"last_run": {k: dt.datetime.fromtimestamp(v).isoformat(timespec="minutes")
                         for k, v in kstore.get("ingest.last_run", {}).items()},
            "due_now": due(), "last_report": kstore.get("ingest.last_report", {})}


def main():
    """Cron entry (thin): runs what is due, prints nothing. CLI: run [jobs], status, cleanup [--apply], restore."""
    args = sys.argv[1:]
    from . import budget
    budget.set_background("ingest")
    if not args:
        try:
            run_due()
        except Exception as e:  # never print: stdout goes to the phone
            events.log("error", where="ingest", error=f"{type(e).__name__}: {e}"[:300])
        return
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args[0] == "run":
        print(json.dumps(run_due(force=args[1:] or list(JOBS)), indent=1, default=str))
    elif args[0] == "status":
        print(json.dumps(status(), indent=1, default=str))
    elif args[0] == "cleanup":
        print(json.dumps(cleanup(apply="--apply" in args), indent=1))
    elif args[0] == "purge":
        phrases = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--phrase"]
        print(json.dumps(purge(apply="--apply" in args, phrases=phrases), indent=1, default=str,
                         ensure_ascii=False))
    elif args[0] == "restore" and len(args) > 1:
        print(json.dumps(restore(args[1]), indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
