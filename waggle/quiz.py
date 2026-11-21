"""The daily knowledge quiz: does Waggle know more about the user than yesterday? (no model calls)

The bank is built from the user's real sources (the Claude Code memory notes), not from the vault, so it measures
what Waggle has taken in: each item is a sentence from a source note with one distinctive answer (a number, a name,
an identifier) blanked out. An item is answered when retrieval for the blanked sentence (the same gated retrieval a
turn uses) brings back text that contains the answer. Generation and scoring are deterministic: the same sources
and the same index give the same score.

score   share of items answered by retrieval (what a turn would actually see)
known   share of items whose answer is anywhere in the indexed vault (what Waggle has, findable or not)"""
import hashlib
import re
import time

from . import kstore, memory, vault
from .config import Settings

MAX_ITEMS = 150
PER_NOTE = 2
ANSWER = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?(?:\s?(?:in|mm|cm|m/s|m|kg|lb|%|°))?|[A-Za-z]+(?:-[A-Za-z0-9]+)+|"
                    r"[A-Z][A-Z0-9]{3,}|[A-Z][a-z]+[A-Z]\w+)(?![\w-])")
DATE = re.compile(r"^\d{4}$|^\d{1,2}$")  # years and bare small numbers say little on their own


def _sentences(text):
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    out = []
    for line in text.splitlines():
        line = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", line).strip(" -*>#\t")
        line = re.sub(r"[*_`]", "", line)
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            sent = sent.strip()
            if 40 <= len(sent) <= 260:
                out.append(sent)
    return out


def _h(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()


def items_from(name, text, common=frozenset()):
    """Up to PER_NOTE cloze items from one note, chosen deterministically."""
    cands = []
    for sent in _sentences(text):
        for m in ANSWER.finditer(sent):
            ans = m.group(1).strip()
            if DATE.match(ans) or ans.lower() in common or len(ans) < 3:
                continue
            q = (sent[:m.start()] + "___" + sent[m.end():]).strip()
            if ans.lower() in q.lower():
                continue  # the answer is still visible elsewhere in the sentence
            cands.append({"id": _h(name, sent, ans)[:12], "note": name, "question": q, "answer": ans})
    cands.sort(key=lambda c: c["id"])
    out, used = [], set()
    for c in cands:
        if c["answer"].lower() in used:
            continue
        used.add(c["answer"].lower())
        out.append(c)
        if len(out) >= PER_NOTE:
            break
    return out


def source_notes(settings=None, dirs=None):
    from . import ingest
    s = settings or Settings.load()
    dirs = ingest.memory_dirs(s) if dirs is None else dirs
    notes = {}
    for d in dirs:
        for p in sorted(d.glob("*.md")):
            if p.stem.upper() in ("MEMORY", "README"):
                continue
            notes[p.stem] = p.read_text(encoding="utf-8", errors="replace")
    return notes


def build_bank(settings=None, dirs=None, now=None):
    """Add items for new source notes, retire items of notes that are gone. Returns the active bank."""
    notes = source_notes(settings, dirs)
    bank = kstore.get("quiz.bank", {})
    # answers that occur in many notes are not distinctive
    freq = {}
    for text in notes.values():
        for a in {m.group(1).lower() for m in ANSWER.finditer(text)}:
            freq[a] = freq.get(a, 0) + 1
    common = frozenset(a for a, n in freq.items() if n > max(3, len(notes) * 0.2))
    have = {it["note"] for it in bank.values() if it.get("active", True)}
    for name, text in notes.items():
        if name in have:
            continue
        for it in items_from(name, text, common):
            bank.setdefault(it["id"], {**it, "added": now or time.time(), "active": True})
    for it in bank.values():
        if it["note"] not in notes:
            it["active"] = False
    kstore.put("quiz.bank", bank)
    return sorted([it for it in bank.values() if it.get("active", True)], key=lambda it: it["id"])[:MAX_ITEMS]


def _vault_text(s):
    r = vault.root(s)
    parts = []
    for p in vault.notes(s):
        rel = p.relative_to(r).as_posix()
        if memory.indexable(rel):
            parts.append(memory.clean(p.read_text(encoding="utf-8", errors="replace")).lower())
    return "\n".join(parts)


def run(settings=None, embedder=None, dirs=None, now=None, bank=None):
    """Score retrieval on the bank. Returns {'items', 'answered', 'score', 'known', 'misses': [...]}."""
    s = settings or Settings.load()
    items = bank if bank is not None else build_bank(s, dirs, now)
    if not items:
        return {"items": 0, "answered": 0, "score": None, "known": None, "misses": []}
    everything = _vault_text(s)
    answered = known = errors = 0
    misses = []
    for it in items:
        ans = it["answer"].lower()
        has = ans in everything
        known += has
        try:
            hits, _ = memory.relevant(it["question"], s, embedder)
        except Exception:
            hits, errors = [], errors + 1
        if any(ans in h["text"].lower() for h in hits):
            answered += 1
        else:
            misses.append({"note": it["note"], "question": it["question"][:160], "answer": it["answer"],
                           "in_vault": has})
    n = len(items)
    if errors == n:  # retrieval itself is down (no embedder): a score of 0 would be a lie, not a baseline
        raise RuntimeError(f"retrieval unavailable: all {n} quiz items failed to search")
    return {"items": n, "answered": answered, "score": round(answered / n, 3), "known": round(known / n, 3),
            "misses": misses[:20]}
