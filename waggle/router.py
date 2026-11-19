"""Who handles a request: the local model, Sonnet, or an Opus background build.

1. Heuristics (instant, free) catch trivial requests: greetings, time, timers, opening apps.
2. The local model sorts the rest: simple / reasoning / build / unsure (about 1 s, free).
   Confident "simple" stays local. Only reasoning, builds and unsure go to Claude.
3. Sonnet then picks the route AND in the same call either answers (route=sonnet) or rewrites the
   request into a detailed build prompt (route=opus).
4. If the Pro budget says no, the local model does the work anyway.
"""
import re
import time

from . import claude_cli, events, ollama, quota
from .config import Settings

TRIVIAL_PATTERNS = [
    r"^(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|cool|nice|good (morning|night))\b[\s!.?]*$",
    r"\bwhat('?s| is) the (time|date|day)\b", r"\bwhat time is it\b",
    r"\b(set|start) (a )?(timer|alarm|reminder)\b", r"\bremind me\b",
    r"^(open|launch|close|start) [\w .-]{1,40}$", r"\b(volume|brightness) (up|down)\b",
    r"\b(pause|play|skip|next) (the )?(music|song|track)\b",
]
BIG_TASK_HINTS = [r"\b(build|make|create|write|code|develop|design)\b.{0,40}\b(app|website|site|game|bot|program|"
                  r"tool|script|api|dashboard|extension|plugin|mod|backend|frontend)\b",
                  r"\brefactor\b", r"\b(fix|debug)\b.{0,30}\b(repo|project|codebase)\b"]

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["local", "sonnet", "opus"]},
        "reason": {"type": "string"},
        "reply": {"type": "string", "description": "Your full answer to the user when route=sonnet, else empty"},
        "build_prompt": {"type": "string",
                         "description": "When route=opus: a complete, detailed prompt for the builder, else empty"},
        "title": {"type": "string", "description": "3-6 word title for a build job, else empty"},
    },
    "required": ["route", "reason", "reply", "build_prompt", "title"],
}

ROUTER_SYSTEM = """You are the routing brain of Waggle, a personal AI agent. Pick who handles the user's message:
- "local": quick tasks a small local 8B model with tools can do well (simple questions, controlling apps or
  files, short summaries, chit-chat, quick lookups).
- "sonnet": needs real reasoning or knowledge but finishes in one reply (explanations, plans, advice, writing,
  study help, reviewing ideas). Write the full reply yourself in "reply".
- "opus": a big multi-step build that needs a strong coding agent working for a long time (apps, websites, games,
  multi-file code, research projects). Write "build_prompt": restate the goal, who it's for, must-have features,
  tech choices that suit a student with a Windows PC and $0 budget, what "done" means, and that the result should
  be pushed to the user's GitHub as a portfolio piece if it is a standalone project.
Use the user's notes below to personalise. Prefer the cheapest route that will do the job well.
Write "reply" in Waggle's voice: calm, sharp, dry wit used sparingly, no filler, length matched to the ask,
offering the obvious next step only when it is genuinely useful."""


def heuristic(text):
    t = (text or "").strip().lower()
    if not t:
        return "trivial"
    if any(re.search(p, t) for p in TRIVIAL_PATTERNS):
        return "trivial"
    if any(re.search(p, t) for p in BIG_TASK_HINTS):
        return "big"
    return "unsure"


def local_route(text, memory_ctx="", settings=None):
    """Fallback when Sonnet is unavailable: the local model handles it (user's choice), but we still
    tell the caller whether it looked big so the reply can say a better result will come later."""
    return {"route": "local", "reason": "Claude budget unavailable; local model handling it",
            "reply": "", "build_prompt": "", "title": "", "looked_big": heuristic(text) == "big",
            "by": "local-fallback"}


SORT_SYSTEM = """Sort the user's message for a personal assistant. Categories:
- simple: chit-chat, quick facts, controlling apps/files, short lookups, reminders, simple edits
- reasoning: needs careful thinking or expertise but one answer (explain, plan, advise, design trade-offs, debug ideas)
- build: a big multi-step creation (an app, website, game, multi-file program, research project)
- unsure: you can't tell
Return JSON: {"category": "...", "confidence": 0.0-1.0}"""


def local_sort(text, settings=None, chat=None):
    """(category, confidence) from the local model; ('unsure', 0) if it fails."""
    chat = chat or ollama.chat_json
    try:
        out = chat([{"role": "system", "content": SORT_SYSTEM}, {"role": "user", "content": text[:2000]}],
                   settings=settings, timeout=60)
        cat = out.get("category")
        conf = float(out.get("confidence", 0))
        if cat in ("simple", "reasoning", "build", "unsure"):
            return cat, max(0.0, min(1.0, conf))
    except Exception:
        pass
    return "unsure", 0.0


def decide(text, memory_ctx="", settings=None, asker=None, sorter=None):
    t0 = time.time()
    d = _decide(text, memory_ctx, settings, asker, sorter)
    d["latency_s"] = round(time.time() - t0, 2)
    events.log("route", route=d["route"], by=d.get("by"), sort=d.get("sort"), reason=d.get("reason", "")[:200],
               latency_s=d["latency_s"], chars=len(text or ""))
    return d


def _decide(text, memory_ctx="", settings=None, asker=None, sorter=None):
    s = settings or Settings.load()
    h = heuristic(text)
    if h == "trivial":
        return {"route": "local", "reason": "trivial request", "reply": "", "build_prompt": "", "title": "",
                "by": "heuristic"}
    cat, conf = (sorter or local_sort)(text, s)
    if h == "big" and cat == "simple":
        cat = "unsure"  # keyword says build, model says simple: let Sonnet settle it
    if cat == "simple" and conf >= 0.7:
        return {"route": "local", "reason": f"local sort: simple ({conf:.2f})", "reply": "", "build_prompt": "",
                "title": "", "by": "local-sort", "sort": cat}
    allowed, why = quota.can_escalate(s.router_model, s)
    if not allowed:
        out = local_route(text, memory_ctx, s)
        out["reason"] = why
        return out
    asker = asker or claude_cli.ask
    prompt = f"{memory_ctx}\n\nUser message:\n{text}"
    try:
        res = asker(prompt, model=s.router_model, system=ROUTER_SYSTEM, schema=ROUTE_SCHEMA, purpose="route")
    except claude_cli.ClaudeError as e:
        out = local_route(text, memory_ctx, s)
        out["reason"] = f"Sonnet unavailable ({e.kind}): {e}"
        return out
    d = res.get("json") or {}
    if d.get("route") not in ("local", "sonnet", "opus"):
        return local_route(text, memory_ctx, s)
    if d["route"] == "opus":
        ok, why = quota.can_escalate(s.builder_model, s)
        if not ok:
            # Sonnet already wrote a great prompt; the local model works from it now.
            d.update(route="local", reason=f"wanted Opus but {why}", looked_big=True)
    d["by"] = "sonnet"
    d["sort"] = cat
    return d


def local_answer(text, memory_ctx="", settings=None):
    s = settings or Settings.load()
    return ollama.chat([{"role": "system", "content": "You are Waggle, a friendly personal assistant.\n" + memory_ctx},
                        {"role": "user", "content": text}], settings=s)
