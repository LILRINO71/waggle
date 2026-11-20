"""Background jobs started from a chat, so heavy work never blocks the conversation.

The chat brain calls the MCP tool `background_task` (served by waggle_mcp.py, which knows its chat from
WAGGLE_CHAT). That queues a `chat.background` job in runtime.db (waggle.core.jobs). The core process runs it:
a Claude tool agent (waggle/agents.py: same tools, policy hook and approvals as any agent) does the task, and
its result goes to the core outbox with the idem key "chat.background:<key>", so it is delivered into the same
chat exactly once, also when the core restarts in the middle: a re-run job whose result is already in the
outbox does not run the agent again, and a second enqueue with the same key changes nothing.

register_core() registers the job kinds, the reap timer, the inbound handler and the pre-warm; the core calls
it at start (detached job runners call it too, so it only registers and never starts work itself).
"""
import json
import os
import uuid

from waggle.core import jobs, outbox, store, timers

JOB_KIND = "chat.background"
REAP_KIND = "chat.reap"
PREWARM_KIND = "chat.prewarm"
REAP_EVERY = "5m"
PREWARM_TIMEOUT_S = 180.0
JOB_LEASE_S = 2400.0  # an agent run may take up to 30 min; the core keeps its own process's jobs on recovery
JOB_MAX_ATTEMPTS = 2  # a crash mid-run (core restart) runs it once more


def _title(task):
    first = " ".join(str(task).split())
    return first[:70] + ("..." if len(first) > 70 else "")


def result_key(payload):
    return f"{JOB_KIND}:{payload['key']}"


def start(chat, task, agent="general", hard=False, origin="", now=None):
    """Queue a background job for `chat`. Returns (job id, key)."""
    key = uuid.uuid4().hex
    payload = {"chat": chat, "task": task, "agent": agent or "general", "hard": bool(hard), "key": key,
               "origin": origin or "", "asked": store.now_or(now)}
    return jobs.enqueue(JOB_KIND, payload, idem_key=f"{JOB_KIND}:{key}", now=now), key


def _queued_result(idem_key):
    row = store.connect().execute("SELECT id FROM outbox WHERE idem_key=?", (idem_key,)).fetchone()
    return row[0] if row else None


def run_background(payload, runner=None):
    """The job handler: do the task, then queue its result for the chat (once). Never raises for a failed
    task: the user is told it didn't finish, and the job's result says so."""
    key = result_key(payload)
    done = _queued_result(key)
    if done is not None:  # a re-run after a restart: the result is already on its way
        return {"ok": None, "outbox": done, "rerun": True}
    try:
        if runner is not None:
            res = runner(payload)
        else:
            from waggle import agents
            res = agents.run(payload.get("agent") or "general", payload["task"], session=payload["chat"],
                             hard=bool(payload.get("hard")), timeout=1800)
        from waggle.agent import approval_message
        ok = bool(getattr(res, "ok", False))
        body = (getattr(res, "text", "") or f"It didn't work: {str(getattr(res, 'error', ''))[:300]}") \
            + (approval_message(res) if getattr(res, "approvals", None) else "")
        err = str(getattr(res, "error", "") or "")
    except Exception as e:  # the agent layer crashed: still an honest message, never silence
        ok, body, err = False, f"It failed: {type(e).__name__}: {e}"[:400], f"{type(e).__name__}: {e}"
    head = "Background job done" if ok else "Background job didn't finish"
    text = f"{head}: {_title(payload['task'])}\n\n{body}"
    oid = outbox.enqueue("text", text, chat=payload["chat"], idem_key=key, origin="background",
                         meta={"kind": "background", "job_key": payload["key"], "ok": ok,
                               "turn_origin": payload.get("origin") or ""})
    return {"ok": ok, "outbox": oid, "error": err[:500], "result": body[:20000]}


def reap(payload=None):
    """Stop chat processes idle longer than settings.chat_idle_s (their sessions resume on the next message).
    The owner's engine chats stay warm longer (chat_session.KEEP_WARM_S)."""
    from waggle import chat_engine, chat_store
    wanted = chat_engine.engine_chats()
    keep = {r["chat"] for r in chat_store.chats_with_address() if r.get("address") in wanted}
    keep |= chat_engine.engine_chat_keys()  # also an engine chat that has no row yet
    return {"stopped": chat_engine.pool().reap(keep=keep)}


def prewarm(payload=None):
    """Start the processes of every engine chat (the owner's, with or without a row yet) and of the chats the user
    wrote in during the last 24 hours."""
    from waggle import chat_engine
    return {"warmed": chat_engine.prewarm()}


def _core_start_key():
    """One idem key per core start (from core.pid, written before plug-ins load), so detached job runners that
    also load this module never queue a second pre-warm. None when no core is running."""
    from waggle.core import settings as core_settings
    try:
        data = json.loads(core_settings.path("core.pid").read_text(encoding="utf-8"))
        return f"{PREWARM_KIND}:{data['pid']}:{data['started']}"
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _register_inbound():
    """Hand inbound messages from the core to the engine. False when this core has no inbound module yet."""
    try:
        from waggle.core import inbound
    except ImportError:
        return False
    from waggle import chat_engine
    inbound.register(chat_engine.on_inbound)
    return True


def register_core():
    """Register with the core: the job kinds (background tasks, reaping, pre-warming), the reap timer, the
    inbound handler, and one pre-warm job per core start. Returns the job kinds."""
    jobs.register(JOB_KIND, run_background, max_attempts=JOB_MAX_ATTEMPTS, lease_s=JOB_LEASE_S)
    jobs.register(REAP_KIND, reap, max_attempts=1)
    jobs.register(PREWARM_KIND, prewarm, max_attempts=1, timeout_s=PREWARM_TIMEOUT_S)
    timers.add(REAP_KIND, "interval", REAP_EVERY, REAP_KIND, catch_up="skip", origin="core")
    _register_inbound()
    key = _core_start_key()
    if key:
        jobs.enqueue(PREWARM_KIND, {}, idem_key=key, priority=5)
    return [JOB_KIND, REAP_KIND, PREWARM_KIND]


def status(chat=None, limit=6):
    """Background jobs (newest first), optionally of one chat."""
    rows = [j for j in jobs.list(kind=JOB_KIND, limit=200)
            if chat is None or (j.get("payload") or {}).get("chat") == chat][:limit]
    out = []
    for j in rows:
        p, r = j.get("payload") or {}, j.get("result") if isinstance(j.get("result"), dict) else {}
        out.append({"job": j["id"], "task": _title(p.get("task", "")), "state": j["state"],
                    "ok": r.get("ok"), "started": j.get("started"), "finished": j.get("finished"),
                    "result": str(r.get("result") or j.get("error") or "")[:600]})
    return out


# ---- MCP tools (served by waggle_mcp.py to the chat brain) -------------------------------------------------

def _t_background_task(args):
    chat = os.environ.get("WAGGLE_CHAT", "")
    task = str(args.get("task") or "").strip()
    if not task:
        return "Give the task: a complete, self-contained description of what to do and what 'done' means."
    if not chat:
        return "background_task only works from a chat; use delegate for a sub-task you wait for."
    jid, _ = start(chat, task, args.get("agent") or "general", bool(args.get("hard")),
                   origin=os.environ.get("WAGGLE_ORIGIN", ""))
    return json.dumps({"started": True, "job": jid, "note": "Queued in the background. Tell the user in one short "
                       "line that you're on it; the result will be posted into this chat when it's done."})


def _t_background_status(args):
    chat = os.environ.get("WAGGLE_CHAT", "")
    return json.dumps(status(chat or None, limit=int(args.get("limit", 6))))


MCP_TOOLS = [
    ({"name": "background_task", "description": "Start a job in the background and return at once: research, "
      "building or editing code, documents, multi-step PC or web work, anything over ~30 seconds. A Claude tool "
      "agent does it (agent: general, coder, researcher, robotics, planner, tutor, monitor; hard=true for difficult "
      "work) and its result is posted into this chat automatically. Write the task completely: the agent does not "
      "see this conversation.",
      "parameters": {"type": "object", "properties": {"task": {"type": "string"}, "agent": {"type": "string"},
                                                      "hard": {"type": "boolean"}}, "required": ["task"]}},
     _t_background_task),
    ({"name": "background_status", "description": "Background jobs started from this chat: status and results.",
      "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}},
     _t_background_status),
]
