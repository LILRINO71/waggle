"""Finding repeated work, and the path from a repetition to an automation the owner can switch on.

mine() reads what the activity model already derived (nothing is read twice, no model is called):
  command sequences  the command shapes of each Claude Code session in time order ("git add > git commit >
                     git push"). A chain typed as one command, or two or three consecutive commands within ten
                     minutes, is a sequence. Read-only sequences (status, log, ls...) are ignored.
  requests           the owner's own turns (Claude Code sessions and Waggle chat turns with origin 'user'), grouped
                     by their keywords (Jaccard >= 0.5 with the group's first turn). Long pasted briefs are ignored.
A repetition becomes a candidate at MIN_COUNT occurrences in MIN_SESSIONS sessions on MIN_DAYS different days.
Each candidate keeps its count, sessions, days, workstreams and last occurrence as evidence.

Lifecycle (candidates table in autonomy.db):
  found      mined; request candidates stay here until someone writes them a recipe
  shadow     the proof step passed: the recipe is classified step by step (OUTWARD, LOCAL_WRITE, SAFE), it has
             at least one step worth automating, and its project folder exists
  proposed   three shadow runs passed. A shadow run never acts outward and never writes: OUTWARD and
             LOCAL_WRITE steps are only recorded as "would run"; SAFE steps (read-only git) run in the project
             folder. Test runners run only when the executor allows it (off by default: no surprise CPU load).
             A shadow run happens when a new real occurrence was seen since the last one.
  enabled    only by the owner: `python -m waggle.miner enable <key>`. This build stops there: running an
             enabled automation for real waits for the background permission rules (see goals.charter_for).
  rejected   by the owner: `python -m waggle.miner reject <key>`.
"""
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from . import activity
from . import autonomy_store as store_
from .config import Settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_COUNT, MIN_SESSIONS, MIN_DAYS = 3, 3, 2
REQUEST_MIN_KEYWORDS = 2
REQUEST_JACCARD = 0.5
GAP_S = 600
SHADOW_RUNS_NEEDED = 3
MAX_CANDIDATES = 30
OUTWARD = {"git push", "git send-email", "gh pr create", "gh pr merge", "gh pr comment", "gh pr review",
           "gh release create", "gh release upload", "gh issue create", "gh issue comment", "gh issue close",
           "gh repo create", "gh repo delete", "gh api", "gh workflow run", "gh gist create", "npm publish",
           "twine upload", "curl", "wget", "scp", "ssh", "rsync", "hermes send", "invoke-webrequest",
           "invoke-restmethod", "send-mailmessage"}
SAFE = {"git status", "git log", "git diff", "git show", "git branch", "git rev-parse", "git remote",
        "git ls-files"}
TEST_RUNNERS = {"python -m pytest": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                "npm test": ["npm", "test"], "node --test": ["node", "--test"]}


def classify(shape):
    """OUTWARD (reaches anyone or anything off this machine), SAFE (read-only, runs in shadow), TEST (a test runner)
    or LOCAL_WRITE (everything else, including what is not recognised: never run in shadow)."""
    if shape in OUTWARD or (shape.startswith("gh ") and not shape.endswith((" view", " list", " status",
                                                                             " checks", " diff"))):
        return "OUTWARD"  # any other gh command may change something on GitHub
    if shape in TEST_RUNNERS:
        return "TEST"
    if shape in SAFE or activity.is_read_only(shape):
        return "SAFE"
    return "LOCAL_WRITE"


def _key(kind, label):
    return f"{kind}:" + hashlib.sha1(label.encode()).hexdigest()[:10]


def _day(ts):
    return dt.date.fromtimestamp(ts).isoformat()


def _sessions(settings=None):
    """The derived facts the activity model cached (all non-machine session files)."""
    con = store_.db()
    try:
        rows = [store_.loads(r[0]) for r in con.execute("SELECT data FROM cc_files")]
    finally:
        con.close()
    return [d for d in rows if d and not d.get("machine")]


# Ad-hoc commands: an inline script, a heredoc, a folder or a file being written. They are how work gets done,
# not a repeatable step, so they never form part of a sequence (and neither do read-only commands).
AD_HOC = {"python", "python -c", "python -", "node", "bash", "sh", "powershell", "pwsh", "cmd", "mkdir", "cat",
          "tee", "touch", "echo", "printf", "start-sleep", "sleep"}


def actionable(shape):
    """The parts of a command shape worth automating: not read-only, not ad hoc."""
    return [p for p in (shape or "").split(" > ") if p and p not in AD_HOC and not activity.is_read_only(p)]


def command_sequences(sessions):
    """{sequence label: [occurrence {ts, session, ws}]}: chains typed as one command, and two or three consecutive
    commands within GAP_S of each other, reduced to their actionable parts (at least two, at most four)."""
    found = {}
    for d in sessions:
        sid = d.get("session") or ""
        units = []
        for ts, shape, ws in sorted(d.get("cmds") or [], key=lambda c: c[0]):
            parts = actionable(shape)
            if not parts:
                continue
            if 2 <= len(parts) <= 4:
                found.setdefault(" > ".join(parts), []).append({"ts": ts, "session": sid, "ws": ws})
            units.append((ts, parts, ws))
        for n in (2, 3):
            for i in range(len(units) - n + 1):
                win = units[i:i + n]
                if win[-1][0] - win[0][0] > GAP_S * (n - 1):
                    continue
                parts = []
                for _, ps, _ in win:
                    for p in ps:
                        if not parts or parts[-1] != p:
                            parts.append(p)
                if not 2 <= len(parts) <= 4:
                    continue
                label = " > ".join(parts)
                occ = found.setdefault(label, [])
                if occ and occ[-1]["session"] == sid and win[0][0] - occ[-1]["ts"] < GAP_S:
                    continue  # the same run of commands seen through a longer window
                occ.append({"ts": win[0][0], "session": sid, "ws": win[-1][2] or win[0][2]})
    return found


def request_groups(turns):
    """turns: [{ts, keywords, source, ws}] -> [{label, members}] grouped by keyword overlap."""
    groups = []
    for t in sorted(turns, key=lambda t: t["ts"]):
        kw = set(t.get("keywords") or [])
        if len(kw) < REQUEST_MIN_KEYWORDS:
            continue
        best, score = None, 0.0
        for g in groups:
            j = len(kw & g["seed"]) / len(kw | g["seed"])
            if j > score:
                best, score = g, j
        if best is not None and score >= REQUEST_JACCARD:
            best["members"].append(t)
        else:
            groups.append({"seed": kw, "members": [t]})
    out = []
    for g in groups:
        counts = {}
        for m in g["members"]:
            for w in set(m["keywords"]):
                counts[w] = counts.get(w, 0) + 1
        common = sorted((w for w, c in counts.items() if c >= 0.6 * len(g["members"])), key=lambda w: -counts[w])
        out.append({"label": " ".join(common[:4]) or " ".join(sorted(g["seed"])[:4]), "members": g["members"]})
    return out


def _evidence(occ):
    return {"count": len(occ), "sessions": len({o.get("session") for o in occ}),
            "days": len({_day(o["ts"]) for o in occ}), "last": max(o["ts"] for o in occ),
            "workstreams": sorted({o["ws"] for o in occ if o.get("ws")}),
            "sources": sorted({o.get("source", "claude_code") for o in occ})}


def qualifies(ev):
    return ev["count"] >= MIN_COUNT and ev["sessions"] >= MIN_SESSIONS and ev["days"] >= MIN_DAYS


def mine(sessions=None, chat=None, now=None, settings=None):
    """Repeated command sequences and requests that qualify, best first. Pure apart from reading the cache."""
    now = store_.now_or(now)
    since = now - 30 * 86400
    sessions = _sessions(settings) if sessions is None else sessions
    out = []
    for label, occ in command_sequences(sessions).items():
        occ = [o for o in occ if o["ts"] >= since]
        if not occ:
            continue
        ev = _evidence(occ)
        if qualifies(ev):
            steps = label.split(" > ")
            # a git recipe starts by reading the repository's state: the one step a shadow run really runs
            pre = [{"shape": "git status", "class": "SAFE"}] if any(x.startswith("git ") for x in steps) else []
            out.append({"key": _key("commands", label), "kind": "commands", "label": label, "evidence": ev,
                        "recipe": pre + [{"shape": x, "class": classify(x)} for x in steps],
                        "value": ev["count"] * len(steps)})
    turns = [{"ts": t[0], "keywords": t[2], "ws": t[1], "session": d.get("session"), "source": "claude_code"}
             for d in sessions if not d.get("agent") for t in d.get("turns") or [] if t[0] >= since]
    for row in (activity.chat_turns(since) if chat is None else chat):
        turns.append({"ts": row["time"], "keywords": activity.keywords(row.get("request")) if
                      len(row.get("request") or "") <= 600 else [], "ws": None,
                      "session": f"chat:{_day(row['time'])}:{int(row['time'])}", "source": "chat"})
    for g in request_groups(turns):
        ev = _evidence(g["members"])
        if qualifies(ev):
            out.append({"key": _key("request", g["label"]), "kind": "request", "label": g["label"], "evidence": ev,
                        "recipe": None, "value": ev["count"] * 2})
    out.sort(key=lambda c: -c["value"])
    return out[:MAX_CANDIDATES]


# ---- store ------------------------------------------------------------------------------------------------------

def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("evidence", "recipe", "proof", "shadow_log", "workstreams"):
        d[k] = store_.loads(d.get(k), None)
    return d


def get(key):
    con = store_.db()
    try:
        return _row(con.execute("SELECT * FROM candidates WHERE key=?", (key,)).fetchone())
    finally:
        con.close()


def candidates(status=None):
    con = store_.db()
    try:
        q, a = "SELECT * FROM candidates", ()
        if status:
            q, a = q + " WHERE status=?", (status,)
        return [_row(r) for r in con.execute(q + " ORDER BY count DESC", a)]
    finally:
        con.close()


def save(found, now=None):
    """Insert new candidates and refresh the evidence of known ones (their status is kept)."""
    now = store_.now_or(now)
    con = store_.db()
    try:
        for c in found:
            ev = c["evidence"]
            con.execute("INSERT INTO candidates(key, kind, label, count, days, sessions, workstreams, evidence, "
                        "recipe, status, shadow_runs, created, updated) VALUES(?,?,?,?,?,?,?,?,?,'found',0,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET count=excluded.count, days=excluded.days, "
                        "sessions=excluded.sessions, workstreams=excluded.workstreams, evidence=excluded.evidence, "
                        "updated=excluded.updated",
                        (c["key"], c["kind"], c["label"], ev["count"], ev["days"], ev["sessions"],
                         store_.dumps(ev["workstreams"]), store_.dumps(ev), store_.dumps(c.get("recipe")), now, now))
        con.commit()
    finally:
        con.close()


def _set(key, **fields):
    fields["updated"] = time.time()
    con = store_.db()
    try:
        con.execute(f"UPDATE candidates SET {', '.join(f'{k}=?' for k in fields)} WHERE key=?",
                    (*[store_.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()], key))
        con.commit()
    finally:
        con.close()


# ---- proof and shadow runs --------------------------------------------------------------------------------------

def target_folder(cand, settings=None):
    """The project folder of the candidate's main workstream, or None."""
    s = settings or Settings.load()
    ws = (cand.get("evidence") or {}).get("workstreams") or []
    if not ws:
        return None
    paths = {Path(p).name.lower(): p for p in activity._known_repos(s)}
    p = paths.get(ws[0])
    return Path(p) if p and Path(p).is_dir() else None


def prove(key, settings=None, target=None):
    """The proof step, by code: a recipe exists, every step is classified, at least one step is worth automating,
    nothing in it spends money, and the project folder exists. Moves found -> shadow when it passes."""
    c = get(key)
    if not c:
        raise KeyError(key)
    checks = []
    recipe = c.get("recipe") or []
    checks.append({"check": "has a recipe", "ok": bool(recipe)})
    checks.append({"check": "every step classified", "ok": all(st.get("class") for st in recipe) if recipe
                   else False})
    checks.append({"check": "something worth automating", "ok": any(st.get("class") != "SAFE" for st in recipe)})
    folder = target if target is not None else target_folder(c, settings)
    checks.append({"check": "project folder exists", "ok": bool(folder and Path(folder).is_dir()),
                   "folder": str(folder) if folder else None})
    ok = all(x["ok"] for x in checks)
    _set(key, proof={"ok": ok, "checks": checks, "time": time.time()},
         status="shadow" if ok and c["status"] == "found" else c["status"])
    return ok, checks


def _git_read(shape, folder):
    sub = shape.split(" ", 1)[1]
    args = {"git log": ["log", "-n", "5", "--oneline"], "git diff": ["diff", "--stat"],
            "git branch": ["branch", "--list"], "git status": ["status", "--short"]}.get(shape, sub.split())
    # --no-optional-locks: a shadow run must not even refresh the repository's index file
    p = subprocess.run(["git", "--no-optional-locks", "-C", str(folder), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
    return p.returncode


def default_executor(step, folder, allow_tests=False):
    """Run one SAFE (or, when allowed, TEST) step in the folder; returns its exit code."""
    if step["class"] == "SAFE" and step["shape"].startswith("git "):
        return _git_read(step["shape"], folder)
    if step["class"] == "TEST" and allow_tests:
        from . import autowork
        return autowork.run_tests(TEST_RUNNERS[step["shape"]], folder)[0]
    return 0  # other read-only shapes (ls, cat...) say nothing about the automation: nothing to run


def shadow_run(key, settings=None, executor=None, target=None, allow_tests=False, now=None):
    """One shadow run with no outward effect: OUTWARD and LOCAL_WRITE steps are recorded, never run. Three
    passing runs make the candidate 'proposed'. Returns the run's record."""
    c = get(key)
    if not c:
        raise KeyError(key)
    if c["status"] not in ("shadow",):
        return {"skipped": f"status is {c['status']}"}
    folder = target if target is not None else target_folder(c, settings)
    run = {"time": store_.now_or(now), "steps": [], "ok": True}
    for st in c.get("recipe") or []:
        cls = st["class"]
        if cls in ("OUTWARD", "LOCAL_WRITE") or (cls == "TEST" and not allow_tests):
            run["steps"].append({"shape": st["shape"], "class": cls, "ran": False, "would_run": True})
            continue
        try:
            code = executor(st, folder) if executor else default_executor(st, folder, allow_tests)
        except Exception as e:  # noqa: BLE001 - a crashed step fails the run
            code = f"{type(e).__name__}: {e}"[:120]
        run["steps"].append({"shape": st["shape"], "class": cls, "ran": True, "would_run": False, "exit": code})
        if code != 0:
            run["ok"] = False
    log = (c.get("shadow_log") or [])[-9:] + [run]
    passed = c["shadow_runs"] + (1 if run["ok"] else 0)
    status = "proposed" if passed >= SHADOW_RUNS_NEEDED else "shadow"
    _set(key, shadow_log=log, shadow_runs=passed, status=status)
    run["shadow_runs"], run["status"] = passed, status
    return run


def enable(key):
    """The owner switches a proposed automation on. Only a proposed candidate can be enabled."""
    c = get(key)
    if not c:
        raise KeyError(key)
    if c["status"] != "proposed":
        raise ValueError(f"only a proposed automation can be enabled (this one is {c['status']}, "
                         f"{c['shadow_runs']} of {SHADOW_RUNS_NEEDED} shadow runs)")
    _set(key, status="enabled")
    return True


def reject(key):
    if not get(key):
        raise KeyError(key)
    _set(key, status="rejected")
    return True


def run(settings=None, now=None, executor=None, sessions=None, chat=None):
    """The core job autonomy.mine: mine, save, prove what is new, and shadow-run every candidate in shadow mode
    that was seen again since its last shadow run (one run per pass)."""
    s = settings or Settings.load()
    now = store_.now_or(now)
    found = mine(sessions, chat, now, s)
    save(found, now)
    proved, shadowed = [], []
    for c in candidates("found"):
        if c["kind"] == "commands" and not c.get("proof"):
            ok, _ = prove(c["key"], s)
            if ok:
                proved.append(c["key"])
    for c in candidates("shadow"):
        last_run = max([r["time"] for r in c.get("shadow_log") or []] or [0])
        last_seen = (c.get("evidence") or {}).get("last") or 0
        if not c.get("shadow_log") or last_seen > last_run:
            shadowed.append((c["key"], shadow_run(c["key"], s, executor, now=now)["ok"]))
    return {"candidates": len(found), "new_proofs": proved, "shadow_runs": shadowed,
            "proposed": [c["key"] for c in candidates("proposed")]}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv[:1] == ["enable"] and len(argv) == 2:
            enable(argv[1])
            print(f"{argv[1]} enabled")
        elif argv[:1] == ["reject"] and len(argv) == 2:
            reject(argv[1])
            print(f"{argv[1]} rejected")
        elif argv[:1] == ["run"]:
            print(json.dumps(run(), indent=2, default=str))
        else:
            for c in candidates():
                ev = c.get("evidence") or {}
                print(f"{c['key']}  [{c['status']}]  {c['kind']}: {c['label']}  x{ev.get('count')} in "
                      f"{ev.get('sessions')} sessions on {ev.get('days')} days  shadow {c['shadow_runs']}/"
                      f"{SHADOW_RUNS_NEEDED}")
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
