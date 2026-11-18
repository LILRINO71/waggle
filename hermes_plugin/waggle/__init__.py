"""Hermes plugin that turns Hermes into Waggle.

Every user turn:  notifications -> resume paused builds -> vault memory -> route
  trivial            -> local model (with memory)
  route=sonnet       -> Sonnet's answer is the reply (turn blocked before the local model runs)
  route=opus         -> background Claude Code build starts; you get pinged when it's done
  budget/limit hit   -> local model does it anyway
After every turn: the local model files new facts about you into the vault.
Before every tool call: guardrails (messaging, money, destructive commands need your OK).
"""
import json
import logging
import sys
import threading
import time
from pathlib import Path

# Waggle is stdlib-only and loaded from the repo, so Hermes's locked environment is never modified.
_repo = Path(__file__).with_name("repo_path.txt")
if _repo.exists() and _repo.read_text(encoding="utf-8").strip() not in sys.path:
    sys.path.insert(0, _repo.read_text(encoding="utf-8").strip())

from waggle import jobs, learn, memory, notify, quota, router, security, vault
from waggle.config import Settings

log = logging.getLogger("waggle")
_last_index = 0.0
_index_lock = threading.Lock()
SKIP_ROUTING_PLATFORMS = {"cron", "kanban", "subagent", "delegation"}


def _reindex_soon():
    global _last_index
    if time.time() - _last_index < 120 or not _index_lock.acquire(blocking=False):
        return

    def work():
        global _last_index
        try:
            memory.reindex()
        except Exception as e:
            log.warning("vault reindex failed: %s", e)
        finally:
            _last_index = time.time()
            _index_lock.release()
    threading.Thread(target=work, daemon=True, name="waggle-index").start()


def on_pre_llm_call(session_id=None, user_message="", platform="", **kwargs):
    _reindex_soon()
    extras = []
    news = notify.unseen()
    if news:
        extras.append("Tell the user these updates first:\n" + "\n".join(f"- {n['text']}" for n in news))
    try:
        jobs.resume_paused()
    except Exception as e:
        log.warning("resume_paused failed: %s", e)
    ctx = memory.context_block(user_message or "")
    if platform in SKIP_ROUTING_PLATFORMS or not user_message:
        return {"context": "\n\n".join([ctx] + extras)}

    d = router.decide(user_message, ctx)
    vault.journal(f"route={d['route']} by={d.get('by')} - {user_message[:80]!r}")
    news_text = ("\n\n" + "\n".join(f"(Update: {n['text']})" for n in news)) if news else ""
    if d["route"] == "sonnet" and d.get("reply"):
        learn.learn_async(user_message, d["reply"])
        return {"action": "block", "message": d["reply"] + news_text}
    if d["route"] == "opus" and d.get("build_prompt"):
        title = d.get("title") or "Build"
        job_id = jobs.create(title, d["build_prompt"], user_message)
        jobs.launch(job_id)
        msg = (f"On it. I gave this to Opus as a background build: **{title}**.\n"
               f"I'll ping you when it's done (job `{job_id}`). Ask \"build status\" any time.{news_text}")
        learn.learn_async(user_message, msg)
        return {"action": "block", "message": msg}
    if d.get("looked_big"):
        extras.append("Claude is unavailable right now (" + d.get("reason", "") + "). Do the best you can locally, "
                      "and tell the user a stronger version can run once the Claude limit resets.")
    return {"context": "\n\n".join([ctx] + extras)}


def on_post_llm_call(user_message="", assistant_response="", **kwargs):
    if user_message and assistant_response:
        learn.learn_async(user_message, assistant_response)


def on_pre_tool_call(tool_name="", args=None, **kwargs):
    return security.check_tool_call(tool_name, args or {})


# ---- tools ---------------------------------------------------------------------------------------

def t_vault_search(args, **kw):
    hits = memory.search(args.get("query", ""), k=int(args.get("k", 6)))
    return json.dumps([{"path": h["path"], "score": h["score"], "text": h["text"][:800]} for h in hits])


def t_remember(args, **kw):
    kind = args.get("kind", "fact")
    if kind not in learn.FILES:
        kind = "fact"
    filed = learn.file_facts([{"kind": kind, "text": args.get("text", "")}])
    return json.dumps({"saved": bool(filed), "note": learn.FILES[kind]})


def t_start_build(args, **kw):
    s = Settings.load()
    ok, why = quota.can_escalate(s.builder_model, s)
    if not ok:
        return json.dumps({"started": False, "reason": why})
    job_id = jobs.create(args.get("title", "Build"), args["prompt"], args.get("request", args["prompt"]))
    jobs.launch(job_id)
    return json.dumps({"started": True, "job_id": job_id})


def t_build_status(args, **kw):
    rows = sorted(jobs.load().values(), key=lambda j: j.get("created", 0), reverse=True)[:8]
    return json.dumps([{k: j.get(k) for k in ("id", "title", "status", "workdir", "note")} |
                       {"result": (j.get("result") or "")[:500]} for j in rows])


def t_waggle_status(args, **kw):
    return json.dumps({"quota": quota.status(), "jobs": len(jobs.load()), "vault": Settings.load().vault})


def _schema(name, desc, props, required):
    return {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    (_schema("vault_search", "Search the user's Obsidian vault (their profile, projects, journal, ideas, people) "
             "for notes relevant to a question. Use before answering anything personal.",
             {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]), t_vault_search),
    (_schema("remember", "Save a durable fact about the user (like, dislike, preference, goal, fact, person, "
             "project, idea) to their vault. Use when the user says 'remember' or states a lasting preference.",
             {"kind": {"type": "string", "enum": list(learn.FILES)}, "text": {"type": "string"}},
             ["kind", "text"]), t_remember),
    (_schema("start_build", "Hand a big multi-step build (app, website, game, multi-file code) to Claude Opus as "
             "a background job. Write a detailed prompt: goal, audience, features, tech, definition of done.",
             {"title": {"type": "string"}, "prompt": {"type": "string"}, "request": {"type": "string"}},
             ["title", "prompt"]), t_start_build),
    (_schema("build_status", "List recent background build jobs and their status/results.", {}, []), t_build_status),
    (_schema("waggle_status", "Show Claude Pro quota use, job count and vault location.", {}, []), t_waggle_status),
]


def register(ctx):
    vault.init()
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset="waggle", schema=schema, handler=handler)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_command("waggle", lambda raw: json.dumps(quota.status(), indent=2),
                         description="Waggle quota and status")
