"""Google through gogcli (https://github.com/openclaw/gogcli, MIT), pinned, for the core and for the chat brain.

    python -m waggle.google status                 signed in? which account? (no network)
    python -m waggle.google install                download the pinned gog release, check its SHA-256, unpack
    python -m waggle.google docs-new TITLE [--text T]
    python -m waggle.google tasks                  open Google Tasks
    python -m waggle.google classroom-due [DAYS]   Classroom work due soon, not yet turned in (default 7 days)

No Claude is needed for any of this. gog keeps its OAuth refresh token in the Windows Credential Manager and its
client file under ~/.waggle-tools/gog/home (outside AppData and outside the repo). Nothing here reads or prints a token.

Safety, in code and not in a prompt:
  * every call allows exactly ONE gog command (--enable-commands-exact), so no delete, share, permission or
    Gmail command can run even if a bug builds the wrong argument list; OPS below is the whole list;
  * --gmail-no-send, --wrap-untrusted and --no-input on every call (output text is data, never a prompt);
  * no attendee, share or send argument exists in any tool, so an event or doc can only be the owner's own;
  * the chat tools that create or change something refuse outside the owner's own turn (the policy asks too).
The switch settings.google_gog (default true) is inert until someone has signed in (docs/GOOGLE.md).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .config import Settings

GOG_VERSION = "0.48.1"
GOG_ZIP = f"gogcli_{GOG_VERSION}_windows_amd64.zip"
GOG_URL = f"https://github.com/openclaw/gogcli/releases/download/v{GOG_VERSION}/{GOG_ZIP}"
GOG_SHA256 = "339d764d2e1b1f57f4b3efeaef72ef933950f3929cc72387af468ff1b62db61a"  # from the release's checksums.txt
SERVICES = "calendar,tasks,docs,drive,classroom"  # what the one-time sign-in asks for (no Gmail at all)
SIGN_IN_HINT = "Google is not signed in (see docs/GOOGLE.md)"

# op name -> the gog command path. Each call allows exactly that one path.
OPS = {
    "docs_new": ("docs", "create"), "docs_read": ("docs", "cat"), "docs_append": ("docs", "write"),
    "drive_search": ("drive", "search"),
    "calendar_list": ("calendar", "events"), "calendar_new": ("calendar", "create"),
    "tasks_lists": ("tasks", "lists", "list"), "tasks_list": ("tasks", "list"), "tasks_add": ("tasks", "add"),
    "tasks_done": ("tasks", "done"),
    "classroom_courses": ("classroom", "courses", "list"), "classroom_work": ("classroom", "coursework", "list"),
    "classroom_mine": ("classroom", "submissions", "list"),
    "auth_list": ("auth", "list"),  # the core's own status check; no chat tool calls it
}
WRITE_OPS = {"docs_new", "docs_append", "calendar_new", "tasks_add", "tasks_done"}
MAX_TEXT = 20000
_ID = re.compile(r"^[A-Za-z0-9_@.\-]{1,200}$")
_WRAP = re.compile(r"<<<EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>\s*Source:[^\n]*\n---\n(.*?)\n?<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>",
                   re.S)


class GoogleError(Exception):
    """kind: off | missing | signin | bad | error. The message is plain words for the owner."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


# ---- finding and running gog -------------------------------------------------------------------------------------

def tools_dir():
    return Path(os.environ.get("WAGGLE_TOOLS_DIR") or Path.home() / ".waggle-tools") / "gog"


def gog_home():
    return tools_dir() / "home"


def gog_exe():
    """The pinned gog.exe, or None when it is not installed. WAGGLE_GOG_EXE overrides (tests)."""
    env = os.environ.get("WAGGLE_GOG_EXE")
    p = Path(env) if env else tools_dir() / f"v{GOG_VERSION}" / ("gog.exe" if os.name == "nt" else "gog")
    return p if p.is_file() else None


def enabled(settings=None):
    s = settings if settings is not None else Settings.load()
    return bool(getattr(s, "google_gog", True))


def unwrap(value):
    """gog marks fetched text with EXTERNAL_UNTRUSTED_CONTENT lines; give back the plain text inside (recursively)."""
    if isinstance(value, str):
        m = _WRAP.fullmatch(value.strip())
        return m.group(1) if m else value
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    return value


def _env():
    # Nothing in the environment may swap the account, the token or the folder (gog also reads GOG_* variables).
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GOG_")}


def _spawn(argv, stdin, timeout):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=_env(), creationflags=flags)


def _run(op, flags=(), positional=(), stdin=None, timeout=45, account=None, runner=None):
    """One gog call. Returns (exit code, parsed JSON or text, stderr). Raises GoogleError for off / missing / signin."""
    exe = gog_exe()
    if exe is None:
        raise GoogleError("gog is not installed (run: python -m waggle.google install)", "missing")
    path = OPS[op]
    cmd = [str(exe), "--home", str(gog_home())]
    if account:
        cmd += ["--account", account]
    cmd += ["--enable-commands-exact", ".".join(path), "--gmail-no-send", "--wrap-untrusted", "--no-input", "--json",
            *path, *flags]
    if positional:
        cmd += ["--", *map(str, positional)]
    try:
        r = (runner or _spawn)(cmd, stdin, timeout)
    except subprocess.TimeoutExpired:
        raise GoogleError("Google did not answer in time", "error") from None
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
    except ValueError:
        data = out
    err = (r.stderr or "").strip()
    if r.returncode in (4, 10):
        raise GoogleError(SIGN_IN_HINT, "signin")
    return r.returncode, data, err


def _call(op, flags=(), positional=(), stdin=None, timeout=45, runner=None, acct=None):
    """_run for a signed-in account; data on success, GoogleError otherwise (exit 3 = nothing found = {})."""
    code, data, err = _run(op, flags, positional, stdin, timeout, account=acct, runner=runner)
    if code == 3:
        return {}
    if code != 0:
        msg = ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            msg = str(data["error"].get("message") or "")
        raise GoogleError((msg or err or f"gog exited with {code}")[:300], "error")
    return unwrap(data)


# ---- account and status ------------------------------------------------------------------------------------------

_ACCOUNTS = {}  # one answer per half minute: a brief makes several calls in a row


def accounts(runner=None):
    """Signed-in account emails from gog's own list (local; no network). [] when not installed or none."""
    hit = _ACCOUNTS.get("v")
    if hit and time.monotonic() - hit[0] < 30 and runner is None:
        return hit[1]
    try:
        code, data, _ = _run("auth_list", runner=runner, timeout=15)
    except GoogleError:
        return []
    rows = data.get("accounts") if isinstance(data, dict) else None
    out = []
    for a in rows or []:
        e = a.get("email") if isinstance(a, dict) else a
        if isinstance(e, str) and "@" in e:
            out.append(e)
    if runner is None:
        _ACCOUNTS["v"] = (time.monotonic(), out)
    return out


def account(settings=None):
    s = settings if settings is not None else Settings.load()
    want = str(getattr(s, "google_account", "") or "").strip()
    have = accounts()
    if want:
        return want if want in have else None
    return have[0] if have else None


def status(settings=None):
    """{"enabled", "installed", "signed_in", "account", "detail"}: what `doctor` and `status` show."""
    s = settings if settings is not None else Settings.load()
    out = {"enabled": enabled(s), "installed": gog_exe() is not None, "signed_in": False, "account": "", "detail": ""}
    if not out["enabled"]:
        out["detail"] = "off (google_gog is false)"
    elif not out["installed"]:
        out["detail"] = "gog is not installed (python -m waggle.google install)"
    else:
        a = account(s)
        out["signed_in"], out["account"] = bool(a), a or ""
        out["detail"] = f"signed in as {a}" if a else "not signed in (see docs/GOOGLE.md)"
    return out


def ready(settings=None):
    """True only when the switch is on, gog is there and someone has signed in. Everything else stays inert."""
    return bool(status(settings)["signed_in"])


def _need_ready(settings=None):
    """The signed-in account's email, or GoogleError (switched off / not installed / not signed in)."""
    st = status(settings)
    if not st["enabled"]:
        raise GoogleError("Google access is switched off (google_gog is false in config.json)", "off")
    if not st["installed"]:
        raise GoogleError(st["detail"], "missing")
    if not st["signed_in"]:
        raise GoogleError(SIGN_IN_HINT, "signin")
    return st["account"]


# ---- install -----------------------------------------------------------------------------------------------------

def install(fetch=None):
    """Download the pinned release from GitHub, refuse it unless its SHA-256 matches, unpack gog.exe. Returns the path."""
    dest = tools_dir() / f"v{GOG_VERSION}"
    if (dest / "gog.exe").is_file():
        return dest / "gog.exe"
    tmp = Path(tempfile.mkdtemp(prefix="gog-dl-"))
    try:
        blob = (fetch or _download)(GOG_URL)
        got = hashlib.sha256(blob).hexdigest()
        if got != GOG_SHA256:
            raise GoogleError(f"the download does not match the pinned checksum ({got[:12]}...); nothing was installed")
        z = tmp / GOG_ZIP
        z.write_bytes(blob)
        with zipfile.ZipFile(z) as zf:
            if "gog.exe" not in zf.namelist():
                raise GoogleError("the release zip has no gog.exe; nothing was installed")
            dest.mkdir(parents=True, exist_ok=True)
            with zf.open("gog.exe") as src, open(dest / "gog.exe", "wb") as out:
                shutil.copyfileobj(src, out)
        return dest / "gog.exe"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _download(url):
    if not url.startswith("https://github.com/openclaw/gogcli/releases/download/"):
        raise GoogleError("refusing to download from anywhere but the gogcli GitHub release")
    req = urllib.request.Request(url, headers={"User-Agent": "waggle-google/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


# ---- small readers for gog's JSON (it passes Google's own field names through) ------------------------------------

def _items(data, *keys):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


def _first(d, *keys, default=""):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _find_key(data, key):
    """The first value stored under `key` anywhere in the data (ids and links sit at different depths)."""
    if isinstance(data, dict):
        if data.get(key) not in (None, ""):
            return data[key]
        for v in data.values():
            r = _find_key(v, key)
            if r not in (None, ""):
                return r
    elif isinstance(data, list):
        for v in data:
            r = _find_key(v, key)
            if r not in (None, ""):
                return r
    return ""


def _check_id(value, what):
    v = str(value or "").strip()
    if not _ID.match(v):
        raise GoogleError(f"that is not a valid {what}", "bad")
    return v


def _check_text(text, what="text"):
    t = str(text or "")
    if len(t) > MAX_TEXT:
        raise GoogleError(f"the {what} is longer than {MAX_TEXT} characters; send it in parts", "bad")
    return t


def _ts(value):
    """ISO date or date-time text -> one with an explicit offset (a bare local time gets the PC's offset)."""
    v = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise GoogleError(f"'{v}' is not a date or time like 2026-11-24 or 2026-11-24T15:30", "bad") from None
    if d.tzinfo is None:
        d = d.astimezone()
    return d.isoformat(timespec="seconds")


# ---- operations --------------------------------------------------------------------------------------------------

def docs_new(title, text="", settings=None, runner=None):
    """Create a Google Doc (optionally with starting text). Returns {"id", "title", "url"}."""
    acct = _need_ready(settings)
    title = str(title or "").strip()
    if not title:
        raise GoogleError("a document needs a title", "bad")
    text = _check_text(text)
    d = _call("docs_new", positional=[title], runner=runner, acct=acct)
    doc_id = str(_find_key(d, "id") or "")
    if not doc_id:
        raise GoogleError("Google made the document but did not say its id", "error")
    if text.strip():
        docs_append(doc_id, text, settings=settings, runner=runner)
    return {"id": doc_id, "title": title,
            "url": str(_find_key(d, "webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit")}


def docs_append(doc_id, text, settings=None, runner=None):
    acct = _need_ready(settings)
    doc_id = _check_id(doc_id, "document id")
    text = _check_text(text)
    if not text.strip():
        raise GoogleError("there is nothing to add", "bad")
    _call("docs_append", ["--append", "--file", "-"], [doc_id], stdin=text, runner=runner, acct=acct)  # text on stdin: any length
    return {"id": doc_id, "added_chars": len(text)}


def docs_read(doc_id, max_chars=20000, settings=None, runner=None):
    acct = _need_ready(settings)
    d = _call("docs_read", [f"--max-bytes={int(max_chars) * 4}"], [_check_id(doc_id, "document id")], runner=runner,
              acct=acct)
    text = d if isinstance(d, str) else str(_first(d, "text", "content", default=json.dumps(d)))
    return text[:max_chars]


def drive_search(query, max_results=10, settings=None, runner=None):
    acct = _need_ready(settings)
    q = str(query or "").strip()
    if not q:
        raise GoogleError("a search needs some words", "bad")
    d = _call("drive_search", [f"--max={max(1, min(int(max_results), 25))}"], [q], runner=runner, acct=acct)
    return [{"id": _first(f, "id"), "name": _first(f, "name"), "type": _first(f, "mimeType"),
             "modified": _first(f, "modifiedTime"), "url": _first(f, "webViewLink")} for f in _items(d, "files")]


def calendar_list(days=7, max_results=25, settings=None, runner=None):
    acct = _need_ready(settings)
    d = _call("calendar_list", [f"--days={max(1, min(int(days), 60))}", f"--max={max(1, min(int(max_results), 50))}"],
              runner=runner, acct=acct)
    out = []
    for e in _items(d, "events"):
        st, en = e.get("start"), e.get("end")
        start, when = local_time(_first(st, "dateTime", "date") if isinstance(st, dict) else str(st or ""))
        end, _ = local_time(_first(en, "dateTime", "date") if isinstance(en, dict) else str(en or ""))
        out.append({"id": _first(e, "id"), "title": _first(e, "summary", default="(no title)"),
                    "start": start, "end": end, "when": when, "where": _first(e, "location")})
    return out


def local_time(value):
    """A Google start/end in the PC's own time zone: (ISO with the local offset, "Tue Dec 1, 4:00 PM").
    Google can hand back UTC ("2026-12-02T00:00:00Z" for Tuesday 4 PM in California); read as a local date that was
    Wednesday, and the thinking pass offered to "fix" a correct practice (2026-11-25)."""
    v = str(value or "")
    if v and "T" not in v:  # an all-day date
        try:
            d = dt.date.fromisoformat(v[:10])
            return v, f"{d:%a %b} {d.day} (all day)"
        except ValueError:
            return v, v
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return v, v
    t = t.astimezone() if t.tzinfo else t
    return t.isoformat(timespec="minutes"), f"{t:%a %b} {t.day}, {t.hour % 12 or 12}:{t:%M} {t:%p}"


def calendar_new(title, start, end="", description="", location="", settings=None, runner=None):
    """A new event on the owner's own primary calendar. No attendees and no invitations exist in this call."""
    acct = _need_ready(settings)
    title = str(title or "").strip()
    if not title:
        raise GoogleError("an event needs a title", "bad")
    s = _ts(start)
    if not end:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):  # all day: Google's end date is the day after
            e = (dt.date.fromisoformat(s) + dt.timedelta(days=1)).isoformat()
        else:
            e = (dt.datetime.fromisoformat(s) + dt.timedelta(hours=1)).isoformat(timespec="seconds")
    else:
        e = _ts(end)
    flags = [f"--summary={title}", f"--from={s}", f"--to={e}", "--send-updates=none"]
    if description:
        flags.append(f"--description={_check_text(description, 'description')}")
    if location:
        flags.append(f"--location={str(location)[:300]}")
    d = _call("calendar_new", flags, ["primary"], runner=runner, acct=acct)
    return {"id": str(_find_key(d, "id") or ""), "title": title, "start": s, "end": e,
            "url": str(_find_key(d, "htmlLink") or "")}


def tasks_open(limit=20, settings=None, runner=None):
    """Open (not completed) tasks of the default list, soonest due first; each {"id","title","due","notes"}."""
    acct = _need_ready(settings)
    d = _call("tasks_list", [f"--max={max(1, min(int(limit), 100))}"], ["@default"], runner=runner, acct=acct)
    rows = [{"id": _first(t, "id"), "title": _first(t, "title"), "due": str(_first(t, "due"))[:10],
             "notes": _first(t, "notes")}
            for t in _items(d, "tasks") if str(t.get("status", "needsAction")) != "completed"]
    return sorted(rows, key=lambda r: (r["due"] or "9999", r["title"]))


def tasks_add(title, due="", notes="", settings=None, runner=None):
    acct = _need_ready(settings)
    title = str(title or "").strip()
    if not title:
        raise GoogleError("a task needs a title", "bad")
    flags = [f"--title={title}"]
    if due:
        flags.append(f"--due={_ts(due)[:10]}")
    if notes:
        flags.append(f"--notes={_check_text(notes, 'notes')}")
    d = _call("tasks_add", flags, ["@default"], runner=runner, acct=acct)
    return {"id": str(_find_key(d, "id") or ""), "title": title, "due": due[:10] if due else ""}


def tasks_done(task_id, settings=None, runner=None):
    acct = _need_ready(settings)
    _call("tasks_done", [], ["@default", _check_id(task_id, "task id")], runner=runner, acct=acct)
    return {"id": task_id, "done": True}


def _due_when(work):
    """A Classroom item's due moment as a local datetime (dueTime is UTC), or None when it has no due date."""
    d = work.get("dueDate")
    if not isinstance(d, dict) or not d.get("year"):
        return None
    t = work.get("dueTime") if isinstance(work.get("dueTime"), dict) else {}
    try:
        if t:
            u = dt.datetime(int(d["year"]), int(d["month"]), int(d["day"]), int(t.get("hours", 0)), int(t.get("minutes", 0)),
                            tzinfo=dt.timezone.utc)
            return u.astimezone().replace(tzinfo=None)
        return dt.datetime(int(d["year"]), int(d["month"]), int(d["day"]), 23, 59)
    except (ValueError, KeyError, TypeError):
        return None


def classroom_due(days=7, now=None, settings=None, runner=None, budget_s=45):
    """Classroom work (as a student) due within `days` days and not turned in, soonest first:
    [{"course","title","due"(datetime),"url"}]. Late work from the last 3 days is kept. Raises GoogleError when
    signed out; an account with no classes simply gives []."""
    acct = _need_ready(settings)
    now = now or dt.datetime.now()
    lo, hi = now - dt.timedelta(days=3), now + dt.timedelta(days=int(days))
    stop = time.monotonic() + budget_s
    courses = _items(_call("classroom_courses", ["--state=ACTIVE", "--student=me", "--max=50"], runner=runner,
                         acct=acct), "courses")
    out = []
    for c in courses:
        if time.monotonic() > stop:
            break
        cid = _first(c, "id")
        if not cid:
            continue
        work = _items(_call("classroom_work", ["--state=PUBLISHED", "--order-by=dueDate desc", "--max=30"], [cid],
                            runner=runner, acct=acct), "coursework", "courseWork")
        for w in work:
            when = _due_when(w)
            if when is None or not (lo <= when <= hi) or time.monotonic() > stop:
                continue
            try:
                subs = _items(_call("classroom_mine", ["--user=me", "--max=5"], [cid, _first(w, "id")], runner=runner,
                              acct=acct), "submissions")
            except GoogleError:
                subs = []
            if any(str(s.get("state")) in ("TURNED_IN", "RETURNED") for s in subs):
                continue
            out.append({"course": str(_first(c, "name", default="")), "title": str(_first(w, "title", default="(untitled)")),
                        "due": when, "url": str(_first(w, "alternateLink"))})
    return sorted(out, key=lambda r: r["due"])


# ---- the morning brief line --------------------------------------------------------------------------------------

def _short_day(d, now):
    delta = (d.date() - now.date()).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"{d:%a}"


def brief_line(settings=None, now=None, runner=None):
    """One line for the morning brief: Classroom work due soon and open tasks. '' when signed out, switched off,
    nothing to say, or anything fails (a brief never waits on Google for long and never shows an error)."""
    now = now or dt.datetime.now()
    try:
        if not ready(settings):
            return ""
        parts = []
        try:
            due = classroom_due(7, now=now, settings=settings, runner=runner, budget_s=30)
        except GoogleError:
            due = []
        if due:
            shown = "; ".join(f"{r['title'][:40]} ({_short_day(r['due'], now)})" for r in due[:3])
            parts.append("due soon: " + shown + (f" +{len(due) - 3} more" if len(due) > 3 else ""))
        try:
            tasks = tasks_open(10, settings=settings, runner=runner)
        except GoogleError:
            tasks = []
        if tasks:
            parts.append("tasks: " + "; ".join(t["title"][:40] for t in tasks[:3])
                         + (f" +{len(tasks) - 3} more" if len(tasks) > 3 else ""))
        return ("Google " + " | ".join(parts))[:400] if parts else ""
    except Exception:  # noqa: BLE001
        return ""


def doctor_check(settings=None):
    """(status, detail, fix) for `waggle doctor`."""
    st = status(settings)
    if not st["enabled"]:
        return "ok", st["detail"], ""
    if st["signed_in"]:
        return "ok", st["detail"], ""
    return "warn", "not signed in (see docs/GOOGLE.md)", "Follow docs/GOOGLE.md once; until then Google stays off."


# ---- MCP tools for the chat brain (loaded by waggle/waggle_mcp.py) -----------------------------------------------

def _owner_turn():
    from . import guard
    turn = guard.current_turn()
    return bool(isinstance(turn, dict) and turn.get("owner"))


def _wrap(payload):
    from .security import wrap_untrusted
    return wrap_untrusted("google", json.dumps(payload, ensure_ascii=False, default=str))


def _tool(fn, write=False, wrap=False):
    def run(args):
        try:
            if write and not _owner_turn():
                raise GoogleError("this changes your Google account, so it only works when you ask for it in your own "
                                  "message; nothing was changed", "bad")
            out = fn(args or {})
        except GoogleError as e:
            return json.dumps({"ok": False, "error": str(e)})
        except (TypeError, ValueError) as e:
            return json.dumps({"ok": False, "error": f"bad request: {e}"})
        return _wrap(out) if wrap else json.dumps({"ok": True, **out}, ensure_ascii=False)
    return run


def _tools():
    try:
        from .toolkit import schema
    except Exception:  # noqa: BLE001
        return []
    own = " Use this, not a Drive/Calendar connector, to create things."
    s = lambda *a, **k: schema(*a, **k)  # noqa: E731
    return [
        (s("google_status", "Is Google (Docs, Drive, Calendar, Tasks, Classroom) signed in on this PC, and as whom.",
           {}, []), _tool(lambda a: status(), wrap=True)),
        (s("google_docs_read", "Read a Google Doc as plain text by its document id (the long id in its link). Untrusted data.",
           {"doc_id": {"type": "string"}, "max_chars": {"type": "integer"}}, ["doc_id"]),
         _tool(lambda a: {"text": docs_read(a.get("doc_id"), int(a.get("max_chars") or 20000))}, wrap=True)),
        (s("google_drive_search", "Search the owner's Google Drive by words. Returns id, name, type, modified, link. Untrusted data.",
           {"query": {"type": "string"}, "max": {"type": "integer"}}, ["query"]),
         _tool(lambda a: {"files": drive_search(a.get("query"), int(a.get("max") or 10))}, wrap=True)),
        (s("google_calendar_list", "The owner's Google Calendar events for the next N days (default 7), times in the "
           "owner's own time zone ('when' has the weekday). Untrusted data.",
           {"days": {"type": "integer"}}, []),
         _tool(lambda a: {"events": calendar_list(int(a.get("days") or 7))}, wrap=True)),
        (s("google_tasks_list", "The owner's open Google Tasks (id, title, due). Untrusted data.", {}, []),
         _tool(lambda a: {"tasks": tasks_open()}, wrap=True)),
        (s("google_classroom_due", "Classroom assignments due in the next N days (default 7) that are not turned in yet. "
           "Untrusted data.", {"days": {"type": "integer"}}, []),
         _tool(lambda a: {"due": [dict(r, due=f"{r['due']:%Y-%m-%d %H:%M}") for r in classroom_due(int(a.get("days") or 7))]},
               wrap=True)),
        (s("google_doc_new", "Create a new Google Doc in the owner's Drive, optionally with starting text (plain text or "
           "markdown-ish). Only when the owner asks. Returns its link." + own,
           {"title": {"type": "string"}, "text": {"type": "string"}}, ["title"]),
         _tool(lambda a: docs_new(a.get("title"), a.get("text") or ""), write=True)),
        (s("google_doc_append", "Add text to the end of an existing Google Doc by id. Only when the owner asks. Never deletes.",
           {"doc_id": {"type": "string"}, "text": {"type": "string"}}, ["doc_id", "text"]),
         _tool(lambda a: docs_append(a.get("doc_id"), a.get("text")), write=True)),
        (s("google_event_new", "Add an event to the owner's own Google Calendar. start/end are ISO ('2026-11-24T15:30', or "
           "a date like '2026-11-24' for all day); end defaults to one hour later. No guests are ever invited. Only when "
           "the owner asks." + own,
           {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
            "description": {"type": "string"}, "location": {"type": "string"}}, ["title", "start"]),
         _tool(lambda a: calendar_new(a.get("title"), a.get("start"), a.get("end") or "", a.get("description") or "",
                                      a.get("location") or ""), write=True)),
        (s("google_task_add", "Add a task to the owner's Google Tasks (due is a date, optional). Only when the owner asks.",
           {"title": {"type": "string"}, "due": {"type": "string"}, "notes": {"type": "string"}}, ["title"]),
         _tool(lambda a: tasks_add(a.get("title"), a.get("due") or "", a.get("notes") or ""), write=True)),
        (s("google_task_done", "Mark one Google Task completed by its id (from google_tasks_list). Only when the owner asks.",
           {"task_id": {"type": "string"}}, ["task_id"]),
         _tool(lambda a: tasks_done(a.get("task_id")), write=True)),
    ]


MCP_TOOLS = _tools()
TOOL_NAMES = {spec["name"] for spec, _ in MCP_TOOLS}


# ---- command line ------------------------------------------------------------------------------------------------

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m waggle.google", description="Google through gogcli (see docs/GOOGLE.md).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="signed in? (no network)")
    sub.add_parser("install", help=f"download and verify gog v{GOG_VERSION}")
    d = sub.add_parser("docs-new", help="create a Google Doc")
    d.add_argument("title")
    d.add_argument("--text", default="")
    sub.add_parser("tasks", help="open Google Tasks")
    c = sub.add_parser("classroom-due", help="Classroom work due soon, not turned in")
    c.add_argument("days", nargs="?", type=int, default=7)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            st = status()
            print("Google: " + st["detail"])
        elif a.cmd == "install":
            print(f"installed: {install()}")
        elif a.cmd == "docs-new":
            r = docs_new(a.title, a.text)
            print(f"created: {r['title']}\n{r['url']}")
        elif a.cmd == "tasks":
            rows = tasks_open()
            print("\n".join(f"- {t['title']}" + (f" (due {t['due']})" if t["due"] else "") for t in rows) or "no open tasks")
        elif a.cmd == "classroom-due":
            rows = classroom_due(a.days)
            print("\n".join(f"- {r['course']}: {r['title']} (due {r['due']:%a %b %d %I:%M %p})" for r in rows)
                  or "nothing due")
    except GoogleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
