"""Skill scout: keeps looking on GitHub for good Agent Skills (folders with a SKILL.md) and installs the useful ones.

The owner asked (2026-11-25) that Waggle always look for good skills on GitHub and install them by itself. A skill is
text instructions, so the risk is what the text tells an agent to do, plus any scripts it ships. The scout therefore
installs only what passes every check below, and only the instructions:

  1. found      `gh search repos` by skill topics (most stars, pushed in the last year), then each repo's SKILL.md
                files from its git tree (at most PER_REPO per repo)
  2. new        a skill whose name Waggle already has (any tier) is skipped
  3. licensed   the repo has a permissive licence (MIT, Apache-2.0, BSD, ISC, CC0, CC-BY, Unlicense)
  4. clean      SKILL.md under MAX_BYTES, no hidden characters, none of the injection patterns (INJECTION); a skill
                folder that ships scripts is never installed: it is recorded "needs review" for the owner
  5. useful     one tool-less model call (purpose "skillscout", its own small allowance) judges the batch against who
                the owner is and the skills Waggle already has: install only "useful" with risk "low"

Installed skills go to ~/.hermes/skills/found/<name>/ (SKILL.md plus its other .md files, nothing executable), with
where it came from (repo, commit, licence) in found.json next to it. They show up in skill_search as tier "found":
ranked below Waggle's own and official skills, and skill_load marks their text as outside instructions. A core timer
runs the scout every day (03:20, caught up when the PC was off) and at most MAX_INSTALLS_PER_RUN are installed per run.
What was installed goes into the evening note.

  python -m waggle.skillscout run [--dry-run]      one pass now
  python -m waggle.skillscout list                 what was found, installed, skipped and why
  python -m waggle.skillscout remove NAME          uninstall one found skill (kept in the record as "removed")
"""
import base64
import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import events
from .config import Settings, data_path

TOPICS = ("agent-skills", "claude-skills", "claude-code-skills", "anthropic-skills", "skills-md", "codex-skills")
REPOS_PER_TOPIC = 8
PER_REPO = 25
MAX_BYTES = 40_000
MAX_JUDGE = 14
MAX_INSTALLS_PER_RUN = 3
LICENSES = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "cc0-1.0", "cc-by-4.0", "unlicense", "0bsd"}
SCRIPT_EXT = {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".exe", ".dll",
              ".jar", ".bin", ".vbs", ".psm1"}
INJECTION = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)", r"disregard (your|the) (system|previous)",
    r"you are no longer", r"do not (tell|inform) the user", r"without (telling|asking) the user",
    r"exfiltrat", r"(send|upload|post) [^\n]{0,60}(token|password|api key|secret|cookie|ssh key|\.env)",
    r"(curl|wget|iwr|invoke-webrequest)[^\n|]*\|\s*(sh|bash|iex|python|pwsh)", r"base64 -d[^\n]*\|\s*(sh|bash)",
    r"(disable|turn off|bypass) [^\n]{0,30}(guard|safety|permission|hook|sandbox)", r"--dangerously",
    r"rm -rf /", r"format c:", r"bypasspermissions",
]
HIDDEN = re.compile("[​-‏‪-‮⁠-⁤﻿\U000e0000-\U000e007f]")
FOUND = Path.home() / ".hermes" / "skills" / "found"
PURPOSE = "skillscout"

JUDGE_SYSTEM = """You choose which community Agent Skills a personal assistant (Waggle) should install. Waggle runs on
its owner's Windows PC and talks to them by Telegram, voice and a dashboard; it uses Claude for chat and work, reads
their Obsidian vault, GitHub and mail, and works on their projects at night. The owner: a 17-year-old student in
California, FTC robotics (team 7831, Java robot code, CAD in Onshape), builds software and web projects for a college
portfolio, applying to colleges, wants Waggle to be proactive and to make money online lawfully.

For each candidate skill (name, description, the start of its instructions) answer: useful (would Waggle use it for
this owner within a month, and is it better than what Waggle already has, listed below), risk (low: plain how-to
instructions; medium: tells the agent to fetch and run things, change settings, or act on accounts; high: anything
deceptive, data-moving, safety-weakening or unrelated to its stated purpose) and one sentence why. Be selective:
most candidates should be useful=false. Return JSON only."""
JUDGE_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
    "key": {"type": "string"}, "useful": {"type": "boolean"}, "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    "why": {"type": "string"}}, "required": ["key", "useful", "risk", "why"]}}}, "required": ["verdicts"]}


# ---- storage -------------------------------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(data_path("skillscout.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS candidates (key TEXT PRIMARY KEY, name TEXT, repo TEXT, path TEXT,
        sha TEXT, license TEXT, stars INTEGER, description TEXT, status TEXT, why TEXT, seen REAL, decided REAL,
        installed_to TEXT)""")
    return con


def records(status=None):
    con = _db()
    q, args = "SELECT * FROM candidates", []
    if status:
        q += " WHERE status=?"
        args.append(status)
    rows = [dict(r) for r in con.execute(q + " ORDER BY decided DESC, seen DESC", args)]
    con.close()
    return rows


def _put(c, status, why, installed_to=""):
    con = _db()
    con.execute("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["key"], c["name"], c["repo"], c["path"], c.get("sha", ""), c.get("license", ""), c.get("stars", 0),
                 c.get("description", "")[:600], status, str(why)[:400], c.get("seen") or time.time(), time.time(),
                 installed_to))
    con.commit()
    con.close()


def _known_keys():
    con = _db()
    keys = {r[0] for r in con.execute("SELECT key FROM candidates")}
    con.close()
    return keys


# ---- GitHub (through the gh CLI the owner is signed in to) ----------------------------------------------------

def gh(*args, timeout=60):
    from . import nightshift
    exe = nightshift.gh_exe() if hasattr(nightshift, "gh_exe") else "gh"
    r = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout or "gh failed").strip()[:200])
    return r.stdout


def search_repos(topics=TOPICS, per_topic=REPOS_PER_TOPIC, run=None, now=None):
    run = run or gh
    since = dt.date.fromtimestamp((now or time.time()) - 365 * 86400).isoformat()
    seen, out = set(), []
    for t in topics:
        try:
            rows = json.loads(run("search", "repos", "--topic", t, "--sort", "stars", "--limit", str(per_topic),
                                  "--updated", f">{since}", "--json",
                                  "fullName,stargazersCount,license,defaultBranch,description"))
        except Exception as e:  # noqa: BLE001 - one topic failing never stops the others
            events.log("skillscout", step="search", topic=t, error=str(e)[:200])
            continue
        for r in rows:
            if r["fullName"] not in seen:
                seen.add(r["fullName"])
                out.append(r)
    return out


def skill_paths(repo, branch, run=None):
    """(sha, [SKILL.md paths], {folder: [files]}) from the repo's git tree."""
    run = run or gh
    tree = json.loads(run("api", f"repos/{repo}/git/trees/{branch}?recursive=1"))
    files = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
    skills = [p for p in files if p.endswith("SKILL.md") and "/." not in "/" + p][:PER_REPO]
    by_folder = {}
    for p in skills:
        folder = p.rsplit("/", 1)[0] if "/" in p else ""
        by_folder[folder] = [f for f in files if (f.startswith(folder + "/") if folder else "/" not in f)]
    return tree.get("sha", ""), skills, by_folder


def fetch(repo, path, sha, run=None):
    run = run or gh
    data = json.loads(run("api", f"repos/{repo}/contents/{path}?ref={sha}"))
    if data.get("size", 0) > MAX_BYTES:
        raise ValueError("too large")
    return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")


# ---- checks --------------------------------------------------------------------------------------------------

def check_text(text):
    """'' when the text is clean, else why not."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        return "too large"
    if HIDDEN.search(text):
        return "hidden characters"
    low = text.lower()
    for pat in INJECTION:
        if re.search(pat, low):
            return f"instruction pattern: {pat[:40]}"
    return ""


def ships_scripts(files):
    return sorted(f for f in files if Path(f).suffix.lower() in SCRIPT_EXT)


def existing_names():
    from . import skills
    try:
        skills.ensure_index()
        return {r["name"].lower() for r in skills._rows()}
    except Exception:  # noqa: BLE001
        return set()


# ---- judging and installing ----------------------------------------------------------------------------------

def judge(cands, settings, asker=None):
    from . import budget, claude_cli
    have = sorted(existing_names())[:250]
    items = [{"key": c["key"], "name": c["name"], "repo": c["repo"], "stars": c.get("stars", 0),
              "description": c.get("description", "")[:300], "start": c["text"][:900]} for c in cands]
    prompt = ("Skills Waggle already has: " + ", ".join(have) + "\n\nCandidates:\n" + json.dumps(items, indent=1))
    with budget.background(PURPOSE):
        res = (asker or claude_cli.ask)(prompt, model=getattr(settings, "skillscout_model", "sonnet") or "sonnet",
                                        system=JUDGE_SYSTEM, schema=JUDGE_SCHEMA, effort="low", purpose=PURPOSE,
                                        timeout=240)
    data = res.get("json") or {}
    return {v["key"]: v for v in data.get("verdicts", []) if isinstance(v, dict) and v.get("key")}


def safe_name(name):
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:60] or "skill"


def install(c, docs, root=None):
    """Write SKILL.md and the folder's other .md files (never anything executable). Returns the folder."""
    root = Path(root or FOUND)
    dest = root / safe_name(c["name"])
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(c["text"], encoding="utf-8")
    for rel, text in docs.items():
        target = (dest / rel).resolve()
        if dest.resolve() in target.parents and target.suffix.lower() == ".md":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    (dest / "found.json").write_text(json.dumps({"repo": c["repo"], "path": c["path"], "commit": c.get("sha", ""),
                                                 "license": c.get("license", ""), "stars": c.get("stars", 0),
                                                 "installed": time.time()}, indent=1), encoding="utf-8")
    return dest


def run(settings=None, dry=False, run_gh=None, asker=None, root=None, now=None):
    """One pass. Returns {"searched", "candidates", "installed": [...], "needs_review": [...], "skipped": n}."""
    s = settings or Settings.load()
    now = now or time.time()
    report = {"searched": 0, "candidates": 0, "installed": [], "needs_review": [], "skipped": 0, "dry": dry}
    if not getattr(s, "skillscout_enabled", True):
        report["note"] = "switched off (skillscout_enabled false)"
        return report
    have, known = existing_names(), _known_keys()
    cands = []
    for r in search_repos(run=run_gh, now=now):
        report["searched"] += 1
        lic = ((r.get("license") or {}).get("key") or "").lower()
        try:
            sha, paths, folders = skill_paths(r["fullName"], r.get("defaultBranch") or "main", run=run_gh)
        except Exception as e:  # noqa: BLE001
            events.log("skillscout", step="tree", repo=r["fullName"], error=str(e)[:160])
            continue
        for p in paths:
            folder = p.rsplit("/", 1)[0] if "/" in p else ""
            key = f"{r['fullName']}:{p}"
            if key in known:
                continue
            c = {"key": key, "repo": r["fullName"], "path": p, "sha": sha, "license": lic,
                 "stars": r.get("stargazersCount", 0), "name": (folder.rsplit("/", 1)[-1] or r["fullName"].split("/")[1]),
                 "seen": now, "folder": folder, "files": folders.get(folder, [])}
            if lic not in LICENSES:
                report["skipped"] += 1
                if not dry:
                    _put(c, "skipped", f"licence {lic or 'none'} is not on the permissive list")
                continue
            try:
                c["text"] = fetch(r["fullName"], p, sha, run=run_gh)
            except Exception as e:  # noqa: BLE001
                report["skipped"] += 1
                if not dry:
                    _put(c, "skipped", f"could not read: {e}")
                continue
            from . import skills
            meta, _ = skills.parse_frontmatter(c["text"])
            c["name"] = str(meta.get("name") or c["name"]).strip()[:80]
            c["description"] = str(meta.get("description") or "")[:600]
            why = ("already installed" if c["name"].lower() in have else check_text(c["text"])
                   or ("ships scripts: " + ", ".join(ships_scripts(c["files"])[:5]) if ships_scripts(c["files"]) else ""))
            if why:
                status = "needs review" if why.startswith("ships scripts") else "skipped"
                if status == "needs review":
                    report["needs_review"].append(c["name"])
                else:
                    report["skipped"] += 1
                if not dry:
                    _put(c, status, why)
                continue
            cands.append(c)
    cands.sort(key=lambda c: -c.get("stars", 0))
    report["candidates"] = len(cands)
    if not cands:
        return report
    batch = cands[:MAX_JUDGE]
    try:
        verdicts = judge(batch, s, asker=asker)
    except Exception as e:  # noqa: BLE001 - budget, limit, CLI: the candidates wait for the next run
        report["note"] = f"judge unavailable: {str(e)[:160]}"
        return report
    installed = 0
    for c in batch:
        v = verdicts.get(c["key"]) or {"useful": False, "risk": "high", "why": "no verdict"}
        if v.get("useful") and v.get("risk") == "low" and installed < MAX_INSTALLS_PER_RUN:
            if dry:
                report["installed"].append({"name": c["name"], "repo": c["repo"], "why": v.get("why", ""), "dry": True})
                installed += 1
                continue
            docs = {}
            for f in c["files"]:
                rel = f[len(c["folder"]) + 1:] if c["folder"] else f
                if f.endswith(".md") and f != c["path"] and len(docs) < 8:
                    try:
                        t = fetch(c["repo"], f, c["sha"], run=run_gh)
                        if not check_text(t):
                            docs[rel] = t
                    except Exception:  # noqa: BLE001
                        pass
            dest = install(c, docs, root=root)
            _put(c, "installed", v.get("why", ""), str(dest))
            report["installed"].append({"name": c["name"], "repo": c["repo"], "why": v.get("why", "")})
            installed += 1
        elif not dry:
            _put(c, "declined", f"{'useful' if v.get('useful') else 'not useful'}, risk {v.get('risk')}: {v.get('why', '')}")
    if report["installed"] and not dry:
        try:
            from . import skills
            skills.reindex(force=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import initiative
            names = ", ".join(f"{i['name']} ({i['repo']})" for i in report["installed"])
            initiative.keep_for_evening(f"skillscout:{dt.date.fromtimestamp(now)}", "skills",
                                        f"New skills installed from GitHub: {names}", now=now)
        except Exception:  # noqa: BLE001
            pass
    events.log("skillscout", installed=[i["name"] for i in report["installed"]], candidates=report["candidates"],
               needs_review=len(report["needs_review"]), dry=dry)
    return report


def remove(name, root=None):
    dest = Path(root or FOUND) / safe_name(name)
    if not dest.exists():
        return False
    shutil.rmtree(dest)
    con = _db()
    con.execute("UPDATE candidates SET status='removed', decided=? WHERE installed_to=?", (time.time(), str(dest)))
    con.commit()
    con.close()
    try:
        from . import skills
        skills.reindex(force=True)
    except Exception:  # noqa: BLE001
        pass
    return True


# ---- the core job and its timer ------------------------------------------------------------------------------

def due(payload=None, **_):
    rep = run()
    return {k: rep.get(k) for k in ("searched", "candidates", "skipped", "note")} | {
        "installed": [i["name"] for i in rep["installed"]], "needs_review": rep["needs_review"][:10]}


def register_core(now=None):
    """Core plug-in entry point: job 'skillscout.run' and its daily timer."""
    from waggle.core import jobs, timers
    jobs.register("skillscout.run", due, max_attempts=1, lease_s=1800)
    timers.add("skillscout.daily", "cron", "20 3 * * *", "skillscout.run", catch_up="latest", max_late_s=20 * 3600,
               origin="skillscout", now=now)
    return {"jobs": ["skillscout.run"], "timers": ["skillscout.daily"]}


def main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args[:1] == ["run"]:
        from . import budget
        budget.set_background(PURPOSE)
        print(json.dumps(run(dry="--dry-run" in args), indent=1, ensure_ascii=False))
        return 0
    if args[:1] == ["list"]:
        for r in records():
            print(f"{r['status']:<13} {r['name'][:34]:<35} {r['repo'][:40]:<41} {r['why'][:90]}")
        return 0
    if args[:1] == ["remove"] and len(args) > 1:
        print("removed" if remove(args[1]) else "no such found skill")
        return 0
    print("usage: python -m waggle.skillscout run [--dry-run] | list | remove NAME")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
