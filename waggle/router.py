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

from . import claude_cli, events, ollama, quota, specialists
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

# The user's own Gmail / Drive / Docs, read through their claude.ai connectors (no local model involved).
CONNECTED_HINT = re.compile(r"\b(e-?mails?|inbox|gmail|unread|mailbox|g-?drive|google (?:drive|docs?)|my (?:docs|drive))\b",
                            re.I)
CONNECTED_SYSTEM = """You are Waggle, the user's personal assistant, with read access to their Gmail, Google Drive
and Google Docs. Use the tools to answer from their real data. Never invent emails or files. You may create a
draft when asked, but you cannot send, delete or share: say the draft is waiting for them. Answer like a quick,
natural spoken reply: lead with what matters (who, what, any deadline), skip promos and newsletters unless asked,
2-4 sentences unless they want the full list."""
CONNECTORS = ["Gmail", "Google_Drive", "Google_Docs"]

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["local", "answer", "connected", "deep", "opus"]},
        "reason": {"type": "string"},
        "reply": {"type": "string", "description": "Your full answer to the user when route=answer, else empty"},
        "build_prompt": {"type": "string",
                         "description": "When route=opus: a complete, detailed prompt for the builder, else empty"},
        "title": {"type": "string", "description": "3-6 word title for a build job, else empty"},
    },
    "required": ["route", "reason", "reply", "build_prompt", "title"],
}

ROUTER_SYSTEM = """You are the brain of Waggle, a personal AI agent. Pick who handles the user's message:
- "answer": you answer it yourself, right now, in "reply". This is the default for conversation, questions,
  advice, explanations, plans, writing and study help. Speak like a quick, natural human conversation partner.
- "connected": the job needs the user's Gmail, Google Drive or Google Docs (reading mail, finding a file or doc,
  drafting a reply). Another call with those connectors answers; leave "reply" empty.
- "local": the job needs the PC's tools (control an app or the desktop, read/write files, run a command, set a
  timer or reminder); a small local model with those tools does it.
- "deep": only when a stronger model clearly matters: hard math or proofs, tricky debugging, detailed engineering
  trade-offs, long careful writing. A bigger model will answer instead of you.
- "opus": a big multi-step build that needs a strong coding agent working for a long time (apps, websites, games,
  multi-file code, research projects). Write "build_prompt": restate the goal, who it's for, must-have features,
  tech choices that suit a student with a Windows PC and $0 budget, what "done" means, and that the result should
  be pushed to the user's GitHub as a portfolio piece if it is a standalone project.
Use the user's notes below to personalise. Prefer the cheapest route that will do the job well.
Write "reply" in Waggle's voice: calm, sharp, dry wit used sparingly, no filler, length matched to the ask,
offering the obvious next step only when it is genuinely useful."""


DEEP_SYSTEM = """You are Waggle, a calm, sharp personal assistant, answering a question that needs careful
reasoning. Use the user's notes to personalise. Be correct first, then concise; no filler."""


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


def adjust(text, cat):
    """Overrule a 'simple' sort when the cheap signals disagree; Sonnet then settles it.
    - a build keyword ("make an app") means it is probably not simple;
    - study, code and robot questions are where the user most needs a right answer and where a 4B model is
      weakest (benchmark: both of its misses were these, sorted 'simple' at 0.95 confidence)."""
    if cat != "simple":
        return cat
    if heuristic(text) == "big" or specialists.pick(text) in ("tutor", "coder", "robotics"):
        return "unsure"
    return cat


def connected_answer(text, memory_ctx="", settings=None, asker=None):
    """Answer from the user's Gmail/Drive/Docs. Returns the reply text, or '' if Claude is unavailable."""
    s = settings or Settings.load()
    asker = asker or claude_cli.ask
    ok, _ = quota.can_escalate(s.router_model, s)
    if not ok:
        return ""
    try:
        res = asker(f"{memory_ctx}\n\nUser message:\n{text}", model=s.router_model, system=CONNECTED_SYSTEM,
                    purpose="connected", effort="low", connectors=CONNECTORS, timeout=120)
    except claude_cli.ClaudeError:
        return ""
    return (res.get("text") or "").strip()


def decide(text, memory_ctx="", settings=None, asker=None, sorter=None, review_allowed=None):
    t0 = time.time()
    d = _decide(text, memory_ctx, settings, asker, sorter, review_allowed)
    d["latency_s"] = round(time.time() - t0, 2)
    events.log("route", route=d["route"], by=d.get("by"), sort=d.get("sort"), reason=d.get("reason", "")[:200],
               latency_s=d["latency_s"], chars=len(text or ""), specialist=d.get("specialist"),
               reviewed=d.get("reviewed", False), answered_by=d.get("answered_by"))
    return d


def _decide(text, memory_ctx="", settings=None, asker=None, sorter=None, review_allowed=None):
    s = settings or Settings.load()
    h = heuristic(text)
    if h == "trivial":
        return {"route": "local", "reason": "trivial request", "reply": "", "build_prompt": "", "title": "",
                "by": "heuristic"}
    if CONNECTED_HINT.search(text or ""):
        reply = connected_answer(text, memory_ctx, s, asker)
        if reply:
            return {"route": "sonnet", "reason": "Gmail / Drive request", "reply": reply, "build_prompt": "",
                    "title": "", "by": "connected", "answered_by": s.router_model, "specialist": "general"}
    if sorter is None and s.router_model == "haiku":
        cat = "unsure"  # Haiku is as fast as the local sort and far better at judging: let it decide
    else:
        cat, conf = (sorter or local_sort)(text, s)
        cat = adjust(text, cat)
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
    spec = specialists.pick(text)
    try:
        res = asker(prompt, model=s.router_model, system=ROUTER_SYSTEM + specialists.guide(spec, s),
                    schema=ROUTE_SCHEMA, purpose="route", effort="low" if s.router_model == "haiku" else "medium")
    except claude_cli.ClaudeError as e:
        out = local_route(text, memory_ctx, s)
        out["reason"] = f"Claude unavailable ({e.kind}): {e}"
        return out
    d = res.get("json") or {}
    if d.get("route") == "sonnet":  # older schema name
        d["route"] = "answer"
    if d.get("route") == "connected":
        d["reply"], d["route"] = connected_answer(text, memory_ctx, s, asker), "answer"
    if d.get("route") not in ("local", "answer", "deep", "opus"):
        return local_route(text, memory_ctx, s)
    if d["route"] == "deep" and s.deep_model != s.router_model:
        ok, why = quota.can_escalate(s.deep_model, s)
        if ok:
            try:
                deep = asker(prompt, model=s.deep_model, system=DEEP_SYSTEM + specialists.guide(spec, s),
                             purpose="deep")
                d["reply"], d["answered_by"] = (deep.get("text") or "").strip(), s.deep_model
            except claude_cli.ClaudeError:
                pass  # keep whatever the router wrote
        d["route"] = "answer"
    if d["route"] == "answer":
        d.setdefault("answered_by", s.router_model)
        d["route"] = "sonnet" if d.get("reply") else "local"  # "sonnet" = a Claude-written reply (legacy name)
    if d["route"] == "opus":
        ok, why = quota.can_escalate(s.builder_model, s)
        if not ok:
            # Sonnet already wrote a great prompt; the local model works from it now.
            d.update(route="local", reason=f"wanted Opus but {why}", looked_big=True)
    d["by"] = s.router_model
    d["sort"] = cat
    d["specialist"] = spec
    if d["route"] == "sonnet" and s.review_enabled:  # off by default: "deep" already covers hard questions
        d["reply"], d["reviewed"] = specialists.review(text, d.get("reply", ""), spec, s, asker=asker,
                                                       allowed=review_allowed)
    return d


def local_answer(text, memory_ctx="", settings=None):
    s = settings or Settings.load()
    return ollama.chat([{"role": "system", "content": "You are Waggle, a friendly personal assistant.\n" + memory_ctx},
                        {"role": "user", "content": text}], settings=s)
