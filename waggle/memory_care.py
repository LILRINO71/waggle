"""Memory upkeep: is Waggle's memory healthy, and what needs cleaning?

- status():      notes, indexed chunks, facts per file, conversation turns, skills, projects missing state
- duplicates():  near-duplicate fact lines across the profile files (embedding cosine, keyword fallback)
- dedupe():      removes exact/near duplicates from Waggle-written fact files (keeps the first, backs up the file)
Facts (vault notes) and procedures (skills) stay separate: this only touches fact files.
"""
import json
import math
import re
import shutil
import sqlite3
import time

from . import projects, vault
from .config import Settings, data_path

FACT_FILES = ["Me/Learned.md", "Me/Likes.md", "Me/Dislikes.md", "People/People.md", "Ideas/Ideas.md"]


def _fact_lines(rel, settings=None):
    try:
        text = vault.read(rel, settings)
    except Exception:
        return []
    return [ln for ln in text.splitlines() if re.match(r"\s*[-*] ", ln)]


def _norm(line):
    line = re.sub(r"^\s*[-*]\s*(\[[ x]\]\s*)?", "", line)
    line = re.sub(r"\(\d{4}-\d{2}-\d{2}[^)]*\)|\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?:?", "", line)
    return " ".join(re.findall(r"[a-z0-9]+", line.lower()))


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def duplicates(settings=None, embedder=None, threshold=0.93):
    """[(file, line_a, line_b, score)] near-duplicate fact pairs within each fact file."""
    out = []
    for rel in FACT_FILES:
        lines = _fact_lines(rel, settings)
        if len(lines) < 2:
            continue
        norms = [_norm(l) for l in lines]
        vecs = None
        if embedder is not None or len(lines) <= 300:
            try:
                from . import ollama
                vecs = (embedder or (lambda t: ollama.embed(t, "document", settings)))(norms)
            except Exception:
                vecs = None
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                if not norms[i] or not norms[j]:
                    continue
                if norms[i] == norms[j]:
                    score = 1.0
                elif vecs:
                    score = _cos(vecs[i], vecs[j])
                else:
                    a, b = set(norms[i].split()), set(norms[j].split())
                    score = len(a & b) / len(a | b) if a | b else 0
                if score >= threshold:
                    out.append((rel, lines[i].strip(), lines[j].strip(), round(score, 3)))
    return out


def dedupe(settings=None, embedder=None, threshold=0.97):
    """Drop the later copy of near-identical fact lines. Backs each changed file up first. Returns count removed."""
    s = settings or Settings.load()
    removed = 0
    pairs = duplicates(s, embedder, threshold)
    by_file = {}
    for rel, _, later, _ in pairs:
        by_file.setdefault(rel, set()).add(later)
    for rel, drop in by_file.items():
        p = vault.safe_path(rel, s)
        text = p.read_text(encoding="utf-8")
        backup = data_path("backups") / f"{rel.replace('/', '_')}.{int(time.time())}.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, backup)
        keep, seen = [], set()
        for ln in text.splitlines():
            if ln.strip() in drop and ln.strip() not in seen:
                seen.add(ln.strip())
                removed += 1
                continue
            keep.append(ln)
        p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return removed


def status(settings=None):
    s = settings or Settings.load()
    notes = list(vault.notes(s))
    try:
        con = sqlite3.connect(data_path("memory.db"))
        chunks = con.execute("SELECT COUNT(*), COUNT(DISTINCT path) FROM chunks WHERE idx >= 0").fetchone()
        con.close()
    except sqlite3.Error:
        chunks = (0, 0)
    facts = {rel: len(_fact_lines(rel, s)) for rel in FACT_FILES}
    try:
        from . import traces
        turns = traces.stats().get("turns", 0)
    except Exception:
        turns = None
    try:
        from . import skills
        sk = skills.stats()
        skill_info = {"total": sk["total"], "by_tier": sk["by_tier"]}
    except Exception:
        skill_info = {}
    audit = projects.audit(s)
    knowledge = {}
    try:
        from . import ingest, kstore, learn
        g = kstore.growth(1)
        knowledge = {"facts_active": len(learn.facts("active")), "ingest_last_run": ingest.status()["last_run"],
                     "today": g[-1] if g else {}}
    except Exception:
        knowledge = {}
    return {"notes": len(notes), "indexed_chunks": chunks[0], "indexed_notes": chunks[1], "facts": facts,
            "conversation_turns": turns, "skills": skill_info,
            "projects_missing_state": [a for a in audit if a["missing"]],
            "stale_projects": [a["name"] for a in audit if a["stale"]], "knowledge": knowledge}


MCP_TOOLS = [
    ({"name": "memory_status", "description": "Health of Waggle's memory: vault notes and index, facts per file, "
      "conversation history size, skills, projects missing a goal/focus/next actions, stale projects.",
      "parameters": {"type": "object", "properties": {}, "required": []}},
     lambda a: json.dumps(status(), default=str)),
    ({"name": "memory_review", "description": "List near-duplicate facts in the user's profile files (to merge).",
      "parameters": {"type": "object", "properties": {}, "required": []}},
     lambda a: json.dumps(duplicates()[:30])),
    ({"name": "note_read", "description": "Read one note from the user's vault by name or path (e.g. 'BIOBUZZ Robot', "
      "'Me/Profile.md', 'Knowledge/Upcoming'). Use after vault_search to read a whole note.",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps(_memory().read_note(a.get("name", "")))),
    ({"name": "related_to", "description": "Everything connected to a project, person, goal or note: its links, the "
      "notes that link to it, and the knowledge graph's relations and dated decisions.",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps(_memory().related(a.get("name", "")), default=str)),
    ({"name": "memory_core", "description": "The short always-relevant summary of the user: profile, current focus "
      "of each active project, what is coming up, yesterday.",
      "parameters": {"type": "object", "properties": {}, "required": []}},
     lambda a: _memory().core_block()),
]


def _memory():
    from . import memory
    return memory
