"""Never "I can't": when Waggle's reply is a refusal for lack of a capability, it builds the capability.

After every turn the final reply is checked. If it says "I can't / I don't have access / I'm unable to..."
about something the user asked it to DO, Waggle:
  1. replaces the refusal with "I can't do that yet, so I'm building the tool now", and
  2. starts a background Opus build that adds the missing capability as a Hermes skill in
     ~/.hermes/skills/waggle/<name>/ (SKILL.md + scripts), first looking for an existing agent-native CLI
     (the cli-hub-meta-skill / CLI-Anything catalogue) and using reverse-engineer-anything when an app has to
     be understood first. When it's done you get a message; send /new so Hermes loads the new skill.

Guard rails: Pro-budget gated like every Opus build, at most `max_selfbuilds_per_day`, the same request is
never rebuilt twice in a week, and refusals for safety reasons (money, passwords, messaging people) are left
alone: those are rules, not missing tools.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import events, quota
from .config import Settings, data_path

REFUSAL = re.compile(
    r"\b(i\s*(?:can(?:'|’)?t|cannot|can not|am unable to|'m unable to|’m unable to|am not able to|'m not able to|"
    r"’m not able to|don(?:'|’)?t have (?:the )?(?:access|ability|permission|a way|a tool|tools?))|"
    r"(?:i\s*)?(?:do not|don(?:'|’)?t) have (?:direct )?access to|not (?:currently )?(?:able|possible) for me|"
    r"(?:is|are) (?:not|n't) (?:something|a thing) i can|beyond my (?:current )?(?:capabilities|abilities))\b", re.I)
RULE_REFUSAL = re.compile(r"\b(password|credit card|card number|payment|pay|purchase|buy|bank|send (?:a )?message|"
                          r"email (?:them|him|her)|text (?:them|him|her)|delete|wipe|illegal|harm|unsafe|"
                          r"your (?:ok|okay|approval|permission))\b", re.I)
ASKS_TO_DO = re.compile(r"\b(open|close|play|pause|turn|set|make|build|create|send|show|read|check|find|get|"
                        r"download|install|run|start|stop|click|type|move|scroll|record|take|look|watch|control|"
                        r"connect|sync|schedule|remind|book|order|edit|change|fix|organi[sz]e|clean|search|what'?s on)\b",
                        re.I)
WEEK = 7 * 86400

BUILD_PROMPT = """Waggle is the user's personal AI agent: Hermes Agent (HERMES_HOME={hermes}) plus the Waggle plugin
(repo {repo}). It just failed a request because it lacks a capability.

User asked: {request}
Waggle replied: {reply}

Give Waggle that capability as a Hermes skill, so next time it simply does it.

1. First look for something that already exists: an agent-native CLI (use the cli-hub-meta-skill skill to search
   the CLI-Anything catalogue), a Hermes built-in tool it should have used (computer_use, browser, terminal,
   web_search, look_at_screen, cron), or a free, well-known tool. Use the reverse-engineer-anything skill only if
   you have to understand how an app works to automate it.
2. Create the skill at {skills}/<short-name>/SKILL.md with Hermes frontmatter (name, description that says WHEN to
   use it, version, platforms: [windows], prerequisites) and a short, exact how-to. Put any helper script next
   to it (Python stdlib or PowerShell preferred). Free tools only, no paid APIs, no API keys to type in.
3. Test it for real on this Windows PC and fix it until it works. If something needs the user (a sign-in, an
   install that needs admin), stop and say exactly what.
4. Never edit Hermes core files, never pip-install into Hermes's own environment, never weaken a security rule.
Finish with: what the skill is called, what it can do now, how you tested it, and anything the user must do."""


def is_capability_refusal(request, reply):
    """True when `reply` refuses an action for lack of a tool (not for a safety rule)."""
    if not reply or not REFUSAL.search(reply[:600]):
        return False
    if RULE_REFUSAL.search(reply[:600]) or RULE_REFUSAL.search(request or ""):
        return False
    return bool(ASKS_TO_DO.search(request or ""))


def _key(request):
    norm = " ".join(re.sub(r"[^a-z0-9 ]", "", (request or "").lower()).split())
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def _state():
    p = data_path("selfbuild.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"builds": []}


def _save(st):
    data_path("selfbuild.json").write_text(json.dumps(st, indent=1), encoding="utf-8")


def handle(request, reply, settings=None, creator=None, now=None):
    """If `reply` is a capability refusal, start a build and return the replacement reply; else None."""
    if not is_capability_refusal(request, reply):
        return None
    s = settings or Settings.load()
    now = now or time.time()
    st = _state()
    key = _key(request)
    recent = [b for b in st["builds"] if now - b["time"] < WEEK]
    if any(b["key"] == key for b in recent):
        events.log("selfbuild", started=False, why="already building or built this week", request=request[:200])
        return ("I can't do that yet. I'm already building the tool for it, and I'll message you when it's "
                "ready.")
    if sum(1 for b in recent if now - b["time"] < 86400) >= s.max_selfbuilds_per_day:
        events.log("selfbuild", started=False, why="daily cap", request=request[:200])
        return None
    ok, why = quota.can_escalate(s.builder_model, s)
    if not ok:
        events.log("selfbuild", started=False, why=why, request=request[:200])
        return f"I can't do that yet, and I'll build the tool for it as soon as there's Claude budget ({why})."
    hermes = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    skills = hermes / "skills" / "waggle"
    skills.mkdir(parents=True, exist_ok=True)
    prompt = BUILD_PROMPT.format(hermes=hermes, repo=Path(__file__).resolve().parent.parent, request=request,
                                 reply=reply[:800], skills=skills)
    if creator is None:
        from . import jobs

        def creator(title, prompt, request, s):
            jid = jobs.create(title, prompt, request, s, project="Waggle")
            jobs.launch(jid)
            return jid
    title = "Skill: " + " ".join(request.split()[:6])
    job_id = creator(title, prompt, request, s)
    st["builds"].append({"time": now, "key": key, "request": request[:300], "job": job_id})
    _save(st)
    events.log("selfbuild", started=True, job=job_id, request=request[:200])
    return ("I can't do that yet, so I'm building the tool for it now in the background. I'll message you when "
            "it's ready; then send /new and ask again.")
