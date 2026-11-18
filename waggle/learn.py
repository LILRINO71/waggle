"""Learning about the user.

After each turn the local model (free) pulls out durable facts: likes, dislikes, preferences,
goals, people, projects. New facts are filed into the vault unless a near-duplicate is already
there. A weekly Sonnet pass (`waggle consolidate`) cleans the profile up."""
import datetime as dt
import json
import math
import queue
import threading

from . import claude_cli, ollama, quota, vault
from .config import Settings, data_path

FILES = {"like": "Me/Likes.md", "dislike": "Me/Dislikes.md", "preference": "Me/Learned.md",
         "goal": "Me/Learned.md", "fact": "Me/Learned.md", "person": "People/People.md",
         "project": "Projects/Projects.md", "idea": "Ideas/Ideas.md"}

EXTRACT_SYSTEM = """Extract durable facts about the USER from this exchange: things worth remembering for months.
Only include facts the user stated or clearly showed (not guesses, not facts about the world, not the assistant).
Return JSON: {"facts": [{"kind": "like|dislike|preference|goal|fact|person|project|idea", "text": "short sentence"}]}
Return {"facts": []} if nothing is worth remembering. Most messages have nothing."""


def extract(user_msg, reply, settings=None, chat=None):
    chat = chat or ollama.chat_json
    try:
        out = chat([{"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": f"USER: {user_msg[:3000]}\n\nASSISTANT: {reply[:1500]}"}],
                   settings=settings)
    except Exception:
        return []
    facts = out.get("facts") if isinstance(out, dict) else None
    clean = []
    for f in facts or []:
        if isinstance(f, dict) and f.get("kind") in FILES and 8 <= len(str(f.get("text", ""))) <= 300:
            clean.append({"kind": f["kind"], "text": str(f["text"]).strip()})
    return clean[:5]


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
    """Queue a turn for learning on a background thread so replies are never slowed down."""
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name="waggle-learn")
        _worker.start()
    _q.put((user_msg, reply))


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
