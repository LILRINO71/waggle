"""Waggle Home: the page the owner uses Waggle from, served by the dashboard at http://127.0.0.1:8897/.

  GET  /            this page (the older engine-room page moved to /ops)
  GET  /api/home    one JSON snapshot: what is on Waggle's mind, what it told the owner today, what it is working on,
                    the last night shift, reminders, usage, and "late": what reached the owner's chat after the
                    fact (answers to this page's questions that came after the voice line's 25 s holding line, and
                    background job results); the page shows each one in its chat if it is newer than the page
  GET  /api/history the owner's conversation, oldest first ({"turns": [...], "more"}; ?before=<time>&n=<count>)
  GET  /api/live    what Waggle is doing right now: the turn in progress with each tool step and the words so far
                    (waggle/liveturn.py), and the background jobs that are running with their last steps
  GET  /api/turn    ?id=<turn id>: one finished turn (full reply, model, seconds, tool steps)
  POST /api/hear    audio/*   the words in one recording, for the page's dictation: nothing is sent, no turn runs;
                    the owner reads and corrects the text, then sends it with /api/say
  POST /api/say     {"text"}  one typed turn for the owner's chat through the core's voice line (reply as JSON,
                    with "status" "done" or "working" and "asked_at", this machine's clock when it was sent).
                    "dictated": true marks text that was dictated and then checked by the owner; "speak": true asks
                    for the answer as audio (the response is then the WAV, as for /api/talk)
  POST /api/stop    {}        end the turn that is running now
  POST /api/brain   {"chat_model"} and/or {"job_model"}: which Claude model answers, which does background jobs
                    (the same settings the owner can change by asking in chat, waggle/comfort.py)
  POST /api/talk    audio/*   one spoken turn; the answer comes back as WAV with the spoken words in a header
                    (X-Waggle-Status and X-Waggle-Asked as for /api/say)
  POST /api/night   {"text"}  "accept 1", "redo 2", "reject 3 because ..." for the last night report
  POST /api/taste   {"line"}  one line in the owner's words for the note the thinking pass reads (think.TASTE_NOTE)
  POST /api/think   {}        run one thinking pass now (in the background; it shows up in the snapshot)

Every POST acts as the owner, so three things are checked first (check_post): the Host header is this machine
(a web site whose name points at 127.0.0.1 is refused), an Origin header, when the browser sends one, is this page,
and the request carries the token that only this page was given (a new one each time the process starts). A page on
another site can do none of the three. Turns still go through the core, the engine and the permission hook like any
other owner turn: this page adds no new power, only a nicer way in.
"""
import datetime as dt
import hmac
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Settings

TOKEN = secrets.token_urlsafe(24)
MAX_BODY = 2 * 1024 * 1024
HEAR_TRIES = 8
_thinking = {"running": False, "started": 0.0, "last": None}


# ---- who may post --------------------------------------------------------------------------------------------

def local_host(host, port):
    return (host or "").strip().lower() in (f"127.0.0.1:{port}", f"localhost:{port}")


def check_post(headers, port):
    """'' when the request may act, else why not."""
    if not local_host(headers.get("Host"), port):
        return "not this machine"
    origin = (headers.get("Origin") or "").strip().lower()
    if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return "another page"
    if not hmac.compare_digest(str(headers.get("X-Waggle-Token") or ""), TOKEN):
        return "no page token"
    return ""


# ---- the snapshot --------------------------------------------------------------------------------------------

def _safe(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one broken panel never blanks the page
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"} if default is None else default


def _usage(now):
    from . import quota
    out = {}
    st = quota.status()
    out["five_hour_pct"] = st.get("window_pct")
    seven = quota.seven_day_reading(now)
    if seven:
        out["week_pct"] = round(100 * seven["utilization"])
        out["week_resets"] = seven["resets_at"]
    return out


def _thinking_state(s, now):
    from . import think
    last = think.passes(1)
    nxt = None
    try:
        from waggle.core import timers
        runs = [t["next_run"] for t in (timers.get(f"outreach.think.{i}") for i in range(4)) if t and t.get("enabled")]
        nxt = min(runs) if runs else None
    except Exception:  # noqa: BLE001
        pass
    thoughts = [{"time": t["time"], "topic": t["topic"], "message": t["message"], "draft": t["draft"],
                 "action": t["action"], "wanted": t["model_action"], "why": t["why"], "guard": t["guard"]}
                for t in reversed(think.recent_thoughts(now, days=3))][:12]
    if last:   # what the latest pass thought and did not send (a dry run, or a thought it kept to itself)
        have = {t["topic"] for t in thoughts if abs(t["time"] - last[0]["time"]) < 5}
        extra = [{"time": last[0]["time"], "topic": t.get("topic"), "message": t.get("message"),
                  "draft": t.get("draft", ""), "action": "considered", "wanted": t.get("action"),
                  "why": t.get("why", ""), "guard": "a dry run: nothing was sent" if last[0]["dry"] else ""}
                 for t in json.loads(last[0]["thoughts"] or "[]") if t.get("topic") not in have]
        thoughts = extra + thoughts
    return {"enabled": bool(getattr(s, "think_enabled", True)), "times": [t for _, t in think.times(s)], "next": nxt,
            "running": _thinking["running"],
            "last": ({"time": last[0]["time"], "ok": bool(last[0]["ok"]), "note": last[0]["note"]} if last else None),
            "thoughts": thoughts,
            "questions": [{"time": q["time"], "question": q["question"]} for q in think.recent_questions(now, 7)]}


TEST_ORIGINS = ("canary", "eval", "test", "bench")


def _told(now):
    from . import notify
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = [r for r in notify.rows(since=start) if r["status"] in ("sent", "queued", "sending", "held")
            and (r.get("meta") or {}).get("turn_origin") not in TEST_ORIGINS]  # test turns are not the owner's day
    return [{"time": r["created"], "text": " ".join((r["text"] or "").split())[:260], "origin": r["origin"],
             "status": r["status"], "kind": r["kind"]} for r in rows[-25:]][::-1]


def late_answers(now, window_s=3600, limit=10):
    """What reached the owner's chat after the fact, oldest first, in full, for the page's own chat (which is the
    same conversation as Telegram): answers to this page's questions that came after the holding line (the voice
    line's copies marked late, waggle/core/voiceline.py queue_copy), kind "answer", and background job results
    in the owner's chat (waggle/chat_jobs.py run_background), kind "job". The page shows only what is newer than
    the moment it was opened."""
    from .core import outbox, voiceline
    owner = voiceline.owner_chat()
    out = []
    for r in outbox.list(limit=80):
        meta = r.get("meta") or {}
        if (r.get("created") or 0) < now - window_s or r.get("state") == "failed":
            continue
        if r.get("origin") == "voice" and meta.get("via") == "dashboard" and meta.get("late"):
            kind = "answer"
        elif r.get("origin") == "background" and owner and r.get("chat") == owner:
            kind = "job"
        else:
            continue
        out.append({"id": r["id"], "time": r["created"], "kind": kind, "text": r.get("body") or ""})
    return out[:limit][::-1]


def _night():
    from . import nightshift
    rep = nightshift.latest_report()
    items = []
    for n, tid in enumerate(json.loads((rep or {}).get("items") or "[]"), 1):
        t = nightshift.task(tid) or {}
        items.append({"n": n, "title": t.get("title") or tid, "status": t.get("status"), "pr": t.get("pr_url"),
                      "answer": t.get("answer")})
    queued = len(nightshift.tasks("queued"))
    return {"sent": (rep or {}).get("sent"), "items": items, "queued": queued}


def _work():
    from . import worklist
    rows, bad = worklist.items()
    return {"items": [{k: r.get(k) for k in ("source", "title", "state", "detail")} for r in rows[:14]],
            "unreadable": bad}


def _reminders(now):
    from . import followups
    return [{"due": it.get("due"), "what": str(it.get("what") or "")[:140], "kind": it.get("kind")}
            for it in followups.items(status="open") if (it.get("due") or 0) > now - 3600][:8]


def state(now=None):
    """The snapshot for GET /api/home."""
    from . import dashboard, think
    s = Settings.load()
    now = now or time.time()
    return {"time": now,
            "health": dashboard._cached("health", 10, dashboard.health),
            "usage": dashboard._cached("home_usage", 30, lambda: _usage(now)),
            "thinking": _safe(lambda: _thinking_state(s, now), None),
            "told": _safe(lambda: _told(now), []),
            "work": _safe(_work, {"items": [], "unreadable": ["work list"]}),
            "night": _safe(_night, {"items": [], "queued": 0}),
            "reminders": _safe(lambda: _reminders(now), []),
            "late": _safe(lambda: late_answers(now), []),
            "brain": _safe(lambda: brain(s), {}),
            "taste": _safe(lambda: [l.lstrip("- ").strip() for l in think.taste(s).splitlines()
                                    if l.strip().startswith("-")], [])}


# ---- acting as the owner -------------------------------------------------------------------------------------

def _core(path, body, ctype, timeout=90):
    """POST to the core's voice line. (status, headers dict, body bytes); never raises."""
    from waggle.core import settings as core_settings
    from waggle.core import voiceline
    req = urllib.request.Request(f"http://127.0.0.1:{core_settings.port()}{path}", data=body, method="POST",
                                 headers={"Content-Type": ctype, "Authorization": f"Bearer {voiceline.token()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the local core
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # noqa: BLE001 - the core is off or restarting
        return 503, {}, json.dumps({"error": f"Waggle's core did not answer ({type(e).__name__})"}).encode()


def _client(dictated):
    return "dashmic" if dictated else "dash"


def say(text, post=None, dictated=False):
    """One typed turn. {"ok", "text", "spoken", "status", "turn"} or {"ok": False, "error"}. dictated: the text was
    dictated on the page and checked by the owner before sending."""
    text = " ".join(str(text or "").split())
    if not text:
        return {"ok": False, "error": "nothing to send"}
    asked_at = time.time()
    code, headers, body = (post or _core)(f"/voice?reply=json&client={_client(dictated)}",
                                          json.dumps({"text": text[:4000]}).encode("utf-8"), "application/json")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if code != 200:
        return {"ok": False, "error": data.get("error") or f"the core answered {code}"}
    low = {k.lower(): v for k, v in (headers or {}).items()}
    return {"ok": True, "text": data.get("text") or "", "spoken": data.get("spoken") or "",
            "status": data.get("status") or low.get("x-waggle-status") or "done", "asked_at": asked_at,
            "turn": low.get("x-waggle-turn", "")}


def say_aloud(text, post=None, dictated=True):
    """One turn from text, answered as audio. The same tuple as talk(), plus the turn id."""
    import urllib.parse
    text = " ".join(str(text or "").split())
    if not text:
        return 400, "application/json", json.dumps({"error": "nothing to send"}).encode(), "", "error", 0.0, ""
    asked_at = time.time()
    code, headers, body = (post or _core)(f"/voice?format=wav&client={_client(dictated)}",
                                          json.dumps({"text": text[:4000]}).encode("utf-8"), "application/json")
    low = {k.lower(): v for k, v in (headers or {}).items()}
    spoken = urllib.parse.unquote(low.get("x-waggle-spoken", ""))
    status = low.get("x-waggle-status", "done" if code == 200 else "error")
    return code, low.get("content-type", "application/json"), body, spoken, status, asked_at, low.get("x-waggle-turn", "")


def hear(audio, ctype, post=None, sleep=time.sleep):
    """The words in one recording, for the page's dictation. No turn runs and nothing is sent or stored: the owner
    reads the text, corrects it and sends it themself. {"ok", "text"}; {"ok": False, "error", "busy"} otherwise.
    The known mishearings are fixed and the fillers and false starts taken out, as for a voice note."""
    if not audio or len(audio) > MAX_BODY or not str(ctype or "").lower().startswith("audio/"):
        return {"ok": False, "error": "send up to 2 MB of audio"}
    code, body = 429, b""
    for _ in range(HEAR_TRIES):  # the recogniser takes one recording at a time
        code, _h, body = (post or _core)("/voice/transcribe", audio, ctype.split(";")[0])
        if code != 429:
            break
        sleep(0.35)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if code != 200:
        return {"ok": False, "busy": code == 429, "error": data.get("error") or f"the core answered {code}"}
    text = str(data.get("text") or "").strip()
    if text:
        from . import stt, voice_turn
        try:
            text = stt.clean_transcript(voice_turn.fix(text, getattr(Settings.load(), "voice_fixes", None))).strip()
        except Exception:  # noqa: BLE001 - the raw words are still better than none
            pass
    return {"ok": True, "text": text, "confident": bool(data.get("confident")), "audio_s": data.get("audio_s")}


def stop(post=None):
    """End the turn that is running now. {"ok", "stopped"}."""
    code, _, body = (post or _core)("/voice/stop", b"{}", "application/json")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    return {"ok": code == 200, "stopped": bool(data.get("stopped")), "error": "" if code == 200 else
            (data.get("error") or f"the core answered {code}")}


# ---- the conversation, and what is happening now ---------------------------------------------------------------

def _owner_chat():
    from waggle.core import voiceline
    return voiceline.owner_chat()


def _short_model(model):
    from . import comfort
    return comfort.model_name(model) or str(model or "")


def _turn_row(r, full=True):
    try:
        extra = json.loads(r["extra"] or "{}")
    except (ValueError, TypeError):
        extra = {}
    v = extra.get("voice_turn") or {}
    tools = extra.get("tools") or []
    source = str(v.get("transcript_source") or "")
    said = "typed" if v.get("typed") or source.startswith("typed") else "dictated" if source.startswith("dictated") \
        else "spoken" if extra.get("voice") else "typed"
    reply = r["reply"] or ""
    return {"id": r["id"], "turn": r["turn_id"] or "", "time": r["time"], "request": r["request"] or "",
            "reply": reply if full else reply[:600], "model": _short_model(r["answered_by"] or r["model"]),
            "seconds": r["total_s"], "outcome": r["turn_outcome"] or "", "said": said, "tools": len(tools),
            "where": "here" if "dashboard" in source else "pc voice" if "PC" in source else "phone"}


def history(before=None, n=30, chat=None):
    """The owner's conversation with Waggle (every channel: this page, the PC voice, the phone), oldest first.
    {"turns": [...], "more": True when older turns exist}. Test turns are left out."""
    import sqlite3

    from . import traces
    chat = chat or _owner_chat()
    n = max(1, min(int(n or 30), 100))
    if not chat:
        return {"turns": [], "more": False}
    con = traces._db()
    con.row_factory = sqlite3.Row
    marks = ",".join("?" * len(TEST_ORIGINS))
    q = (f"SELECT id, turn_id, time, request, reply, model, answered_by, total_s, turn_outcome, extra FROM traces "
         f"WHERE chat=? AND reply IS NOT NULL AND reply != '' AND request IS NOT NULL AND request != '' "
         f"AND (origin IS NULL OR origin NOT IN ({marks}))")
    params = [chat, *TEST_ORIGINS]
    if before:
        q += " AND time < ?"
        params.append(float(before))
    rows = con.execute(q + " ORDER BY time DESC, id DESC LIMIT ?", (*params, n + 1)).fetchall()
    con.close()
    return {"turns": [_turn_row(r) for r in reversed(rows[:n])], "more": len(rows) > n}


def turn(turn_id):
    """One finished turn for the page: the full reply, who answered, how long it took and each tool step."""
    from . import traces
    row = traces.by_turn_id(str(turn_id or "")[:64])
    if not row:
        return {"error": "no such turn"}
    out = _turn_row(row)
    try:
        tools = (json.loads(row.get("extra") or "{}").get("tools")) or []
    except (ValueError, TypeError):
        tools = []
    out["steps"] = [{"line": progress_line(t), "ok": t.get("ok"), "denied": bool(t.get("denied"))}
                    for t in tools if isinstance(t, dict)][:60]
    return out


def progress_line(step):
    from . import liveturn
    return liveturn._line(step)


def live(now=None):
    """What Waggle is doing right now: {"turn": the turn in progress or the last one (test turns are not shown),
    "jobs": the background jobs that are running, each with its last steps}."""
    from . import liveturn
    now = now or time.time()
    out = {"time": now, "turn": None, "jobs": []}
    st = _safe(lambda: liveturn.read(now), {})
    if st and st.get("origin") not in TEST_ORIGINS and (st.get("running") or now - (st.get("finished") or 0) < 600):
        st["model"] = _short_model(st.get("model"))
        out["turn"] = st

    def jobs():
        from . import chat_jobs
        rows = []
        for j in chat_jobs.status(_owner_chat() or None, limit=8, now=now):
            if str(j.get("state") or j.get("status") or "") not in ("running", "queued", "leased", "pending"):
                continue
            p = j.get("progress") or {}
            rows.append({"title": str(j.get("task") or j.get("title") or "")[:160], "state": j.get("state") or j.get("status"),
                         "agent": j.get("agent") or "", "model": j.get("model") or "",
                         "minutes": p.get("running_min"), "steps": p.get("steps", 0),
                         "recent": p.get("recent_steps") or [], "files": p.get("files_written") or []})
        return rows
    out["jobs"] = _safe(jobs, [])
    return out


def brain(settings=None):
    """Who answers and who works: {"chat_model", "job_model" ("" = each agent's own), "models"}."""
    from . import comfort
    s = settings or Settings.load()
    return {"chat_model": comfort.current("chat_model", s), "job_model": comfort.current("job_model", s),
            "chat_effort": comfort.current("chat_effort", s), "job_effort": comfort.current("job_effort", s),
            "models": list(comfort.MODELS), "efforts": list(comfort.EFFORTS)}


def set_brain(args):
    """The owner picked a model on the page. The same change, with the same checks and backup, as asking in chat."""
    from . import comfort
    done = {}
    try:
        for key in ("chat_model", "job_model", "chat_effort", "job_effort"):
            if key in (args or {}):
                done[key] = comfort.set_value(key, args[key], turn={"owner": True, "origin": "home", "chat": "home"})
    except comfort.SettingError as e:
        return {"ok": False, "error": str(e), **brain()}
    return {"ok": True, "changed": {k: v["new_words"] for k, v in done.items() if not v.get("unchanged")}, **brain()}


def talk(audio, ctype, post=None):
    """One spoken turn. (status, content type, body, spoken words, turn status, asked_at)."""
    import urllib.parse
    if not audio or len(audio) > MAX_BODY or not str(ctype or "").lower().startswith("audio/"):
        return 400, "application/json", json.dumps({"error": "send up to 2 MB of audio"}).encode(), "", "error", 0.0
    asked_at = time.time()
    code, headers, body = (post or _core)("/voice?format=wav&client=dash", audio, ctype.split(";")[0])
    low = {k.lower(): v for k, v in headers.items()}
    spoken = urllib.parse.unquote(low.get("x-waggle-spoken", ""))
    status = low.get("x-waggle-status", "done" if code == 200 else "error")
    return code, low.get("content-type", "application/json"), body, spoken, status, asked_at


def night(text):
    from . import nightshift
    reply = nightshift.answer(" ".join(str(text or "").split())[:300])
    return {"ok": reply is not None, "text": reply or "That is not an answer to the last night report."}


def teach(line):
    from . import think
    return {"ok": think.add_taste(line)}


def think_now(runner=None):
    """Start one thinking pass in the background (one at a time). The snapshot shows it when it is done."""
    if _thinking["running"]:
        return {"ok": False, "error": "already thinking"}
    _thinking.update(running=True, started=time.time())

    def go():
        try:
            from . import think
            _thinking["last"] = (runner or think.run)(slot="asked")
        except Exception as e:  # noqa: BLE001
            _thinking["last"] = {"ok": False, "note": f"{type(e).__name__}: {e}"[:200]}
        finally:
            _thinking["running"] = False
    threading.Thread(target=go, daemon=True, name="waggle-think-now").start()
    return {"ok": True}


def page():
    return page_template().replace("__TOKEN__", TOKEN)


# ---- the memory graph (the vault as Obsidian draws it) -------------------------------------------------------

LINK = re.compile(r"\[\[([^\]|#^]+)")
GRAPH_MAX_NODES = 900
_graph_cache = {"at": 0.0, "data": None}


def graph(settings=None, now=None, ttl=300):
    """{"nodes": [{id, name, group, degree}], "links": [[i, j]], "groups": [...]} for the whole vault: one node per
    note, one link per [[wikilink]] that resolves to another note. Groups are the top folders. Cached `ttl` s."""
    now = now or time.time()
    if _graph_cache["data"] is not None and now - _graph_cache["at"] < ttl:
        return _graph_cache["data"]
    from . import vault
    s = settings or Settings.load()
    base = vault.root(s).resolve()
    notes = sorted(vault.notes(s))[:4000]
    rel = {}
    for p in notes:
        try:
            rel[p] = p.resolve().relative_to(base).as_posix()
        except ValueError:
            continue
    by_name = {}
    for p, r in rel.items():
        by_name.setdefault(p.stem.lower(), r)
        by_name.setdefault(r[:-3].lower(), r)
    edges, degree = set(), {}
    for p, r in rel.items():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in LINK.finditer(text):
            target = m.group(1).strip().replace("\\", "/").lower().removesuffix(".md")
            t = by_name.get(target) or by_name.get(target.rsplit("/", 1)[-1])
            if t and t != r:
                e = tuple(sorted((r, t)))
                if e not in edges:
                    edges.add(e)
                    degree[e[0]] = degree.get(e[0], 0) + 1
                    degree[e[1]] = degree.get(e[1], 0) + 1
    keep = sorted(rel.values(), key=lambda r: -degree.get(r, 0))[:GRAPH_MAX_NODES]
    index = {r: i for i, r in enumerate(keep)}
    def group_of(r):   # the top folder; inside PARA containers (01 - Areas/Identity/...) the first two levels
        parts = r.split("/")
        return "/".join(parts[:2]) if len(parts) >= 3 else parts[0] if len(parts) == 2 else "(top)"
    groups = sorted({group_of(r) for r in keep})
    data = {"nodes": [{"id": r, "name": r.rsplit("/", 1)[-1][:-3], "group": groups.index(group_of(r)),
                       "degree": degree.get(r, 0)} for r in keep],
            "links": [[index[a], index[b]] for a, b in edges if a in index and b in index],
            "groups": groups, "notes": len(rel), "time": now}
    _graph_cache.update(at=now, data=data)
    return data


def note(rel_path, settings=None, cap=4000):
    """The text of one vault note for the graph's side panel ({"error"} when the path is outside the vault)."""
    from . import vault
    s = settings or Settings.load()
    base = vault.root(s).resolve()
    try:
        p = (base / str(rel_path or "")).resolve()
        p.relative_to(base)
    except (ValueError, OSError):
        return {"error": "not a note in the vault"}
    if p.suffix != ".md" or not p.is_file():
        return {"error": "not a note in the vault"}
    text = p.read_text(encoding="utf-8", errors="replace")
    return {"path": rel_path, "name": p.stem, "text": text[:cap], "more": len(text) > cap}


PAGE_FILE = Path(__file__).with_name("home_page.html")


def page_template():
    return PAGE_FILE.read_text(encoding="utf-8")
