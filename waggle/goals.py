"""Standing goals with limits: what Waggle works toward between conversations, and what it may do on its own.

A goal has
  title, why     what and what for, in the owner's words
  indicators     1-3 measurable numbers, each with how it is measured (a `measure` that code evaluates; see
                 MEASURES). A model's report never sets an indicator.
  charter        the limits of background work for this goal:
                   actions      which kinds of step it may take on its own (ACTION_KINDS)
                   write_roots  where it may write (templates: {home}, {vault}, {jobs}, {waggle_home}, {goal})
                   channels     which outward channels it may use (CHANNELS; the seeds use none: results reach the
                                owner only through the evening note)
                   money        always 0; any other value is refused
                   expires      ISO date after which the goal stops (it must be renewed by the owner)
  status         disabled (default) | enabled | expired

Goals live in autonomy.db (never in the repository). The single switch for all autonomous work is
`autonomy_enabled` (default off) next to `max_steps_per_day`, in the same database:

    python -m waggle.goals list | show <id> | add <id> --title ... | enable <id> | disable <id> | seed
    python -m waggle.goals autonomy on|off | limit <steps per day>

Charter boundary for the permission rules (interface only; nothing here is wired into policy.py, guard.py,
agent_hook.py or security.py, and background work keeps today's rules until it is):

  charter_for(session=..., objective_id=..., goal_id=...) returns, for background work that belongs to a goal,
      {"goal": id, "charter": normalised charter, "write_roots": [absolute paths], "expired": bool}
  and None for everything else (chat turns, the owner's own objectives, routines): None means "today's rules
  only". A work-loop step runs as the agent session `objective-<id>` (agents.run), which is how it is found:
  the objective's origin is 'autowork' and its goal is named in it; afterwards autonomy.db steps maps
  objective_id -> goal_id. Autonomous work whose goal cannot be found gets an empty, expired charter.

  classify_call(tool_name, args) maps one tool call to what a charter speaks about:
      {"action": research | write_note | edit_code | run_tests | commit_local, "path": file written or None,
       "channel": outward channel or None, "money": 0 | 1}

  How policy would consult it, in this order, inside policy.check(tool, args, session=...):
    1. d = decide(tool, args)               today's rules first, unchanged: a DENY stays a DENY
    2. info = goals.charter_for(session=session)
       if info is None: return d            not autonomous background work
    3. if info["expired"]: DENY "the goal is disabled or its charter expired"
    4. ok, why = goals.call_allowed(info, tool, args)
       if not ok: DENY "outside the goal's charter: " + why
       (call_allowed = classify_call + charter_allows: action kind, written path inside the write roots,
        outward channel listed, money always 0)
    5. if d is ASK: DENY "needs the owner, who is not watching"; the step ends as needs_approval and the evening
       note says so, instead of an approval code waiting overnight
    6. return d                             the charter only narrows today's rules, it never widens them
"""
import argparse
import datetime as dt
import re
import sys
import time
from pathlib import Path

from . import autonomy_store as store_
from .config import Settings

ACTION_KINDS = ("research", "write_note", "edit_code", "run_tests", "commit_local")
CHANNELS = ("evening_note", "telegram", "email", "github_push", "github_pr", "web_post")
OUTWARD = tuple(c for c in CHANNELS if c != "evening_note")
MEASURES = {
    "proven_steps": "steps of this goal whose outcome the proof step verified, in the last 7 days",
    "files_changed": "files under `root` (a write-root template) modified in the last 7 days",
    "commits": "commits in the last 7 days in the workstreams named in `repos` (all when empty), from the "
               "activity model's HEAD-log reading",
    "tests_failing": "failing tests reported by an allowed test command `cmd` run in `cwd`",
}
STATUSES = ("disabled", "enabled", "expired")
DEFAULT_MAX_STEPS = 2
SEED_DAYS = 90


class CharterError(ValueError):
    pass


# ---- the switch ------------------------------------------------------------------------------------------------

def autonomy_enabled():
    return bool(store_.get("autonomy_enabled", False))


def set_autonomy(on):
    store_.put("autonomy_enabled", bool(on))
    return autonomy_enabled()


def max_steps_per_day():
    try:
        return max(0, int(store_.get("max_steps_per_day", DEFAULT_MAX_STEPS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_STEPS


def set_max_steps(n):
    store_.put("max_steps_per_day", max(0, int(n)))
    return max_steps_per_day()


# ---- charter ----------------------------------------------------------------------------------------------------

def check_charter(charter):
    """A valid charter (normalised copy) or CharterError. Money other than 0 is never accepted."""
    c = dict(charter or {})
    if float(c.get("money", 0) or 0) != 0:
        raise CharterError("a charter may never spend money (money must be 0)")
    c["money"] = 0
    acts = list(c.get("actions") or [])
    bad = [a for a in acts if a not in ACTION_KINDS]
    if bad:
        raise CharterError(f"unknown action kind(s): {bad}; allowed: {list(ACTION_KINDS)}")
    chans = list(c.get("channels") or [])
    bad = [x for x in chans if x not in CHANNELS]
    if bad:
        raise CharterError(f"unknown channel(s): {bad}; known: {list(CHANNELS)}")
    roots = [str(r) for r in c.get("write_roots") or []]
    if ("write_note" in acts or "edit_code" in acts or "commit_local" in acts) and not roots:
        raise CharterError("a charter that writes needs at least one write root")
    exp = c.get("expires")
    if not exp:
        raise CharterError("a charter needs an expiry date (expires: YYYY-MM-DD)")
    try:
        dt.date.fromisoformat(str(exp)[:10])
    except ValueError:
        raise CharterError(f"expires is not a date: {exp!r}") from None
    c.update(actions=acts, channels=chans, write_roots=roots, expires=str(exp)[:10])
    return c


def expand_roots(charter, goal_id="", settings=None):
    """The charter's write roots as absolute, resolved paths."""
    s = settings or Settings.load()
    from . import config
    subs = {"home": str(Path.home()), "vault": s.vault, "jobs": s.jobs_dir, "waggle_home": str(config.HOME),
            "goal": goal_id}
    out = []
    for r in (charter or {}).get("write_roots") or []:
        try:
            out.append(Path(str(r).format(**subs)).expanduser().resolve())
        except (KeyError, IndexError, ValueError):
            continue
    return out


def _inside(path, roots):
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return any(p == r or r in p.parents for r in roots)


def expired(charter, now=None):
    today = dt.date.fromtimestamp(store_.now_or(now))
    try:
        return dt.date.fromisoformat(str((charter or {}).get("expires"))[:10]) < today
    except ValueError:
        return True


def charter_allows(charter, *, action=None, path=None, channel=None, money=0, goal_id="", settings=None, now=None):
    """(allowed, reason) for one intended act under a charter. Every argument given must pass:
    action in charter.actions; path inside one of the write roots; channel in charter.channels; money == 0;
    and the charter not expired. Pure apart from resolving paths."""
    try:
        c = check_charter(charter)
    except CharterError as e:
        return False, f"invalid charter: {e}"
    if expired(c, now):
        return False, f"the charter expired on {c['expires']}"
    if money:
        return False, "spending money is never allowed"
    if action is not None and action not in c["actions"]:
        return False, f"action '{action}' is not in the charter ({', '.join(c['actions']) or 'none'})"
    if path is not None and not _inside(path, expand_roots(c, goal_id, settings)):
        return False, f"{path} is outside the charter's write roots"
    if channel is not None and channel not in c["channels"]:
        return False, f"channel '{channel}' is not in the charter"
    return True, "ok"


# ---- the permission rules' view -------------------------------------------------------------------------------

SHELL = {"bash", "powershell"}
WRITE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
READ_TOOLS = {"read", "glob", "grep", "ls", "websearch", "webfetch", "notebookread", "todowrite", "toolsearch"}
TEST_SHAPES = ("python -m pytest", "npm test", "node --test")
_OUT_SHAPES = {"git push": "github_push", "gh pr create": "github_pr", "gh pr merge": "github_pr",
               "gh pr comment": "github_pr", "gh issue create": "github_pr", "gh issue comment": "github_pr",
               "gh release create": "github_push", "curl": "web_post", "wget": "web_post",
               "invoke-webrequest": "web_post", "invoke-restmethod": "web_post", "hermes send": "telegram",
               "send-mailmessage": "email", "npm publish": "web_post"}
_SEND = re.compile(r"(^|_)(send|reply|forward|post|publish|tweet|dm|share|invite)(_|$)", re.I)
_MONEY = re.compile(r"payment|purchase|checkout|transfer|trade|order|billing|invoice|refund|buy", re.I)


def classify_call(tool_name, args=None, settings=None):
    """What one tool call means in charter terms: {action, path, channel, money}."""
    from . import activity
    s = settings or Settings.load()
    args = args if isinstance(args, dict) else {}
    name = (tool_name or "").lower()
    base = name.split("__")[-1]
    out = {"action": "research", "path": None, "channel": None, "money": 0}
    if _MONEY.search(base):
        out["money"] = 1
    if name in WRITE_TOOLS:
        p = str(args.get("file_path") or args.get("notebook_path") or "")
        out["path"] = p
        try:
            vault, target = Path(s.vault).resolve(), Path(p).resolve()
            in_vault = vault == target or vault in target.parents
        except (OSError, ValueError):
            in_vault = False
        out["action"] = "write_note" if in_vault and p.lower().endswith(".md") else "edit_code"
    elif name in SHELL:
        shape = activity.command_shape(str(args.get("command") or ""))
        parts = shape.split(" > ") if shape else []
        for part in parts:
            if part in _OUT_SHAPES or (part.startswith("gh ") and not part.endswith((" view", " list", " status",
                                                                                      " checks", " diff"))):
                out["channel"] = _OUT_SHAPES.get(part, "github_pr")
        if any(x in ("git commit", "git merge", "git rebase", "git tag") for x in parts):
            out["action"] = "commit_local"
        elif parts and any(x in TEST_SHAPES for x in parts) and all(
                x in TEST_SHAPES or activity.is_read_only(x) for x in parts):
            out["action"] = "run_tests"
        elif parts and not activity.is_read_only(shape):
            out["action"] = "edit_code"  # any other command may write: it must be allowed to change code
    elif base in READ_TOOLS:
        out["action"] = "research"
    elif _SEND.search(base):
        out["channel"] = "email" if "mail" in name else "telegram" if "telegram" in name else "web_post"
    return out


def charter_for(session=None, objective_id=None, goal_id=None, settings=None, now=None):
    """The charter that bounds this background work, or None when the work belongs to no goal (then only today's
    permission rules apply). session is the agent session name ('objective-<id>')."""
    if session and str(session).startswith("objective-") and not objective_id:
        objective_id = str(session).split("-", 1)[1]
    if objective_id and not goal_id:
        con = store_.db()
        try:
            row = con.execute("SELECT goal_id FROM steps WHERE objective_id=? ORDER BY time DESC LIMIT 1",
                              (objective_id,)).fetchone()
        finally:
            con.close()
        goal_id = row[0] if row else None
        if goal_id is None:  # the step is recorded after it runs; while it runs the objective itself says it
            try:
                from . import objectives
                con = objectives._db()
                row = con.execute("SELECT origin, goal FROM objectives WHERE id=?", (objective_id,)).fetchone()
                con.close()
            except Exception:  # noqa: BLE001
                row = None
            if row and row[0] in ("autowork", "shadow"):
                m = re.match(r"\[(.+?)\]", row[1] or "")
                hit = next((g for g in all_goals() if m and g["title"] == m.group(1)), None)
                if hit is None:  # autonomous work whose goal cannot be found: the narrowest charter there is
                    return {"goal": None, "charter": {"actions": [], "write_roots": [], "channels": [], "money": 0,
                                                      "expires": "1970-01-01"}, "write_roots": [], "expired": True}
                goal_id = hit["id"]
    if not goal_id:
        return None
    g = get(goal_id)
    if not g:
        return None
    c = check_charter(g["charter"])
    return {"goal": goal_id, "charter": c, "write_roots": [str(r) for r in expand_roots(c, goal_id, settings)],
            "expired": expired(c, now) or g["status"] != "enabled"}


def call_allowed(info, tool_name, args=None, settings=None, now=None):
    """(allowed, reason) for one tool call under charter_for()'s result."""
    if info is None:
        return True, "no charter: today's rules only"
    if info.get("expired"):
        return False, "the goal is disabled or its charter expired"
    c = classify_call(tool_name, args, settings)
    return charter_allows(info["charter"], action=c["action"], path=c["path"], channel=c["channel"],
                          money=c["money"], goal_id=info["goal"] or "", settings=settings, now=now)


# ---- store ------------------------------------------------------------------------------------------------------

def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["indicators"] = store_.loads(d.get("indicators"), [])
    d["charter"] = store_.loads(d.get("charter"), {})
    return d


def check_indicators(indicators):
    out = []
    for ind in indicators or []:
        m = dict(ind.get("measure") or {})
        if m.get("type") not in MEASURES:
            raise ValueError(f"indicator {ind.get('name')!r}: measure type must be one of {list(MEASURES)}")
        if not ind.get("name"):
            raise ValueError("every indicator needs a name")
        out.append({"name": str(ind["name"])[:60], "how": str(ind.get("how") or MEASURES[m["type"]])[:200],
                    "measure": m})
    if len(out) > 3:
        raise ValueError("at most three indicators per goal")
    return out


def add(goal_id, title, why="", indicators=None, charter=None, status="disabled", now=None):
    """Create a goal (disabled unless said otherwise). Raises on an invalid charter or indicator."""
    gid = re.sub(r"[^a-z0-9-]+", "-", goal_id.lower()).strip("-")
    if not gid or not title:
        raise ValueError("a goal needs an id and a title")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    c = check_charter(charter)
    inds = check_indicators(indicators)
    now = store_.now_or(now)
    con = store_.db()
    try:
        con.execute("INSERT INTO goals(id, title, why, indicators, charter, status, created, updated) "
                    "VALUES(?,?,?,?,?,?,?,?)", (gid, title, why, store_.dumps(inds), store_.dumps(c), status, now,
                                                now))
        con.commit()
    finally:
        con.close()
    return gid


def get(goal_id):
    con = store_.db()
    try:
        return _row(con.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())
    finally:
        con.close()


def all_goals(status=None):
    con = store_.db()
    try:
        q, args = "SELECT * FROM goals", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [_row(r) for r in con.execute(q + " ORDER BY created, id", args)]
    finally:
        con.close()


def _set(goal_id, **fields):
    fields["updated"] = time.time()
    con = store_.db()
    try:
        n = con.execute(f"UPDATE goals SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                        (*fields.values(), goal_id)).rowcount
        con.commit()
    finally:
        con.close()
    return n == 1


def enable(goal_id, now=None):
    g = get(goal_id)
    if not g:
        raise KeyError(goal_id)
    check_charter(g["charter"])
    if expired(g["charter"], now):
        raise CharterError(f"the charter expired on {g['charter']['expires']}; renew it first")
    return _set(goal_id, status="enabled")


def disable(goal_id):
    if not get(goal_id):
        raise KeyError(goal_id)
    return _set(goal_id, status="disabled")


def renew(goal_id, expires):
    g = get(goal_id)
    if not g:
        raise KeyError(goal_id)
    c = check_charter({**g["charter"], "expires": expires})
    return _set(goal_id, charter=store_.dumps(c))


def enabled_goals(now=None):
    """Enabled goals whose charter has not expired; expired ones are marked expired on the way."""
    out = []
    for g in all_goals("enabled"):
        if expired(g["charter"], now):
            _set(g["id"], status="expired")
            continue
        out.append(g)
    return out


def touch(goal_id, now=None):
    _set(goal_id, last_step=store_.now_or(now))


# ---- seeds ------------------------------------------------------------------------------------------------------

def seed_specs(now=None):
    exp = (dt.date.fromtimestamp(store_.now_or(now)) + dt.timedelta(days=SEED_DAYS)).isoformat()
    notes = "{vault}/Goals/{goal}"
    proven = {"name": "proven steps this week", "measure": {"type": "proven_steps"}}
    return [
        {"id": "schoolwork", "title": "Schoolwork help",
         "why": "Keep coursework, tests and deadlines prepared ahead of time.",
         "indicators": [proven, {"name": "study notes touched this week",
                                 "measure": {"type": "files_changed", "root": notes}}],
         "charter": {"actions": ["research", "write_note"], "write_roots": [notes], "channels": [], "money": 0,
                     "expires": exp}},
        {"id": "ftc-robot", "title": "FTC robot work",
         "why": "Move the robot's design, code and match preparation forward.",
         "indicators": [proven, {"name": "robot notes touched this week",
                                 "measure": {"type": "files_changed", "root": notes}}],
         "charter": {"actions": ["research", "write_note"], "write_roots": [notes], "channels": [], "money": 0,
                     "expires": exp}},
        {"id": "portfolio", "title": "Portfolio repositories",
         "why": "Keep the public portfolio projects moving every week.",
         "indicators": [proven, {"name": "commits this week", "measure": {"type": "commits", "repos": []}}],
         "charter": {"actions": ["research", "edit_code", "run_tests", "commit_local"],
                     "write_roots": ["{home}/autonomy-worktrees/{goal}", notes], "channels": [], "money": 0,
                     "expires": exp}},
        {"id": "waggle", "title": "Waggle itself",
         "why": "Make Waggle more reliable and more useful, with proof.",
         "indicators": [proven, {"name": "Waggle commits this week",
                                 "measure": {"type": "commits", "repos": ["waggle"]}}],
         "charter": {"actions": ["research", "edit_code", "run_tests", "commit_local"],
                     "write_roots": ["{home}/autonomy-worktrees/{goal}", notes], "channels": [], "money": 0,
                     "expires": exp}},
    ]


def seed(now=None):
    """Create the four starting goals, DISABLED, when they do not exist yet. Returns the ids created."""
    made = []
    for spec in seed_specs(now):
        if get(spec["id"]):
            continue
        add(spec["id"], spec["title"], spec["why"], spec["indicators"], spec["charter"], "disabled", now)
        made.append(spec["id"])
    return made


# ---- command line -----------------------------------------------------------------------------------------------

def _show(g):
    c = g["charter"]
    lines = [f"{g['id']}  [{g['status']}]  {g['title']}", f"  why: {g['why']}"]
    for ind in g["indicators"]:
        lines.append(f"  indicator: {ind['name']}  ({ind['how']})")
    lines.append(f"  charter: actions={','.join(c.get('actions', [])) or '-'}  "
                 f"channels={','.join(c.get('channels', [])) or 'none'}  money=0  expires={c.get('expires')}")
    for r in c.get("write_roots", []):
        lines.append(f"  may write under: {r}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m waggle.goals")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list")
    sub.add_parser("seed")
    for name in ("show", "enable", "disable"):
        sub.add_parser(name).add_argument("id")
    a = sub.add_parser("add")
    a.add_argument("id")
    a.add_argument("--title", required=True)
    a.add_argument("--why", default="")
    a.add_argument("--action", action="append", default=[], choices=ACTION_KINDS)
    a.add_argument("--root", action="append", default=[], help="write root (templates: {home} {vault} {goal})")
    a.add_argument("--expires", default=(dt.date.today() + dt.timedelta(days=30)).isoformat())
    a.add_argument("--indicator", action="append", default=[],
                   help="name=type[:arg], e.g. 'notes=files_changed:{vault}/School' or 'done=proven_steps'")
    r = sub.add_parser("renew")
    r.add_argument("id")
    r.add_argument("expires")
    au = sub.add_parser("autonomy")
    au.add_argument("state", choices=["on", "off", "status"])
    lim = sub.add_parser("limit")
    lim.add_argument("steps", type=int)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.cmd in (None, "list"):
            gs = all_goals()
            print(f"autonomy: {'ON' if autonomy_enabled() else 'off'}; at most {max_steps_per_day()} steps a day")
            for g in gs:
                print(f"  {g['id']:12} [{g['status']:8}] {g['title']}  (expires {g['charter'].get('expires')})")
            if not gs:
                print("  no goals yet: python -m waggle.goals seed")
        elif args.cmd == "seed":
            print("created (disabled): " + (", ".join(seed()) or "nothing, all exist"))
        elif args.cmd == "show":
            g = get(args.id)
            print(_show(g) if g else f"no goal {args.id}")
            return 0 if g else 1
        elif args.cmd == "enable":
            enable(args.id)
            print(f"{args.id} enabled" + ("" if autonomy_enabled() else
                                          " (autonomy is off: python -m waggle.goals autonomy on)"))
        elif args.cmd == "disable":
            disable(args.id)
            print(f"{args.id} disabled")
        elif args.cmd == "renew":
            renew(args.id, args.expires)
            print(f"{args.id} renewed until {args.expires}")
        elif args.cmd == "add":
            inds = []
            for spec in args.indicator:
                name, _, rest = spec.partition("=")
                typ, _, arg = rest.partition(":")
                m = {"type": typ}
                if typ == "files_changed":
                    m["root"] = arg
                elif typ == "commits":
                    m["repos"] = [x for x in arg.split(",") if x]
                inds.append({"name": name, "measure": m})
            gid = add(args.id, args.title, args.why, inds, {"actions": args.action, "write_roots": args.root,
                                                            "channels": [], "money": 0, "expires": args.expires})
            print(f"{gid} added (disabled)")
        elif args.cmd == "autonomy":
            if args.state != "status":
                set_autonomy(args.state == "on")
            print(f"autonomy: {'ON' if autonomy_enabled() else 'off'}")
        elif args.cmd == "limit":
            print(f"at most {set_max_steps(args.steps)} steps a day")
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
