"""Traces: what Waggle did on every turn, so it can learn from it (OpenJarvis's trace -> learn -> eval loop).

One row per user turn (request, route, model, agent, latency, tokens, outcome, feedback, correction) plus one
row per tool step (tool, redacted input, ok/failed/denied). Everything is redacted for secrets before storage.
Feedback is captured from the conversation itself: "no, I meant..." marks the previous answer wrong (with the
correction), "perfect, thanks" marks it right, 👍/👎 work too. Full-text search over past turns powers `recall`.
"""
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from .config import data_path
from .policy import redact

CORRECTION = re.compile(
    r"^\s*(no\b(?!,?\s+(thanks?|thank you|problem|worries|need|rush)\b)(?!,?\s+that('?s| is) (fine|great|perfect)\b)|nope\b|wrong\b|that'?s (not|wrong|incorrect)|not (what|quite)|i (meant|said|asked)|you (didn'?t|forgot|missed|"
    r"misunderstood)|that (didn'?t|doesn'?t) work|try again|incorrect|actually,?\s|still (wrong|not|broken)|"
    r"that'?s not right|why did you)", re.I)
PRAISE = re.compile(r"^\s*(thanks?|thank you|perfect|great|awesome|nice|that worked|works( now)?|exactly|"
                    r"good job|love it|👍)\b", re.I)


# The per-turn truth record (turn engine and plugin): who really answered and why, how fast, whether and where it
# arrived, what it claims to have produced, which code version ran it and where the message came from.
TRUTH_COLUMNS = [("turn_id", "TEXT"),         # one id per turn (also the outbox idem key of an undelivered reply)
                 ("chat", "TEXT"),            # the engine's chat key
                 ("origin", "TEXT"),          # user | test | canary | bench | agent (NULL: recorded before origins)
                 ("answered_by", "TEXT"),     # the Claude model id | local | none
                 ("fallback_reason", "TEXT"), # why Claude did not (fully) answer
                 ("ttfw_s", "REAL"),          # message received -> first words shown to the user
                 ("total_s", "REAL"),         # message received -> final text delivered
                 ("delivered", "INTEGER"),    # 1 delivered, 0 not (queued in the outbox or lost), NULL unknown
                 ("delivery", "TEXT"),        # the transport that delivered it, or outbox | none
                 ("delivery_error", "TEXT"),
                 ("message_ids", "TEXT"),     # JSON list of the transport's message ids
                 ("turn_outcome", "TEXT"),    # ok | degraded | failed (turn_outcome(); the old `outcome` is feedback)
                 ("outcome_why", "TEXT"),     # JSON list of the reasons for degraded / failed
                 ("claims", "TEXT"),          # JSON {files, commits, urls, ids} the answer says it produced
                 ("version", "TEXT")]         # code version stamp (code_version())
TEXT_CAP = 20000  # the transcript keeps whole replies (the old 4000-char cut lost long answers)
STEP_RESULT_CAP = 500  # characters of each tool result kept, after redaction
ORIGINS = ("user", "test", "canary", "bench", "agent")
NON_USER = tuple(o for o in ORIGINS if o != "user")
LEARNABLE = "(origin IS NULL OR origin='user')"  # rows that may feed learning, recall and the profile
REPO = Path(__file__).resolve().parent.parent


def _db():
    con = sqlite3.connect(data_path("traces.db"), timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, session TEXT, platform TEXT, request TEXT, route TEXT,
        model TEXT, specialist TEXT, agent TEXT, latency_s REAL, ok INTEGER, error TEXT, reply TEXT,
        tokens TEXT, feedback INTEGER DEFAULT 0, correction TEXT, outcome REAL, skills TEXT, extra TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS steps (
        trace_id INTEGER, idx INTEGER, tool TEXT, input TEXT, ok INTEGER, denied INTEGER, result TEXT)""")
    have = {r[1] for r in con.execute("PRAGMA table_info(traces)")}
    for col, typ in TRUTH_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE traces ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS traces_session ON traces(session, time)")
    con.execute("CREATE INDEX IF NOT EXISTS steps_trace ON steps(trace_id)")
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS traces_fts USING fts5(request, reply, content='traces', "
                    "content_rowid='id')")
    except sqlite3.OperationalError:
        pass  # no FTS5 in this SQLite build: recall falls back to LIKE
    return con


def _has_fts(con):
    return bool(con.execute("SELECT name FROM sqlite_master WHERE name='traces_fts'").fetchone())


def record(request, *, session="", platform="", route="", model="", specialist="", agent="", latency_s=0.0,
           ok=True, error="", reply="", tokens=None, steps=None, skills_used=None, extra=None, turn_id=None,
           chat=None, origin=None, answered_by=None, fallback_reason=None, ttfw_s=None, total_s=None, delivered=None,
           delivery=None, delivery_error=None, message_ids=None, turn_outcome=None, outcome_why=None, claims=None,
           version=None):
    """Store one finished turn; returns its id. The truth fields (turn_id...version) are set by the turn engine
    and the plugin; older callers leave them empty. Every row gets the code version stamp, and the origin from
    WAGGLE_ORIGIN when the caller gives none (benches and test harnesses set it for their whole process)."""
    if origin is None:
        env = (os.environ.get("WAGGLE_ORIGIN") or "").strip().lower()
        origin = env if env in ORIGINS else None
    elif origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, not {origin!r}")
    con = _db()
    cur = con.execute(
        """INSERT INTO traces (time, session, platform, request, route, model, specialist, agent, latency_s, ok,
           error, reply, tokens, skills, extra, turn_id, chat, origin, answered_by, fallback_reason, ttfw_s, total_s,
           delivered, delivery, delivery_error, message_ids, turn_outcome, outcome_why, claims, version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), session or "", platform or "", redact(request or "")[:TEXT_CAP], route or "", model or "",
         specialist or "", agent or "", float(latency_s or 0), 1 if ok else 0, redact(error or "")[:500],
         redact(reply or "")[:TEXT_CAP], json.dumps(tokens or {}), ",".join(skills_used or []),
         json.dumps(extra or {}, default=str), turn_id, chat, origin, answered_by,
         (fallback_reason or None) and redact(fallback_reason)[:500], ttfw_s, total_s,
         None if delivered is None else int(bool(delivered)), delivery,
         (delivery_error or None) and redact(delivery_error)[:500], _json_or_null(message_ids), turn_outcome,
         _json_or_null(outcome_why), _json_or_null(claims), version or code_version()))
    tid = cur.lastrowid
    for i, s in enumerate(steps or []):
        con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?)",
                    (tid, i, s.get("tool", ""), redact(str(s.get("input", "")))[:400],
                     None if s.get("ok") is None else int(bool(s.get("ok"))), int(bool(s.get("denied"))),
                     redact(str(s.get("result", "")))[:STEP_RESULT_CAP]))
    if _has_fts(con):
        con.execute("INSERT INTO traces_fts(rowid, request, reply) VALUES (?,?,?)",
                    (tid, redact(request or "")[:TEXT_CAP], redact(reply or "")[:TEXT_CAP]))
    con.commit()
    con.close()
    return tid


def _json_or_null(value):
    return json.dumps(value, default=str, ensure_ascii=False) if value else None


# ---- the truth helpers: outcome rule, claims, version stamp ------------------------------------------------

_FAMILIES = {"bash": "shell", "powershell": "shell", "webfetch": "web", "websearch": "web", "read": "files",
             "glob": "files", "grep": "files"}


def tool_family(tool):
    """The kind of work a tool does, so a retry with a sibling counts as the same attempt: Bash/PowerShell,
    WebFetch/WebSearch, Read/Glob/Grep, and every tool of one MCP server."""
    name = str(tool or "").lower()
    if name.startswith("mcp__"):
        return "mcp:" + name.split("__")[1]
    return _FAMILIES.get(name, name)


def step_status(step):
    """ok | failed | refused | pending for one recorded tool call. refused: the safety hook turned it down (it
    needs the user's approval, or is blocked); pending: no result ever came back."""
    if step.get("denied"):
        return "refused"
    if step.get("ok") is None:
        return "pending"
    return "ok" if step.get("ok") else "failed"


def _refusal_kind(step):
    text = str(step.get("result") or "")
    if "NEEDS_APPROVAL" in text or not text:
        return "needs approval"
    return "blocked by the safety policy"


def turn_outcome(*, answered_by, delivered, queued=False, steps=(), partial=False, failure_notice=False,
                 degraded_why=(), fast=False):
    """(outcome, reasons) for one turn by a fixed rule, no model involved:
    failed    - no real answer (only an honest failure notice), or the reply was neither delivered nor queued;
    degraded  - answered, but a tool failed or was refused (the safety hook: needs approval, or blocked) and the
                answer went out without it, or the local model answered, or a tool never returned, or the answer
                was cut off, or it waits in the outbox instead of being shown;
    recovered - every tool that failed was followed by a later call of the same kind of work that succeeded
                (Bash then PowerShell, a second WebFetch ...), nothing was refused, and the answer was delivered;
    ok        - no tool failed.
    degraded_why: more reasons that make an answered turn degraded (a voice turn whose spoken reply was not sent).
    fast: the local model answered on purpose (the voice quick-answer tier, waggle/voice_fast.py), not as a fallback.
    A refused call is only recovered by the identical call succeeding later (an approval given in the same turn):
    another command that happens to work is not the action the user has to approve. The reasons list every
    failure, refusal and recovery, in order."""
    why = []
    if failure_notice:
        why.append("no answer: a failure notice was sent instead")
    if not delivered and not queued:
        why.append("reply neither delivered nor queued")
    if why:
        return "failed", why
    bad, healed = [], []
    if answered_by == "local" and not fast:
        bad.append("answered by the local model")
    steps = list(steps or ())
    for i, st in enumerate(steps):
        status, name = step_status(st), st.get("tool") or "?"
        later = steps[i + 1:]
        if status == "refused":
            if any(step_status(x) == "ok" and x.get("tool") == st.get("tool") and x.get("input") == st.get("input")
                   for x in later):
                healed.append(f"{_refusal_kind(st)}, then allowed: {name}")
            else:
                bad.append(f"{_refusal_kind(st)}: {name} (the answer went out without it)")
        elif status == "failed":
            fix = next((x for x in later if step_status(x) == "ok" and tool_family(x.get("tool")) == tool_family(name)),
                       None)
            if fix is not None:
                healed.append(f"tool error: {name}, later {fix.get('tool') or name} succeeded")
            else:
                bad.append(f"tool error: {name}")
        elif status == "pending":
            bad.append(f"tool without a result: {name}")
    if partial:
        bad.append("answer cut off")
    if not delivered:
        bad.append("delivery deferred to the outbox")
    bad.extend(degraded_why or ())  # e.g. the spoken reply could not be sent
    if bad:
        return "degraded", bad + healed
    return ("recovered" if healed else "ok"), healed


_FILE_RE = re.compile(r"(?<![\w/:.\\])((?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|/)?(?:[\w.\-]+[\\/])+[\w.\-]+\.[A-Za-z0-9]{1,8})\b")
_TICKED_FILE_RE = re.compile(r"`([\w.\-]+\.(?:py|md|txt|json|ya?ml|toml|csv|xlsx?|docx?|pptx?|pdf|png|jpe?g|svg|html?|"
                             r"js|ts|tsx|css|java|kt|cs|cpp|h|stl|step|dxf|ogg|mp3|wav|mp4|zip|ps1|bat|sh))`", re.I)
_COMMIT_RE = re.compile(r"\b(?:commit(?:ted)?|sha|hash)\b[^\n]{0,30}?\b([0-9a-f]{7,40})\b", re.I)
_GIT_OUT_RE = re.compile(r"\[[\w./-]+(?: \(root-commit\))? ([0-9a-f]{7,40})\]")
_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+")
_ID_RE = re.compile(r"\b(?:job|id|issue|pr|pull request|message|objective|ticket|order)\s*(?:id)?\s*[:#]?\s*"
                    r"([A-Za-z0-9-]*\d[\w-]{2,})", re.I)
_HASH_ID_RE = re.compile(r"(?<![\w&])#(\d{2,})\b")
_PATH_IN_INPUT = re.compile(r'"(?:file_path|notebook_path|path)":\s*"((?:[^"\\]|\\.)+)"')
_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}


def extract_claims(reply, steps=()):
    """What a reply says it produced, read from its text and its tool calls (no model): files, commits, urls, ids.
    The verifier (plan decision 5) checks these later; here they are only recorded. None when there are none."""
    text = reply or ""
    out = {"files": [], "commits": [], "urls": [], "ids": []}

    def add(key, value):
        value = (value or "").strip().rstrip(".,;:")
        if value and value not in out[key] and len(out[key]) < 20:
            out[key].append(value)

    for m in _URL_RE.finditer(text):
        add("urls", m.group(0))
    no_urls = _URL_RE.sub(" ", text)
    for m in _FILE_RE.finditer(no_urls):
        add("files", m.group(1))
    for m in _TICKED_FILE_RE.finditer(no_urls):
        add("files", m.group(1))
    for m in _COMMIT_RE.finditer(text):
        add("commits", m.group(1))
    for m in _ID_RE.finditer(no_urls):
        if m.group(1) not in out["commits"]:
            add("ids", m.group(1))
    for m in _HASH_ID_RE.finditer(no_urls):
        add("ids", m.group(1))
    for st in steps or ():
        name = str(st.get("tool") or "").lower()
        if name in _WRITE_TOOLS and st.get("ok") is not False:
            m = _PATH_IN_INPUT.search(str(st.get("input") or ""))
            if m:
                add("files", m.group(1).replace("\\\\", "\\"))
        if name in ("bash", "powershell") and st.get("ok"):
            for m in _GIT_OUT_RE.finditer(str(st.get("result") or "")):
                add("commits", m.group(1))
    return {k: v for k, v in out.items() if v} or None


_version = None


def code_version():
    """'waggle <package version>+<git sha>' of the code that is running (cached per process; WAGGLE_VERSION
    overrides it, e.g. for a deployed copy without git)."""
    global _version
    if _version is None:
        ver = os.environ.get("WAGGLE_VERSION", "")
        if not ver:
            pkg = "0"
            try:
                m = re.search(r'^version\s*=\s*"([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M)
                pkg = m.group(1) if m else pkg
            except OSError:
                pass
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                sha = subprocess.run(["git", "rev-parse", "--short=10", "HEAD"], cwd=str(REPO), capture_output=True,
                                     text=True, timeout=5, creationflags=flags).stdout.strip()
            except Exception:
                sha = ""
            ver = f"waggle {pkg}" + (f"+{sha}" if re.fullmatch(r"[0-9a-f]{4,40}", sha) else "")
        _version = ver
    return _version


def by_turn_id(turn_id):
    """The most recent record of one turn id (the core's turn id), or None."""
    if not turn_id:
        return None
    con = _db()
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM traces WHERE turn_id=? ORDER BY id DESC LIMIT 1", (str(turn_id),)).fetchone()
    con.close()
    return dict(row) if row else None


def last(session=None, before=None):
    con = _db()
    con.row_factory = sqlite3.Row
    q, params = "SELECT * FROM traces", []
    conds = []
    if session:
        conds.append("session=?")
        params.append(session)
    if before:
        conds.append("time<?")
        params.append(before)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    row = con.execute(q + " ORDER BY id DESC LIMIT 1", params).fetchone()
    con.close()
    return dict(row) if row else None


def set_feedback(trace_id, value, correction=""):
    con = _db()
    con.execute("UPDATE traces SET feedback=?, correction=?, outcome=? WHERE id=?",
                (int(value), redact(correction)[:1000], 1.0 if value > 0 else 0.0 if value < 0 else None, trace_id))
    con.commit()
    con.close()
    if value:
        row = get(trace_id)
        for name in filter(None, (row or {}).get("skills", "").split(",")):
            try:
                from . import skills
                skills.rate(name, 1.0 if value > 0 else 0.0)
            except Exception:
                pass


def judge_followup(session, message):
    """Called with each new user message: if it corrects or praises Waggle's previous answer, record that.
    Returns (trace_id, value) or None."""
    if not message:
        return None
    if CORRECTION.search(message):
        value = -1
    elif PRAISE.search(message) and len(message) < 80:
        value = 1
    else:
        return None
    prev = last(session)
    if not prev or time.time() - prev["time"] > 30 * 60 or prev.get("feedback"):
        return None
    set_feedback(prev["id"], value, message if value < 0 else "")
    return prev["id"], value


def get(trace_id):
    con = _db()
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM traces WHERE id=?", (trace_id,)).fetchone()
    steps = [dict(r) for r in con.execute("SELECT * FROM steps WHERE trace_id=? ORDER BY idx", (trace_id,))]
    con.close()
    if not row:
        return None
    out = dict(row)
    out["steps"] = steps
    return out


def all_traces(since=0.0, limit=5000, include_non_user=False):
    """Turns since `since`, newest first, with their steps. Turns from tests, canaries, benches and agents are
    left out unless include_non_user: what learns from these rows must only see the user's own words."""
    con = _db()
    con.row_factory = sqlite3.Row
    where = "time>=?" + ("" if include_non_user else f" AND {LEARNABLE}")
    rows = [dict(r) for r in con.execute(f"SELECT * FROM traces WHERE {where} ORDER BY id DESC LIMIT ?",
                                         (since, limit))]
    steps = {}
    for r in con.execute("SELECT * FROM steps WHERE trace_id IN (SELECT id FROM traces WHERE time>=?) ORDER BY idx",
                         (since,)):
        steps.setdefault(r["trace_id"], []).append(dict(r))
    con.close()
    for r in rows:
        r["steps"] = steps.get(r["id"], [])
    return rows


def search(query, k=6):
    """Past turns matching the query (newest first among the best matches)."""
    words = re.findall(r"\w{3,}", query or "")
    if not words:
        return []
    con = _db()
    con.row_factory = sqlite3.Row
    rows = []
    if _has_fts(con):
        try:
            fts_q = " OR ".join(f'"{w}"' for w in words[:12])
            rows = [dict(r) for r in con.execute(
                """SELECT t.id, t.time, t.request, t.reply, t.route FROM traces_fts f JOIN traces t ON t.id=f.rowid
                   WHERE traces_fts MATCH ? AND (t.origin IS NULL OR t.origin='user') ORDER BY rank LIMIT ?""",
                (fts_q, k))]
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        like = " OR ".join(["request LIKE ? OR reply LIKE ?"] * len(words[:6]))
        params = [p for w in words[:6] for p in (f"%{w}%", f"%{w}%")]
        rows = [dict(r) for r in con.execute(f"SELECT id, time, request, reply, route FROM traces WHERE ({like}) AND {LEARNABLE} "
                                             f"ORDER BY id DESC LIMIT ?", (*params, k))]
    con.close()
    return rows


def stats(since=0.0):
    rows = all_traces(since, include_non_user=True)
    if not rows:
        return {"turns": 0}
    by_route, by_model = {}, {}
    for r in rows:
        by_route[r["route"]] = by_route.get(r["route"], 0) + 1
        by_model[r["model"]] = by_model.get(r["model"], 0) + 1
    fb = [r["feedback"] for r in rows if r["feedback"]]
    steps = [s for r in rows for s in r["steps"]]
    lat = sorted(r["latency_s"] for r in rows if r["latency_s"])
    return {"turns": len(rows), "ok_rate": round(sum(r["ok"] for r in rows) / len(rows), 3),
            "by_route": by_route, "by_model": by_model,
            "median_latency_s": lat[len(lat) // 2] if lat else None,
            "feedback": {"good": fb.count(1), "bad": fb.count(-1)},
            "tool_steps": len(steps), "tool_failures": sum(1 for s in steps if s["ok"] == 0 and not s["denied"]),
            "denied": sum(1 for s in steps if s["denied"])}


def session_turns(session, n=8, within_s=12 * 3600):
    """The last n exchanges of one chat session, oldest first: rebuilds the conversation after a gateway restart."""
    if not session:
        return []
    con = _db()
    rows = con.execute("SELECT request, reply FROM traces WHERE session=? AND time>? AND reply IS NOT NULL AND "
                       "reply != '' ORDER BY id DESC LIMIT ?", (session, time.time() - within_s, n)).fetchall()
    con.close()
    return [(r[0] or "", r[1] or "") for r in reversed(rows)]


def chat_transcript(chat, n=8, within_s=3 * 86400, max_chars=12000, origin=None):
    """The last n exchanges of one chat as plain "User: ... / Waggle: ..." text, oldest first, whole replies.
    Seeds a fresh Claude session so the conversation continues after its process or session is replaced.
    origin: only turns from that origin ("user": the owner's own, without canary or test turns)."""
    if not chat:
        return ""
    con = _db()
    q = "SELECT request, reply FROM traces WHERE chat=? AND time>? AND reply IS NOT NULL AND reply != ''"
    params = [chat, time.time() - within_s]
    if origin:
        q += " AND origin=?"
        params.append(origin)
    rows = con.execute(q + " ORDER BY id DESC LIMIT ?", (*params, n)).fetchall()
    con.close()
    text = "\n\n".join(f"User: {r[0]}\nWaggle: {r[1]}" for r in reversed(rows))
    return text[-max_chars:] if len(text) > max_chars else text


def recent_chat(chat, n=12, within_s=6 * 3600, now=None):
    """The last n turns of one chat, newest first, as dicts (request, reply, answered_by, time): to see which of them
    Claude answered and which Waggle answered without it (waggle/voice_fast.py)."""
    if not chat:
        return []
    con = _db()
    rows = con.execute("SELECT request, reply, answered_by, time FROM traces WHERE chat=? AND time>? AND reply IS NOT NULL "
                       "AND reply != '' AND answered_by IS NOT NULL ORDER BY id DESC LIMIT ?",
                       (chat, (time.time() if now is None else now) - within_s, n)).fetchall()
    con.close()
    return [{"request": r[0], "reply": r[1], "answered_by": r[2], "time": r[3]} for r in rows]


def engine_stats(n=50, platform="telegram", since=0.0, origin="user"):
    """The turn engine's scorecard over the last n turns on `platform` (all platforms when empty) from `origin`
    (all when None): share answered by Claude, time to first words p50/p90, outcomes, lost answers (neither
    delivered nor waiting in the outbox: must be 0), fallbacks by reason."""
    con = _db()
    con.row_factory = sqlite3.Row
    q = ("SELECT id, turn_id, answered_by, fallback_reason, ttfw_s, total_s, delivered, delivery, turn_outcome, extra "
         "FROM traces WHERE answered_by IS NOT NULL AND time>=?")
    params = [since]
    if platform:
        q += " AND platform=?"
        params.append(platform)
    if origin:
        q += " AND origin=?"
        params.append(origin)
    rows = [dict(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", (*params, n))]
    con.close()
    if not rows:
        return {"turns": 0, "claude_share": None, "ttfw_p50_s": None, "ttfw_p90_s": None, "lost": 0, "pending": 0,
                "outcomes": {}, "fallbacks": {}}
    from waggle.core import outbox
    lost = pending = 0
    for r in rows:
        if r["delivered"] == 0:
            try:
                oid = json.loads(r["extra"] or "{}").get("outbox")
            except ValueError:
                oid = None
            item = outbox.get(oid) if oid else None
            if item and item["state"] in ("queued", "sending"):
                pending += 1
            elif not item or item["state"] != "sent":
                lost += 1
    firsts = sorted(r["ttfw_s"] for r in rows if r["ttfw_s"] is not None)
    outcomes, falls = {}, {}
    for r in rows:
        outcomes[r["turn_outcome"] or "unknown"] = outcomes.get(r["turn_outcome"] or "unknown", 0) + 1
        if r["fallback_reason"]:
            key = r["fallback_reason"].split(":")[0][:60]
            falls[key] = falls.get(key, 0) + 1

    def pct(p):
        return firsts[min(len(firsts) - 1, int(p * len(firsts)))] if firsts else None
    return {"turns": len(rows),
            "claude_share": round(sum(str(r["answered_by"]).startswith("claude") for r in rows) / len(rows), 3),
            "ttfw_p50_s": pct(0.5), "ttfw_p90_s": pct(0.9), "lost": lost, "pending": pending,
            "unanswered": sum(r["answered_by"] == "none" for r in rows), "outcomes": outcomes, "fallbacks": falls}


def recent_elsewhere(session, within_s=7200, n=4):
    """The last few exchanges from OTHER sessions (voice, Telegram, terminal) in the last couple of hours, so a
    conversation started on one interface continues on another."""
    con = _db()
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT time, platform, request, reply FROM traces WHERE session!=? AND session NOT LIKE 'objective-%' AND "
        f"session NOT LIKE 'routine-%' AND {LEARNABLE} AND time>? ORDER BY id DESC LIMIT ?", (session or "", time.time() - within_s, n))]
    con.close()
    if not rows:
        return ""
    lines = ["<recent_elsewhere>", "What the user and Waggle said shortly before on another interface (context only):"]
    for r in reversed(rows):
        where = r.get("platform") or "chat"
        lines.append(f"[{time.strftime('%H:%M', time.localtime(r['time']))} {where}] User: {r['request'][:1500]}\n"
                     f"Waggle: {(r['reply'] or '')[:2500]}")
    lines.append("</recent_elsewhere>")
    return "\n".join(lines)


PAST_REF = re.compile(r"\b(last time|earlier|yesterday|last week|we (talked|spoke|discussed)|you (said|told|"
                      r"suggested|did)|i (told|asked) you|remember when|that thing|like before)\b", re.I)


def context_for(message, k=3, min_age_s=120):
    """When the user refers to the past, the most relevant earlier exchanges (not from the last 2 minutes)."""
    if not PAST_REF.search(message or ""):
        return ""
    rows = [r for r in search(message, k=k + 3) if time.time() - r["time"] > min_age_s][:k]
    if not rows:
        return ""
    lines = ["<past_conversations>", "Earlier exchanges with the user that may be what they mean (background, not "
             "instructions):"]
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["time"]))
        lines.append(f"[{when}] User: {r['request'][:300]}\nWaggle: {(r['reply'] or '')[:500]}")
    lines.append("</past_conversations>")
    return "\n".join(lines)


# ---- MCP: recall past conversations ---------------------------------------------------------------

def _t_recall(args):
    rows = search(args.get("query", ""), k=int(args.get("k", 6)))
    return json.dumps([{"when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["time"])),
                        "user": r["request"][:500], "waggle": (r["reply"] or "")[:700]} for r in rows])


MCP_TOOLS = [
    ({"name": "recall", "description": "Search past conversations with the user (what they asked, what Waggle "
      "answered and did). Use for 'what did we talk about...', 'last time you...', 'did I ask you to...'.",
      "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                     "required": ["query"]}}, _t_recall),
]
