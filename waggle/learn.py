"""Learning about the user.

After each turn the local model (free) pulls out durable facts: likes, dislikes, preferences,
goals, people, projects. New facts are filed into the vault unless a near-duplicate is already
there. A weekly Sonnet pass (`waggle consolidate`) cleans the profile up."""
import datetime as dt
import json
import math
import queue
import re
import threading

from . import claude_cli, ollama, quota, vault
from .config import Settings, data_path

FILES = {"like": "Me/Likes.md", "dislike": "Me/Dislikes.md", "preference": "Me/Learned.md",
         "goal": "Me/Learned.md", "fact": "Me/Learned.md", "person": "People/People.md",
         "project": "Projects/Projects.md", "idea": "Ideas/Ideas.md"}

FILES["about"] = "Me/Profile.md"  # personal details live in the profile's "About me" section

# Single-value details: a new one replaces the old line instead of piling up.
ABOUT_KEYS = ["home", "school", "grade", "age", "job", "schedule", "routine", "place", "family", "health", "other"]
SINGLE = {"home", "school", "grade", "age", "job"}

EXTRACT_SYSTEM = """You read ONE message the user typed to their personal assistant and pull out lasting facts
about the USER themself, worth remembering for months. Ignore questions, requests and anything about the world.

kinds:
- about: personal details. Also give "key": home (where they live), school, grade, age, job, schedule (classes,
  practice times), routine, place (places they go: gym, practice, work), family, health, other
- like / dislike: things they enjoy or don't
- preference: how they want answers or work done
- goal, person (someone in their life + who they are), project, idea

Examples:
"what's the weather tomorrow?" -> {"facts": []}
"I'm a junior at Paloma Valley High" -> {"facts": [{"kind":"about","key":"school","text":"Goes to Paloma Valley High"},
  {"kind":"about","key":"grade","text":"Junior (11th grade)"}]}
"robotics practice is Tuesdays and Thursdays 3-6" -> {"facts": [{"kind":"about","key":"schedule",
  "text":"Robotics practice Tuesdays and Thursdays 3-6 pm"}]}
"I hate long answers" -> {"facts": [{"kind":"preference","text":"Wants short answers"}]}

Return JSON {"facts": [...]}. Most messages have nothing: return {"facts": []}."""

# The small model sometimes "learns" search results or forecasts; those are never facts about the user.
WORLD_FACT = re.compile(r"\b(forecast|chance of (rain|snow)|°[FC]|mph|high of|low of|announced|championship will|"
                        r"news|according to|wind speeds?)\b", re.I)


def extract(user_msg, reply=None, settings=None, chat=None):
    """Facts from the user's OWN words only (the assistant reply is ignored on purpose)."""
    chat = chat or ollama.chat_json
    msg = (user_msg or "").strip()
    if len(msg) < 4:
        return []
    try:
        out = chat([{"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": msg[:3000]}], settings=settings)
    except Exception:
        return []
    facts = out.get("facts") if isinstance(out, dict) else None
    clean = []
    for f in facts or []:
        if not (isinstance(f, dict) and f.get("kind") in FILES):
            continue
        text = str(f.get("text", "")).strip()
        if not 6 <= len(text) <= 300 or WORLD_FACT.search(text):
            continue
        item = {"kind": f["kind"], "text": text}
        if f["kind"] == "about":
            item["key"] = f.get("key") if f.get("key") in ABOUT_KEYS else "other"
        clean.append(item)
    return clean[:5]


def set_about(key, text, settings=None):
    """Write one personal detail into Profile -> About me. Single-value keys replace their old line."""
    s = settings or Settings.load()
    text = text.strip().rstrip(".")
    prof = vault.read("Me/Profile.md", s) or "# Profile\n"
    line = f"- **{key.capitalize()}:** {text} ({dt.date.today():%Y-%m-%d})"
    m = re.search(r"## About me\n(.*?)(?=\n## |\Z)", prof, re.S)
    lines = [l for l in (m.group(1).strip().splitlines() if m else []) if l.strip()]
    if key in SINGLE:
        lines = [l for l in lines if not l.startswith(f"- **{key.capitalize()}:**")]
    elif any(text.lower() in l.lower() for l in lines):
        return False
    lines.append(line)
    block = "## About me\n" + "\n".join(lines) + "\n"
    if m:
        prof = prof[:m.start()] + block + "\n" + prof[m.end():].lstrip("\n")
    else:
        prof = prof.replace("## Basics", block + "\n## Basics", 1) if "## Basics" in prof else prof + "\n" + block
    vault.write("Me/Profile.md", prof, s)
    return True


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    n = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / n if n else 0


def file_facts(facts, settings=None, embedder=None, threshold=0.9):
    """Append facts to their notes, skipping near-duplicates of existing bullet lines."""
    s = settings or Settings.load()
    embedder = embedder or (lambda t: ollama.embed(t, "document", s))
    filed = []
    for f in facts:
        if f["kind"] == "about":
            if set_about(f.get("key", "other"), f["text"], s):
                filed.append(f)
            continue
        rel = FILES[f["kind"]]
        existing = [l[2:].split(" (")[0] for l in vault.read(rel, s).splitlines() if l.startswith("- ")]
        if existing:
            vecs = embedder(existing + [f["text"]])
            new = vecs[-1]
            if any(_cos(new, v) >= threshold for v in vecs[:-1]):
                continue
        vault.append(rel, f"- {f['text']} ({dt.date.today():%Y-%m-%d})", s)
        filed.append(f)
    return filed


_q = queue.Queue()
_worker = None


def _loop():
    while True:
        user_msg, reply = _q.get()
        try:
            file_facts(extract(user_msg, reply))
        except Exception:
            pass


def learn_async(user_msg, reply):
    """Retired, no-op. Per-message extraction filed passing remarks, and development test prompts, as
    permanent profile facts with no source. Learning returns with whole conversations and a recorded origin."""
    return None


CONSOLIDATE_SYSTEM = """You maintain a personal profile note for the user of an AI assistant. You get the current
profile and raw learned facts. Rewrite the profile in clean Markdown: merge duplicates, drop stale or contradicted
items (keep the newest), keep sections Basics / How I like answers / Goals / Likes / Dislikes / Current projects.
Be concise. Output only the new Markdown."""


def consolidate(settings=None):
    s = settings or Settings.load()
    ok, why = quota.can_escalate(s.router_model, s)
    if not ok:
        return f"skipped: {why}"
    parts = {rel: vault.read(rel, s) for rel in ["Me/Profile.md", "Me/Likes.md", "Me/Dislikes.md", "Me/Learned.md"]}
    prompt = "\n\n".join(f"=== {k} ===\n{v}" for k, v in parts.items())
    res = claude_cli.ask(prompt, model=s.router_model, system=CONSOLIDATE_SYSTEM, purpose="consolidate")
    stamp = dt.datetime.now()
    vault.write(f"Me/History/Profile-{stamp:%Y%m%d-%H%M}", parts["Me/Profile.md"], s)  # keep the old one
    vault.write("Me/Profile.md", res["text"].strip() + "\n", s)
    data_path("last_consolidate.json").write_text(json.dumps({"time": stamp.isoformat()}), encoding="utf-8")
    return "profile updated"
