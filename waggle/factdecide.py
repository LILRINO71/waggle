"""How a new fact relates to the facts Waggle already knows: ADD, UPDATE, INVALIDATE or SKIP (after Mem0's update step).

When learn.file_fact has a new fact, the 10 most similar ACTIVE facts are found (embeddings and BM25, fused by rank)
and the new fact is judged against them:

    ADD         nothing known says the same or the opposite: file it
    UPDATE      it replaces a known fact that changed (a new number, "now", "switched to"): file it, close the old one
    INVALIDATE  it says a known fact stopped being true ("no longer plays tennis") and nothing more: close the old
                one, file nothing
    SKIP        it repeats a known fact: file nothing

Rules decide first (word overlap, numbers, change and negation words, embedding closeness). Only a case the rules
cannot settle goes to the LOCAL model, through an Ollama JSON schema so its answer has a fixed shape; Claude is never
asked. Without Ollama, or with an answer that does not validate, an unclear case is ADD: what the code did before.

decide() reads facts and writes nothing; learn.file_fact acts on the decision (and keeps its own older-statement rule
in force: a statement that arrives late never replaces one stated after it)."""
import math
import re

from . import ollama

ADD, UPDATE, INVALIDATE, SKIP = "ADD", "UPDATE", "INVALIDATE", "SKIP"
ACTIONS = (ADD, UPDATE, INVALIDATE, SKIP)
NEIGHBOURS = 10
RRF_K = 60
COS_SKIP = 0.93  # embedding closeness of two statements of the same fact
COS_NEAR = 0.75  # close enough that a rule that finds nothing hands the case to the model
MODEL_TIMEOUT_S = 30

CHANGE = re.compile(r"\b(now|no longer|not anymore|anymore|instead|switched|switching|changed|moved|moving|"
                    r"rescheduled|postponed|pushed|upgraded|replaced|used to|new)\b", re.I)
STRONG_NEGATION = re.compile(r"\b(no longer|not anymore|anymore|stopped|quit|gave up|dropped|ended|finished with)\b",
                             re.I)
WEAK_NEGATION = re.compile(r"\b(not|never|don't|doesn't|do not|does not|isn't|aren't|won't|can't)\b", re.I)
NOISE = {"the", "and", "has", "have", "with", "for", "user", "wants", "likes", "now", "new", "used", "that", "this",
         "are", "was", "his", "her", "from", "into", "than", "then", "also", "very", "really", "just", "only",
         "currently", "going", "still", "again", "will", "been", "being"}
NEG_WORDS = {"not", "never", "dont", "doesnt", "isnt", "arent", "wont", "cant", "longer", "anymore", "stopped",
             "quit", "gave", "dropped", "ended", "finished", "instead", "switched", "switching", "changed", "moved",
             "moving", "rescheduled", "postponed", "pushed", "upgraded", "replaced"}

SCHEMA = {"type": "object",
          "properties": {"action": {"type": "string", "enum": list(ACTIONS)},
                         "target": {"type": ["integer", "null"]},
                         "reason": {"type": "string"}},
          "required": ["action", "target", "reason"]}

SYSTEM = """You keep a list of facts about one person. A NEW fact arrives; the numbered KNOWN facts are the closest
ones already stored. Decide how the new fact relates to them. Reply with JSON {"action", "target", "reason"}:
ADD: it is new information and none of the known facts says the same or the opposite.
UPDATE: it replaces one known fact that has changed (target = that fact's number).
INVALIDATE: it only says one known fact is no longer true (target = that number).
SKIP: it says the same as a known fact (target = that number).
When in doubt, answer ADD."""


# ---- text helpers --------------------------------------------------------------------------------------------

def tokens(text):
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}|\d+", (text or "").lower().replace("'", ""))
            if w not in NOISE]


def words(text):
    return {w for w in tokens(text) if not w.isdigit()}


def numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def subject(text):
    """The words that say WHAT a fact is about: no numbers, no negation or change words."""
    return {w for w in words(text) if w not in NEG_WORDS}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    n = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / n if n else 0.0


def negated(text):
    return bool(STRONG_NEGATION.search(text or "") or WEAK_NEGATION.search(text or ""))


# ---- the closest known facts ---------------------------------------------------------------------------------

def _bm25(query_tokens, docs, k1=1.5, b=0.75):
    """BM25 score of each doc (a token list) for the query tokens."""
    n = len(docs)
    avg = sum(len(d) for d in docs) / n if n else 0
    df = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scores = []
    for d in docs:
        tf = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in set(query_tokens):
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / (avg or 1)))
        scores.append(s)
    return scores


def neighbours(fact, known, embedder=None, k=NEIGHBOURS):
    """The k known facts closest to `fact`, best first: [{'fact': row, 'cos': float|None, 'bm25': float}]. Dense
    (embedder, when it works) and BM25 rankings are fused by reciprocal rank."""
    pool = [r for r in known if r.get("text")]
    if not pool:
        return []
    docs = [tokens(r["text"]) for r in pool]
    bm = _bm25(tokens(fact["text"]), docs)
    cos = [None] * len(pool)
    if embedder is not None:
        try:
            vecs = embedder([fact["text"]] + [r["text"] for r in pool])
            cos = [cosine(vecs[0], v) for v in vecs[1:]]
        except Exception:
            cos = [None] * len(pool)  # no embeddings (Ollama down): BM25 alone ranks them
    score = [0.0] * len(pool)
    by_bm = sorted(range(len(pool)), key=lambda i: bm[i], reverse=True)
    for rank, i in enumerate(by_bm):
        if bm[i] > 0:
            score[i] += 1.0 / (RRF_K + rank + 1)
    if all(c is not None for c in cos):
        for rank, i in enumerate(sorted(range(len(pool)), key=lambda i: cos[i], reverse=True)):
            score[i] += 1.0 / (RRF_K + rank + 1)
    order = sorted(range(len(pool)), key=lambda i: (score[i], bm[i]), reverse=True)
    return [{"fact": pool[i], "cos": cos[i], "bm25": round(bm[i], 3)} for i in order[:k] if score[i] > 0]


# ---- the rules ------------------------------------------------------------------------------------------------

def _same_class(f, a):
    """Dated events are compared with dated events; lasting facts only with facts of their own kind (a like is
    never a duplicate of a dislike), and 'about' facts by key."""
    ev = {"event", "deadline", "reply"}
    if f["kind"] in ev or a["kind"] in ev:
        return f["kind"] in ev and a["kind"] in ev
    if f["kind"] != a["kind"]:
        return False
    return f.get("key") == a.get("key") if f["kind"] == "about" else True


def by_rules(fact, near):
    """(action, target_fact_or_None, reason) when the rules settle it, else None."""
    text, f_sub, f_num = fact["text"], subject(fact["text"]), numbers(fact["text"])
    f_neg = negated(text)
    best_unclear = None
    for n in near:
        a = n["fact"]
        if not _same_class(fact, a):
            continue
        a_sub, a_num = subject(a["text"]), numbers(a["text"])
        sim = jaccard(f_sub, a_sub)
        cos = n["cos"] if n["cos"] is not None else 0.0
        a_neg = negated(a["text"])
        same_date = fact.get("date") == a.get("date")
        # SKIP: the same statement again
        if re.sub(r"\W+", " ", text.lower()).strip() == re.sub(r"\W+", " ", a["text"].lower()).strip():
            return SKIP, a, "the same words as a known fact"
        if f_neg == a_neg and f_num == a_num and same_date and (sim >= 0.75 or cos >= COS_SKIP):
            return SKIP, a, f"says the same as fact #{a['id']}"
        small = f_sub <= a_sub and len(f_sub) >= 3 and f_neg == a_neg and f_num <= a_num and same_date
        if small:
            return SKIP, a, f"already contained in fact #{a['id']}"
        # INVALIDATE: "no longer ..." about something a known fact still asserts
        if STRONG_NEGATION.search(text) and not a_neg and sim >= 0.5:
            return INVALIDATE, a, f"says fact #{a['id']} is no longer true"
        # UPDATE: the same thing with a different number or date, or an explicit change
        if sim >= 0.5 and f_num and a_num and f_num != a_num and fact["kind"] not in ("event", "deadline", "reply"):
            return UPDATE, a, f"same subject as fact #{a['id']} with a changed number"
        if sim >= 0.5 and CHANGE.search(text):
            return UPDATE, a, f"says fact #{a['id']} changed"
        if fact["kind"] in ("event", "deadline", "reply") and sim >= 0.6 and not same_date and CHANGE.search(text):
            return UPDATE, a, f"moves the event of fact #{a['id']}"
        if sim >= 0.3 or cos >= COS_NEAR:
            if best_unclear is None or sim > best_unclear[0]:
                best_unclear = (sim, a)
    return None if best_unclear is None else ("UNCLEAR", best_unclear[1], "close to a known fact, rules cannot tell")


# ---- the local model (unclear cases only) ----------------------------------------------------------------------

def by_model(fact, near, settings=None, chat=None):
    """(action, target_fact_or_None, reason) from the local model, or None when it is unavailable or its answer does
    not validate. `chat(messages, schema=..., settings=...)` is injectable for tests."""
    chat = chat or ollama.chat_json
    ids = {n["fact"]["id"]: n["fact"] for n in near}
    known = "\n".join(f"{i}: {a['text']}" for i, a in ids.items())
    user = f"NEW: {fact['text']}\nKNOWN:\n{known}"
    try:
        out = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], schema=SCHEMA,
                   settings=settings, timeout=MODEL_TIMEOUT_S)
    except Exception:
        return None
    if not isinstance(out, dict) or out.get("action") not in ACTIONS:
        return None
    action, target = out["action"], out.get("target")
    if action == ADD:
        return ADD, None, str(out.get("reason") or "the local model sees new information")[:160]
    if not isinstance(target, int) or isinstance(target, bool) or target not in ids:
        return None  # a decision about a fact that was not offered is no decision
    return action, ids[target], str(out.get("reason") or "local model")[:160]


def decide(fact, known, settings=None, embedder=None, chat=None, k=NEIGHBOURS):
    """The decision for one checked candidate `fact` against the active facts `known`:
    {'action', 'target' (a fact row or None), 'reason', 'via' ('rules'|'model'|'fallback'|'none'),
    'neighbours' (ids)}. Reads only."""
    near = neighbours(fact, known, embedder, k)
    base = {"neighbours": [n["fact"]["id"] for n in near]}
    if not near:
        return {**base, "action": ADD, "target": None, "reason": "no similar known fact", "via": "none"}
    got = by_rules(fact, near)
    if got and got[0] != "UNCLEAR":
        return {**base, "action": got[0], "target": got[1], "reason": got[2], "via": "rules"}
    if got:  # unclear: the local model decides, and ADD is the safe answer when it cannot
        ruled = by_model(fact, near, settings, chat)
        if ruled:
            return {**base, "action": ruled[0], "target": ruled[1], "reason": ruled[2], "via": "model"}
        return {**base, "action": ADD, "target": None, "reason": "unclear and no local model answer: filed as new",
                "via": "fallback"}
    return {**base, "action": ADD, "target": None, "reason": "nothing known is about the same thing", "via": "rules"}
