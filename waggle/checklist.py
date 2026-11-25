"""Judging a step on a checklist of requirements, from evidence and never from the agent's own report (research idea 7:
Agent-as-a-Judge, SWE-bench's before/after tests, AppWorld's collateral-damage checks).

Each step carries 2-5 concrete requirements. After the step:

  1. code checks what it can. A requirement that names evidence (file_exists, tests_pass, commit_exists, url_ok: the
     checks of waggle/autowork.py, reused as they are) or a file that must exist is decided by code alone.
  2. an evidence-only judge decides the rest. It sees the requirement texts and the ARTIFACTS the step left behind:
     the files that were added or changed inside the write roots (found by comparing the folders before and after),
     the results of the code checks, and, for a requirement marked `content` only, the step's final output as the
     deliverable. It never sees what the agent said about its own work. It must quote the evidence for every
     requirement it calls met, and a quote that is not in the evidence turns that requirement into "unmet". The judge
     is the cheap-first cascade (waggle/cascade.py, need="judge"): local model first, Haiku, then Sonnet, and a judge
     that is not sure and cannot ask a stronger model (usage is high) never passes a requirement.
  3. a side-damage check compares the files outside the charter's write roots before and after the step (the vault
     outside the roots, the folders beside them, the working folder and the home folder's top level): any file added,
     changed or removed there fails the step, with the paths listed.

The step is "proven" only when every requirement is met and nothing changed outside the roots. The per-requirement
results are kept (autowork records them with the step, objectives in steps.proof), so the evening note can say what
was missing. Switch off with config.json "checklist_judge": false: the old proof by code and the Haiku check on the
agent's report come back.
"""
import os
from pathlib import Path

from .config import Settings

MAX_REQUIREMENTS = 6          # evidence items plus the model's own: more than this is noise
MAX_FILES = 20000             # files looked at per folder when comparing before and after
EXCERPT = 3000                # characters of one changed file shown to the judge
MAX_ARTIFACT_FILES = 8
MAX_EVIDENCE = 12000          # characters of evidence in all
TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".csv", ".html", ".js", ".ts", ".yaml", ".yml", ".toml", ".cfg",
                 ".ini", ".tex", ".rst", ".c", ".cpp", ".h", ".java", ".sh", ".bat", ".ps1", ".xml", ".log"}
# Folders Waggle writes to itself while a step runs, and tool folders: never counted as side damage.
IGNORE_DIRS = {".git", ".obsidian", ".trash", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv",
               "journal", "sources", "drop", "dashboard"}
SKIP_FILES = {"desktop.ini", "thumbs.db"}
SKIP_SUFFIXES = {".tmp", ".lock", ".pyc", ".swp"}


def enabled(settings=None):
    return bool(getattr(settings or Settings.load(), "checklist_judge", True))


def home_dir():
    """The home folder whose top level is watched (a seam: the tests point it at a temp folder)."""
    return Path.home()


# ---- the requirements -------------------------------------------------------------------------------------------

def describe(ev):
    """A readable requirement for one piece of autowork evidence."""
    typ = ev.get("type")
    if typ == "file_exists":
        return (f"{ev.get('path')} exists, written during this step"
                + (f" and contains {ev['contains']!r}" if ev.get("contains") else ""))
    if typ == "tests_pass":
        return f"the tests pass ({' '.join(str(x) for x in ev.get('cmd') or [])} in {ev.get('cwd')})"
    if typ == "commit_exists":
        return f"a new commit exists in {ev.get('repo')}"
    if typ == "url_ok":
        return f"{ev.get('url')} answers"
    return f"evidence of type {typ}"


def normalize(requirements=None, evidence=None, limit=MAX_REQUIREMENTS):
    """[{"id", "text", "check"?, "path"?, "content"?}]: the evidence items first (decided by code), then the model's
    own requirements (a string, or {"text", "path"?, "content"?}). Duplicates and empty ones are dropped; at most
    `limit`. Ids are r1, r2, ... in that order."""
    out, seen = [], set()

    def add(item):
        text = str(item.get("text") or "").strip()[:300]
        if not text or text.lower() in seen or len(out) >= limit:
            return
        seen.add(text.lower())
        item["text"], item["id"] = text, f"r{len(out) + 1}"
        out.append(item)

    for ev in evidence or []:
        if isinstance(ev, dict) and ev.get("type"):
            add({"text": describe(ev), "check": ev})
    for r in requirements or []:
        if isinstance(r, str):
            add({"text": r})
        elif isinstance(r, dict):
            item = {"text": r.get("text")}
            if r.get("path"):
                item["path"] = str(r["path"])
            if r.get("content"):
                item["content"] = True
            add(item)
    return out


# ---- before and after: files ------------------------------------------------------------------------------------

def _skip_file(name):
    low = name.lower()
    return low in SKIP_FILES or low.startswith("ntuser") or Path(low).suffix in SKIP_SUFFIXES


def _under(path, roots):
    return any(path == r or r in path.parents for r in roots)


def _files(dirs, skip=(), cap=MAX_FILES):
    """{path: (mtime_ns, size)} for the files below `dirs`, leaving out ignored folders and everything under `skip`."""
    out = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for cur, subdirs, names in os.walk(d):
            here = Path(cur)
            subdirs[:] = [x for x in subdirs if x.lower() not in IGNORE_DIRS and not _under(here / x, skip)]
            for n in names:
                if _skip_file(n):
                    continue
                p = here / n
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[str(p)] = (st.st_mtime_ns, st.st_size)
                if len(out) >= cap:
                    return out
    return out


def _shallow(d, skip=(), cap=2000):
    """The files directly in one folder (not folders, not dotfiles)."""
    out = {}
    d = Path(d)
    if not d.is_dir():
        return out
    try:
        entries = list(os.scandir(d))
    except OSError:
        return out
    for e in entries[:cap]:
        try:
            if e.name.startswith(".") or _skip_file(e.name) or not e.is_file() or _under(Path(e.path), skip):
                continue
            st = e.stat()
        except OSError:
            continue
        out[e.path] = (st.st_mtime_ns, st.st_size)
    return out


def zones(roots, s=None):
    """(folders compared recursively, folders compared at their top level) outside the write roots."""
    s = s or Settings.load()
    from . import config
    deep, top = [], []
    if getattr(s, "vault", ""):
        deep.append(Path(s.vault))
    for r in roots:
        top.append(Path(r).parent)
    top.append(Path(s.agent_workdir) if getattr(s, "agent_workdir", "") else home_dir() / "Claude")
    top.append(home_dir())
    skip_home = Path(config.HOME).resolve()
    keep, seen = [], set()
    for d in top:
        try:
            d = d.resolve()
        except OSError:
            continue
        if d != skip_home and str(d) not in seen:
            seen.add(str(d))
            keep.append(d)
    return deep, keep


def watch(roots, s=None, deep=None, top=None):
    """What the folders look like now: {"inside": files under the write roots, "outside": files in the zones around
    them}. Take one before the step and one after (evaluate does the second)."""
    s = s or Settings.load()
    roots = [Path(r).resolve() for r in roots]
    d, t = zones(roots, s)
    deep = d if deep is None else [Path(x) for x in deep]
    top = t if top is None else [Path(x) for x in top]
    skip = tuple(roots)
    outside = _files(deep, skip=skip)
    for z in top:
        outside.update(_shallow(z, skip=skip))
    return {"inside": _files(roots), "outside": outside}


def changes(before, after):
    """{"added": [...], "changed": [...], "removed": [...]} between two {path: (mtime_ns, size)} maps."""
    return {"added": sorted(p for p in after if p not in before),
            "changed": sorted(p for p in after if p in before and after[p] != before[p]),
            "removed": sorted(p for p in before if p not in after)}


def side_damage(before, after):
    """The files outside the write roots that were added, changed or removed between two watch() snapshots, as
    ["path (added)", ...]."""
    ch = changes(before.get("outside", {}), after.get("outside", {}))
    return [f"{p} ({kind})" for kind in ("added", "changed", "removed") for p in ch[kind]]


# ---- the evidence the judge sees --------------------------------------------------------------------------------

def _excerpt(path, limit=EXCERPT):
    p = Path(path)
    try:
        if p.suffix.lower() not in TEXT_SUFFIXES or p.stat().st_size > 400_000:
            return f"(a {p.stat().st_size}-byte {p.suffix or 'file'}; contents not shown)"
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError as e:
        return f"(unreadable: {e})"


def gather(before=None, after=None, results=(), files=(), deliverable=None, reqs=()):
    """The text the judge reads: files added or changed inside the write roots, the named files, the results of the
    code checks, and the deliverable (only when a requirement is marked content). Never the agent's report."""
    parts = []
    if before is not None and after is not None:
        ch = changes(before.get("inside", {}), after.get("inside", {}))
        shown = [(p, "added") for p in ch["added"]] + [(p, "changed") for p in ch["changed"]]
        if shown:
            parts.append("FILES ADDED OR CHANGED BY THE STEP (inside the write roots, found by comparing the folders "
                         "before and after):")
            for p, kind in shown[:MAX_ARTIFACT_FILES]:
                parts.append(f"--- {p} ({kind})\n{_excerpt(p)}")
            if len(shown) > MAX_ARTIFACT_FILES:
                parts.append(f"(and {len(shown) - MAX_ARTIFACT_FILES} more files)")
        else:
            parts.append("FILES ADDED OR CHANGED BY THE STEP: none.")
        if ch["removed"]:
            parts.append("FILES REMOVED BY THE STEP: " + ", ".join(ch["removed"][:10]))
    for p in files:
        if Path(p).is_file():
            parts.append(f"--- {p} (named by the work)\n{_excerpt(p)}")
        else:
            parts.append(f"--- {p} does not exist")
    code = [f"- {r.get('text') or r.get('type')}: {'ok' if r.get('ok') else 'NOT ok'} ({r.get('why', '')})"
            for r in results]
    if code:
        parts.append("RESULTS OF CHECKS RUN BY CODE:\n" + "\n".join(code))
    if deliverable and any(r.get("content") for r in reqs):
        parts.append("DELIVERABLE (the step's final output, the thing to be judged for the requirements marked "
                     "[content]; it is the work itself, not a report on it):\n" + str(deliverable)[:4000])
    return "\n\n".join(parts)[:MAX_EVIDENCE]


# ---- decided by code --------------------------------------------------------------------------------------------

def _path_result(req, started, roots, s):
    """A requirement that names a file: it must exist, and have been written during the step. Inside the roots when
    roots are known."""
    p = Path(str(req["path"])).expanduser()
    res = {"id": req["id"], "text": req["text"], "by": "code", "type": "file_exists", "ok": False}
    try:
        if roots and not any(p.resolve() == r or r in p.resolve().parents for r in roots):
            res["why"] = f"{p} is outside the write roots"
        elif p.is_dir():  # a folder requirement: it must hold something written during the step
            fresh = [f for f in list(p.rglob("*"))[:2000] if f.is_file() and f.stat().st_mtime >= started - 2]
            if fresh:
                res.update(ok=True, why=f"folder with {len(fresh)} file(s) written during this step")
            else:
                res["why"] = f"{p} is a folder with nothing written in it during this step"
        elif not p.is_file():
            res["why"] = f"{p} does not exist"
        elif p.stat().st_mtime < started - 2:
            res["why"] = f"{p} was not written during this step"
        else:
            res.update(ok=True, why=f"{p.stat().st_size} bytes")
    except OSError as e:
        res["why"] = f"{type(e).__name__}: {e}"[:160]
    return res


# ---- the judge --------------------------------------------------------------------------------------------------

JUDGE_SYSTEM = """You check whether finished work meets a list of requirements. You are shown the requirements and the
EVIDENCE: files the work left behind and results of checks that code ran. You are NOT shown what the worker said about
its own work, and you must not assume anything the evidence does not show. For each requirement: met=true only when
the evidence shows it, and then copy one short exact quote from the evidence into "quote"; otherwise met=false and
say in "missing" what is not shown. A requirement marked [content] is judged on the DELIVERABLE. Do not add
requirements of your own. Reply JSON only."""
JUDGE_SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {
    "type": "array", "items": {"type": "object", "required": ["id", "met", "quote", "missing"], "properties": {
        "id": {"type": "string"}, "met": {"type": "boolean"}, "quote": {"type": "string"},
        "missing": {"type": "string"}}}}}}


def _norm(text):
    return " ".join(str(text or "").lower().split())


def judge_prompt(pending, evidence):
    lines = "\n".join(f"{r['id']}{' [content]' if r.get('content') else ''}: {r['text']}" for r in pending)
    return f"REQUIREMENTS:\n{lines}\n\nEVIDENCE:\n{evidence or '(none)'}"


def judge_check(pending, evidence):
    """The rule check handed to the cascade: every requirement answered once, and every `met` backed by a quote
    that really is in the evidence. A judge that fails it is asked again one tier up."""
    ids = {r["id"] for r in pending}
    hay = _norm(evidence)

    def check(data):
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return False, "no results list"
        got = [str(i.get("id")) for i in items if isinstance(i, dict)]
        if sorted(got) != sorted(ids):
            return False, f"answered {sorted(got)} for {sorted(ids)}"
        for i in items:
            if i.get("met") is True and not (len(_norm(i.get("quote"))) >= 4 and _norm(i.get("quote")) in hay):
                return False, f"the quote for {i.get('id')} is not in the evidence"
        return True, ""
    return check


def judge(pending, evidence, s, *, asker=None, local=None, objective=None, pressure=None, task="checklist"):
    """({id: (met, why)}, info). Runs the cascade (need="judge"). Raises what the cascade raises when no tier could
    answer (budget.Deferred, claude_cli.ClaudeError)."""
    from . import cascade
    res = cascade.ask(task, judge_prompt(pending, evidence), JUDGE_SCHEMA, "judge", system=JUDGE_SYSTEM,
                      check=judge_check(pending, evidence), purpose="verify", objective=objective, asker=asker,
                      local=local, pressure=pressure, baseline="haiku")
    by = {str(i.get("id")): i for i in (res.get("json") or {}).get("results", []) if isinstance(i, dict)}
    unsure = bool(res.get("low_confidence"))
    out = {}
    for r in pending:
        it = by.get(r["id"]) or {}
        if it.get("met") is True and not unsure:
            out[r["id"]] = (True, f"\"{str(it.get('quote'))[:120]}\"")
        elif it.get("met") is True:
            out[r["id"]] = (False, "the judge was not sure and a stronger one could not be asked (usage is high)")
        else:
            out[r["id"]] = (False, str(it.get("missing") or "the evidence does not show it")[:200])
    return out, {"tier": res.get("tier"), "confidence": res.get("confidence"), "low_confidence": unsure}


# ---- the whole check --------------------------------------------------------------------------------------------

def evaluate(reqs, *, started, settings=None, goal=None, roots=None, before_git=None, before=None, files=(),
             deliverable=None, fetch=None, asker=None, local=None, objective=None, undecided_as_unmet=True,
             pressure=None):
    """Judge a finished step. reqs: normalize()'s list. goal: an autowork goal (its charter's write roots decide
    file evidence); roots: the write roots for the side-damage and artifact comparison; before: watch() taken
    before the step (without it there is no side-damage check and no changed-file evidence); files: extra files
    to show the judge; deliverable: the step's final output, shown only for [content] requirements.
    Returns {"proven", "results", "checks", "missing", "side_damage", "undecided", "judge"}: results is one dict per
    requirement {"id", "text", "by": "code"|"judge", "type", "ok", "why"}, checks the same plus the side-damage
    entry. undecided is a text when the judge could not run (and undecided_as_unmet is False); the step is then
    neither proven nor failed."""
    s = settings or Settings.load()
    results, pending = [], []
    if goal is not None:
        from . import autowork
    for r in reqs:
        if r.get("check") and goal is not None:
            _, res = autowork.prove([r["check"]], goal, started, before_git, s, fetch)
            results.append({**res[0], "id": r["id"], "text": r["text"], "by": "code",
                            "ok": bool(res[0].get("ok")), "why": res[0].get("why", "")})
        elif r.get("path"):
            results.append(_path_result(r, started, roots, s))
        else:
            pending.append(r)
    after = watch(roots, s) if (before is not None and roots) else None
    undecided, info = None, {}
    if pending and any(not r["ok"] for r in results):  # code already failed the step: no judge call is spent on it
        for r in pending:
            results.append({"id": r["id"], "text": r["text"], "by": "judge", "type": "judge", "ok": False,
                            "why": "not judged: a check run by code already failed"})
        results.sort(key=lambda x: {q["id"]: i for i, q in enumerate(reqs)}.get(x["id"], 99))
    elif pending:
        evidence = gather(before, after, results, files, deliverable, reqs)
        try:
            verdicts, info = judge(pending, evidence, s, asker=asker, local=local, objective=objective,
                                   pressure=pressure)
        except Exception as e:  # noqa: BLE001 - no tier could answer: budget, limit, Claude down, nothing usable
            why = f"could not be judged: {type(e).__name__}: {e}"[:200]
            verdicts = {r["id"]: (False, why) for r in pending}
            undecided = None if undecided_as_unmet else why
        for r in pending:
            ok, why = verdicts[r["id"]]
            results.append({"id": r["id"], "text": r["text"], "by": "judge", "type": "judge", "ok": ok, "why": why})
        order = {r["id"]: i for i, r in enumerate(reqs)}
        results.sort(key=lambda x: order.get(x["id"], 99))
    checks = list(results)
    damage = []
    if after is not None:
        damage = side_damage(before, after)
        checks.append({"id": "side", "text": "nothing changed outside the write roots", "by": "code",
                       "type": "side_damage", "ok": not damage,
                       "why": ("no file outside the write roots changed" if not damage else
                               "changed outside the write roots: " + "; ".join(damage[:5])
                               + (f" (+{len(damage) - 5} more)" if len(damage) > 5 else ""))})
    unmet = [c for c in checks if not c["ok"]]
    return {"proven": bool(results) and not unmet, "results": results, "checks": checks,
            "missing": "; ".join(f"[{c.get('id')}] {c.get('text') or c['type']}: {c.get('why')}" for c in unmet),
            "side_damage": damage, "undecided": undecided, "judge": info}


def summary(checks):
    """One line for a stored list of checks: how many were met, and the first that were not."""
    done = sum(1 for c in checks if c.get("ok"))
    line = f"{done} of {len(checks)} requirements met"
    miss = [f"{c.get('text') or c.get('type')} ({c.get('why')})" for c in checks if not c.get("ok")]
    return line + ("; missing: " + "; ".join(m[:140] for m in miss[:2]) if miss else "")
