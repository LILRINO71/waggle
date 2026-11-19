"""Background jobs started from a chat, so heavy work never blocks the conversation.

The chat brain calls the MCP tool `background_task` (served by waggle_mcp.py, which knows its chat from
WAGGLE_CHAT). That writes a job row (chat.db) and starts a detached worker process:

    python waggle/chat_jobs.py run <job_id>

The worker runs a Claude tool agent (waggle/agents.py: same tools, policy hook and approvals as any agent),
then writes the result to the chat's outbox. Whichever process owns a transport for that chat (the gateway's
outbox drain) delivers it into the same chat. If nobody has delivered it after `wait_s`, the worker sends it
through notify.send (queued for the next message + phone), so a result is never dropped, even when the
gateway is down or restarted while the job runs.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from waggle import chat_store  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
WAIT_FOR_DELIVERY_S = 180


def _python():
    exe = Path(sys.executable)
    if exe.name.lower().startswith("pythonw"):
        exe = exe.with_name("python.exe")
    return str(exe)


def start(chat, task, agent="general", hard=False, spawn=None):
    """Queue a job for `chat` and start its worker. Returns the job id."""
    jid = chat_store.new_job(chat, task, agent, hard)
    cmd = [_python(), str(REPO / "waggle" / "chat_jobs.py"), "run", jid]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", WAGGLE_SESSION=chat, WAGGLE_CHAT=chat)
    from waggle.config import HOME
    env["WAGGLE_HOME"] = str(HOME)
    if spawn is not None:
        spawn(cmd, env)
        return jid
    flags = 0
    if os.name == "nt":  # survives the chat process (and its MCP server) being stopped
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    log = open(chat_store.data_path(f"job-{jid}.log"), "w", encoding="utf-8") if hasattr(chat_store, "data_path") \
        else subprocess.DEVNULL
    p = subprocess.Popen(cmd, env=env, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)
    chat_store.update_job(jid, pid=p.pid)
    return jid


def _title(task):
    first = " ".join(str(task).split())
    return first[:70] + ("..." if len(first) > 70 else "")


def run(jid, runner=None, wait_s=WAIT_FOR_DELIVERY_S, sleep=time.sleep, phone=None):
    """Do the job, then make sure its result reaches the user. Returns the outbox id."""
    job = chat_store.get_job(jid)
    if not job:
        return None
    chat_store.update_job(jid, status="running", started=time.time(), pid=os.getpid())
    try:
        if runner is not None:
            res = runner(job)
        else:
            from waggle import agents
            res = agents.run(job["agent"] or "general", job["task"], session=job["chat"], hard=bool(job["hard"]),
                             timeout=1800)
        from waggle.agent import approval_message
        ok = bool(getattr(res, "ok", False))
        body = (getattr(res, "text", "") or f"It didn't work: {getattr(res, 'error', '')[:300]}") + approval_message(res)
        err = getattr(res, "error", "")
    except Exception as e:
        ok, body, err = False, f"It failed: {type(e).__name__}: {e}"[:400], str(e)
    text = f"Background job done: {_title(job['task'])}\n\n{body}" if ok else \
        f"Background job didn't finish: {_title(job['task'])}\n\n{body}"
    oid = chat_store.enqueue(job["chat"], text, kind="job", source=jid)
    chat_store.update_job(jid, status="done" if ok else "failed", finished=time.time(), result=body[:20000],
                          error=(err or "")[:500])
    _ensure_delivered(oid, wait_s, sleep, phone)
    return oid


def _ensure_delivered(oid, wait_s, sleep, phone):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        item = chat_store.get_item(oid)
        if not item or item["state"] == "delivered":
            return True
        sleep(3)
    if chat_store.claim(oid):  # still undelivered: the gateway is down or can't reach the chat
        item = chat_store.get_item(oid)
        from waggle import chat_engine
        ok = (phone or chat_engine.deliver_by_phone)(item)
        chat_store.mark(oid, ok, via="notify", error="" if ok else "phone delivery failed")
        return ok
    return False


# ---- MCP tools (served by waggle_mcp.py to the chat brain) -------------------------------------------------

def _t_background_task(args):
    chat = os.environ.get("WAGGLE_CHAT", "")
    task = str(args.get("task") or "").strip()
    if not task:
        return "Give the task: a complete, self-contained description of what to do and what 'done' means."
    if not chat:
        return "background_task only works from a chat; use delegate for a sub-task you wait for."
    jid = start(chat, task, args.get("agent") or "general", bool(args.get("hard")))
    return json.dumps({"started": True, "job": jid, "note": "Running in the background. Tell the user in one short "
                       "line that you're on it; the result will be posted into this chat when it's done."})


def _t_background_status(args):
    chat = os.environ.get("WAGGLE_CHAT", "")
    rows = chat_store.jobs(chat or None, limit=int(args.get("limit", 6)))
    return json.dumps([{"job": r["id"], "task": _title(r["task"]), "status": r["status"],
                        "started": r["started"], "finished": r["finished"],
                        "result": (r["result"] or "")[:600]} for r in rows])


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


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "run":
        run(sys.argv[2])


if __name__ == "__main__":
    main()
