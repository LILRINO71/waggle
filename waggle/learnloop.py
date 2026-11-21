"""The learning loop (rebuild plan step 7): a miss or a correction leads to one small fix, a re-check, keep or
revert, and a line in the evening note with an undo code. One ledger, every change reversible.

Signals (all free, no model needed to notice them)
  correction   the owner pushing back on Waggle's last answer in chat ("no, my team meets Tue and Thu", "that's
               wrong", "not what I asked", a thumbs-down). Read from the recorded turns, only from the user's own
               (origin user) turns that directly follow one of Waggle's answers in the same chat. Detection is
               deliberately narrow: "no problem", "no thanks", "actually that's great" and a bare "no" to a question
               are not corrections. A hit is written on the answered turn (traces feedback/correction).
  quiz miss    an item of the nightly knowledge quiz that retrieval did not answer (kstore "quiz.last").
  failed turn  a user turn whose outcome was failed, or whose tool call failed (one ledger row per tool and error).

Diagnosis (cheap) and fix
  wrong or stale fact   the owner stated the right value: it is filed through learn.file_fact (the machinery the
                        digest uses), sourced to the correcting turn, and the old fact it contradicts is closed.
  missing fact          a quiz item whose answer is nowhere in the vault: the sentence from the owner's own note is
                        filed as a fact (origin "memory").
  retrieval miss        the answer is in the vault but retrieval does not bring it back: re-index first (free,
                        silent when it helps); otherwise a retrieval hint is recorded and the row waits for code.
  style / tool failure  needs code: the ledger row says "needs_code" and carries the evidence (title, evidence,
                        origin) for the code-proposal pipeline. Nothing here edits code.
The claim to file is taken from the owner's own words. When the words are not enough on their own (pronouns), the
local model rewrites them, and only when it is down a Claude call is tried, at most twice a night, through the
background budget; a refusal leaves the row open for a later night. A claim not found in the owner's words is
refused.

Re-check, keep or revert
  The same retrieval the chat uses (memory.relevant, after a re-index) is asked again: the original question for a
  correction, the quiz item for a miss. The change is kept only if that now finds the new fact AND none of a
  held-out sample of other quiz items that retrieval answered before the change is missed after it; otherwise the
  change is reverted on the spot and the ledger says why. No re-check, no keep.

Report and undo
  evening_lines() gives the evening note one short line per kept or reverted change and per needs-code row, each
  with its code: "Fixed: ... Undo: reply 'undo L7'". "undo L7" in the engine chat reverts that change with no
  model call (chat_engine.approval_preflight routes it like "approve CODE"). A change the owner undid is never
  tried again.

The ledger is the table `improvements` in knowledge.db. A core timer (register_core, a little before the evening
note) runs the loop; `python -m waggle.learnloop run --dry-run` shows what it would do on the real data without
writing anything."""
import hashlib
import json
import re
import sqlite3
import time

from . import events, kstore, learn, vault_git
from .config import Settings

KIND = "learnloop.run"
TIMER = "learnloop.nightly"
LEASE_S = 120.0
TIMEOUT_S = 1200.0
MAX_LATE_S = 20 * 3600
DEFAULT_CRON = "30 20 * * *"  # 30 min before the default evening note: the note can report it
PURPOSE = "learnloop"

LOOKBACK_DAYS = 2
WINDOW_S = 30 * 60  # a correction counts when it follows the answer within this long
MAX_CHANGES = 3  # facts changed per run
MAX_NEW_CODE = 5  # needs-code rows opened per run
MAX_CLAUDE = 2  # Claude calls per run (only when the local model could not word the claim)
HELD_OUT_N = 40  # quiz items kept aside to prove a change did not hurt
QUIZ_MAX_AGE_S = 40 * 3600
DEFER_GIVE_UP_S = 3 * 86400
CODE_LINES = 3  # code requests named in one evening note; the rest are summarised in one line

STATUSES = ("open", "deferred", "kept", "reverted", "needs_code", "noted", "skipped", "undone", "dismissed")
SKIP_ROUTES = {"approval", "command", "sticker", "empty", "attachment"}  # answers that are not an answer to correct

SCHEMA = """CREATE TABLE IF NOT EXISTS improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, updated REAL, source TEXT, signal_key TEXT UNIQUE,
    kind TEXT, status TEXT, trace_id INTEGER, origin TEXT, summary TEXT, evidence TEXT, change TEXT, checks TEXT,
    reason TEXT, reported REAL)"""


# ---- the ledger ----------------------------------------------------------------------------------------------

def _con():
    con = kstore.db()
    con.execute(SCHEMA)
    return con


def _j(value):
    return json.dumps(value, default=str, ensure_ascii=False)


def _row(r):
    if r is None:
        return None
    out = dict(r)
    for k in ("evidence", "change", "checks"):
        try:
            out[k] = json.loads(out[k]) if out.get(k) else None
        except ValueError:
            out[k] = None
    return out


def known_keys():
    """Signal keys already in the ledger (empty when the table does not exist: a dry run never creates it)."""
    con = kstore.db()
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='improvements'").fetchone():
            return set()
        return {r[0] for r in con.execute("SELECT signal_key FROM improvements")}
    finally:
        con.close()


def add(signal_key, source, kind, summary, evidence=None, trace_id=None, status="open", origin="user", now=None):
    """One ledger row per signal. Returns (id, created); a key that is already there returns its id, created False."""
    now = time.time() if now is None else now
    con = _con()
    try:
        try:
            cur = con.execute("INSERT INTO improvements (created, updated, source, signal_key, kind, status, trace_id, "
                              "origin, summary, evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (now, now, source, signal_key, kind, status, trace_id, origin, str(summary)[:300],
                               _j(evidence or {})))
            con.commit()
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            row = con.execute("SELECT id FROM improvements WHERE signal_key=?", (signal_key,)).fetchone()
            return row["id"], False
    finally:
        con.close()


def update(row_id, now=None, **fields):
    """Change fields of a row (json fields given as dicts)."""
    now = time.time() if now is None else now
    cols = {"kind", "status", "summary", "evidence", "change", "checks", "reason", "reported", "trace_id"}
    sets, params = ["updated=?"], [now]
    for k, v in fields.items():
        if k not in cols:
            raise ValueError(f"unknown ledger field {k}")
        if k == "status" and v not in STATUSES:
            raise ValueError(f"unknown ledger status {v}")
        sets.append(f"{k}=?")
        params.append(_j(v) if k in ("evidence", "change", "checks") else v)
    con = _con()
    try:
        con.execute(f"UPDATE improvements SET {', '.join(sets)} WHERE id=?", (*params, row_id))
        con.commit()
    finally:
        con.close()


def get(row_id):
    con = _con()
    try:
        return _row(con.execute("SELECT * FROM improvements WHERE id=?", (int(row_id),)).fetchone())
    finally:
        con.close()


def rows(status=None, since=None, source=None):
    q, params = "SELECT * FROM improvements WHERE 1=1", []
    for col, val in (("status", status), ("source", source)):
        if val:
            vals = [val] if isinstance(val, str) else list(val)
            q += f" AND {col} IN ({','.join('?' * len(vals))})"
            params += vals
    if since:
        q += " AND updated>=?"
        params.append(since)
    con = _con()
    try:
        return [_row(r) for r in con.execute(q + " ORDER BY id", params)]
    finally:
        con.close()


def code_requests():
    """The needs-code rows in the shape the code-proposal pipeline takes: title, evidence, origin (+ the ledger id)."""
    return [{"ledger_id": r["id"], "title": r["summary"], "evidence": r["evidence"], "origin": "learnloop"}
            for r in rows("needs_code")]


# ---- recognising a correction ----------------------------------------------------------------------------------

MAX_PUSHBACK_CHARS = 600
THUMBS_DOWN = re.compile(r"^\W*(👎|:thumbs[_ ]?down:|thumbs?[- ]?down)\W*$", re.I)
# "that's wrong", "you got that wrong": a verdict on the answer, whatever else the message says
WRONG_STRONG = re.compile(
    r"^\W*(?:no\W+)?(?:(?:that|this|it)(?:'s|’s| is| was)|you(?:'re|’re| are| were)|your answer (?:is|was))\s+"
    r"(?:totally |completely |just |so |all |really |still )?"
    r"(?:wrong|incorrect|inaccurate|false|mistaken|not right|not correct|not true|not accurate)\b"
    r"|^\W*(?:no\W+)?you (?:got (?:that|it|this) wrong|made a mistake|mixed (?:that|it) up)\b"
    r"|^\W*(?:wrong|incorrect)\b\s*(?:[.!,]|$)", re.I)
# the owner says what they meant or asked: the answer misread the request (needs a change of behaviour, not a fact)
MISREAD = re.compile(
    r"^\W*(?:no\W+)?(?:that(?:'s|’s| is) )?not what i (?:asked|meant|said|wanted)\b"
    r"|^\W*(?:no\W+)?(?:that(?:'s|’s| is) )?not what i was (?:asking|after)\b"
    r"|^\W*(?:no\W+)?i (?:didn(?:'|’)?t|never) (?:ask|say|mean|tell)\b"
    r"|^\W*(?:no\W+)?(?:you )?misunderstood\b|^\W*(?:no\W+)?(?:you )?misread\b"
    r"|^\W*(?:no\W+)?i (?:meant|asked for|said)\b", re.I)
ACTUALLY = re.compile(r"^\W*actually[\s,:-]+(?:it(?:'s|’s| is)|its|that(?:'s|’s| is)|they(?:'re|’re| are)?|the|my|"
                      r"our|i(?:'m|’m| am)?|we|she|he|there)\b", re.I)
NO_LEAD = re.compile(r"^\W*(?:no|nope|nah)\b[,.:;!\s-]+(?=\S+(?:\s+\S+){2,})", re.I)
# openers that are not corrections even though they start with the same word
NEGATIVE = re.compile(
    r"^\W*(?:no|nope|nah)\W+(?:problem|worries|worry|thanks|thank you|need|rush|hurry|idea|clue|big deal|pressure|"
    r"i don'?t|i do not|i haven'?t|i have not|i can'?t|i won'?t|not now|not yet|not really|that'?s (?:fine|ok|okay|"
    r"good|great|all)|it'?s (?:fine|ok|okay|good|great|all)|go ahead|just|one|more|other|not)\b"
    r"|^\W*actually[\s,:-]+(?:that(?:'s|’s| is)|it(?:'s|’s| is)|they(?:'re|’re| are)?)\s+(?:fine|ok|okay|good|great|"
    r"perfect|cool|nice|helpful|right|better|useful|awesome|amazing|true)\b"
    r"|^\W*actually[\s,:-]+i(?:'m|’m| am)?\s+(?:have a|had a|want|wanna|need|would like|'d like|was wondering|"
    r"think i(?:'ll| will)|will|just)\b"
    r"|^\W*actually[\s,:-]+(?:the question|a question|another|one more|never mind)\b", re.I)
_OPENER = re.compile(
    r"^\W*(?:no|nope|nah|wrong|incorrect|actually|i meant|i said|i asked for|i asked|not what i (?:asked|meant|said)|"
    r"(?:that|this|it)(?:'s|’s| is| was) (?:totally |completely |just |so |all |really |still )?(?:not )?"
    r"(?:wrong|incorrect|inaccurate|false|right|correct|true|accurate)|you(?:'re|’re| are) wrong|"
    r"you got (?:that|it|this) wrong)\b[\s,.:;!-]*", re.I)
_PRONOUN_LEAD = {"it", "it's", "its", "that", "that's", "this", "they", "they're", "he", "she", "there", "those",
                 "these", "then", "so", "but", "and", "or", "which", "who", "what", "when", "where", "how", "why"}
_REQUEST_LEAD = {"can", "could", "would", "will", "please", "try", "do", "does", "make", "show", "tell", "give", "send",
                 "write", "check", "look", "remind", "fix", "use", "just", "let's", "let", "go", "find", "get", "put",
                 "open", "stop", "don't", "dont", "never", "always", "explain", "list", "add", "remove", "delete"}
_STOP = {"the", "and", "has", "have", "with", "for", "that", "this", "are", "was", "were", "you", "your", "not", "but",
         "its", "his", "her", "our", "their", "they", "them", "from", "into", "than", "then", "also", "very"}


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())} - _STOP


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())) if s.strip()]


def _claim_from(text):
    """What the owner stated after the pushback words: up to two declarative sentences, the opener cut off."""
    keep = []
    for sent in _sentences(text):
        rest = sent
        for _ in range(3):
            new = _OPENER.sub("", rest, count=1)
            if new == rest:
                break
            rest = new
        rest = rest.strip(" ,.;:!-")
        if len(rest) >= 3 and not rest.endswith("?") and not WRONG_STRONG.match(rest):
            keep.append(rest)
    return " ".join(keep[:2])[:300]


def standalone(claim):
    """True when a claim makes sense without the question: long enough, not led by a pronoun, not a request."""
    words = (claim or "").strip().lower().split()
    if len(words) < 4 or "?" in claim or re.search(r"\byou(?:r|'re|rs)?\b", claim, re.I):
        return False  # (a claim about "you", the assistant, is an instruction: left to the model or to code)
    return words[0] not in _PRONOUN_LEAD and words[0] not in _REQUEST_LEAD


def pushback(text, prev_reply=""):
    """None, or {"kind": wrong | misread | thumbs_down, "claim": what the owner stated (may be empty), "rule"}.
    Narrow on purpose: it must be the owner disagreeing with the answer just given."""
    t = " ".join((text or "").split())
    if not t or len(t) > MAX_PUSHBACK_CHARS:
        return None
    if THUMBS_DOWN.match(t):
        return {"kind": "thumbs_down", "claim": "", "rule": "thumbs_down"}
    if NEGATIVE.match(t):
        return None
    asked = "?" in (prev_reply or "").strip()[-200:]  # Waggle asked something: a "no" may just be the answer
    strong = WRONG_STRONG.match(t)
    if t.endswith("?") and not strong:
        return None
    if strong:
        kind, rule = "wrong", "wrong"
    elif MISREAD.match(t):
        kind, rule = "misread", "misread"
    elif ACTUALLY.match(t):
        kind, rule = "wrong", "actually"
    elif NO_LEAD.match(t) and not asked:
        kind, rule = "wrong", "no"
    else:
        return None
    return {"kind": kind, "claim": _claim_from(t), "rule": rule}


def _chat_key(r):
    return str(r.get("chat") or r.get("session") or "")


def find_corrections(rows, since=0.0, known=frozenset()):
    """[{"turn", "prev", "verdict"}]: user turns that push back on the answer the user turn before them in the same
    chat got. rows: traces (any order, all origins: a canary or test turn in between breaks the pair)."""
    last, out = {}, []
    for r in sorted(rows, key=lambda x: x["id"]):
        key = _chat_key(r)
        prev, last[key] = last.get(key), r
        if not key or prev is None or r["time"] < since or f"turn:{r['id']}" in known:
            continue
        if not (learn.is_user_turn(r) and learn.is_user_turn(prev)):
            continue
        if r["time"] - prev["time"] > WINDOW_S or not (prev.get("reply") or "").strip():
            continue
        if prev.get("turn_outcome") == "failed" or (prev.get("route") or "") in SKIP_ROUTES:
            continue
        v = pushback(r.get("request"), prev["reply"])
        if v:
            out.append({"turn": r, "prev": prev, "verdict": v})
    return out


# ---- the claim: what to file ---------------------------------------------------------------------------------

WORDING_SYSTEM = (
    "You turn a user's correction of an assistant's answer into one standalone fact. Reply with JSON "
    '{"verdict": "fact" | "style" | "unclear", "fact": "...", "kind": "fact" | "about" | "preference" | "goal" | '
    '"person" | "project"}. "fact": ONLY when the user states what is true (about themselves, their people, projects, '
    "schedule or preferences) and so corrects the answer; write it as one short sentence that makes sense on its own "
    "(replace pronouns using the question) with ONLY information the user gave, never anything new. \"style\": the "
    "user objects to the format, length, tone or what was done and states no new fact. \"unclear\": anything else.")
WORDING_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}, "fact": {"type": "string"},
                                                   "kind": {"type": "string"}}, "required": ["verdict"]}


def grounded(fact_text, *sources, ratio=0.8):
    """True when at least `ratio` of the fact's content words occur in the sources (the owner's own words)."""
    want = _words(fact_text)
    have = set()
    for s in sources:
        have |= _words(s)
    return bool(want) and len(want & have) / len(want) >= ratio


def _prompt(turn, prev):
    return json.dumps({"question": (prev.get("request") or "")[:500], "assistant_answer": (prev.get("reply") or "")[:700],
                       "user_correction": (turn.get("request") or "")[:500]}, ensure_ascii=False)


def _accept(out, turn, prev):
    """(verdict, fact dict or None) from a model's JSON; a fact that is not grounded in the owner's words is dropped."""
    if not isinstance(out, dict):
        return "unclear", None
    verdict = str(out.get("verdict") or "unclear").lower()
    text = " ".join(str(out.get("fact") or "").split()).rstrip(".")
    if verdict != "fact" or not text:
        return ("style" if verdict == "style" else "unclear"), None
    if not grounded(text, turn.get("request"), prev.get("request")):
        return "unclear", None
    kind = str(out.get("kind") or "fact").lower()
    return "fact", {"kind": kind if kind in learn.FILES else "fact", "text": text}


class Claude:
    """The budgeted Claude fallback for wording a claim: a few calls a night, each through the background checks."""

    def __init__(self, settings, asker=None, limit=None):
        self.s, self.asker, self.used = settings, asker, 0
        self.limit = MAX_CLAUDE if limit is None else limit

    def ask(self, turn, prev):
        """(out json or None, why not). Never raises."""
        from . import quota
        if self.used >= self.limit:
            return None, f"Claude limit for this run ({self.limit})"
        try:
            ok, why = quota.can_escalate(self.s.router_model, self.s)
        except Exception as e:  # noqa: BLE001
            return None, f"usage check failed: {type(e).__name__}"
        if not ok:
            return None, why
        self.used += 1
        try:
            if self.asker is None:
                from . import claude_cli
                self.asker = claude_cli.ask
            res = self.asker(_prompt(turn, prev), model=self.s.router_model, system=WORDING_SYSTEM,
                             schema=WORDING_SCHEMA, purpose=PURPOSE, effort="low", timeout=120)
            return res.get("json"), ""
        except Exception as e:  # noqa: BLE001  (a budget deferral is a ClaudeError too)
            return None, f"{type(e).__name__}: {e}"[:200]


def word_claim(turn, prev, verdict, s, *, chat=None, claude=None):
    """(kind, fact or None, why, via): kind is fact | style | unclear | deferred. Order: the owner's own words when
    they stand alone, the local model, then (budget permitting) Claude."""
    claim = verdict.get("claim") or ""
    if verdict["kind"] == "misread":
        return "style", None, "the owner said what they meant: the answer misread the request", "rule"
    if verdict["kind"] == "thumbs_down" or not claim:
        return "unclear", None, "a rating with no correction in it", "rule"
    if standalone(claim):
        return "fact", {"kind": "fact", "text": claim.rstrip(".")}, "", "words"
    why = ""
    try:
        from . import ollama
        out = (chat or ollama.chat_json)([{"role": "system", "content": WORDING_SYSTEM},
                                          {"role": "user", "content": _prompt(turn, prev)}], settings=s)
        got, fact = _accept(out, turn, prev)
        if got != "unclear":
            return got, fact, "" if fact else "the owner objects to the style, not a fact", "local"
        why = "the local model found no standalone fact"
    except Exception as e:  # noqa: BLE001
        why = f"local model unavailable ({type(e).__name__})"
    if claude is None:
        return "unclear", None, why, "none"
    out, cwhy = claude.ask(turn, prev)
    if out is None:
        return "deferred", None, f"{why}; Claude not used: {cwhy}", "none"
    got, fact = _accept(out, turn, prev)
    return got, fact, "" if fact else "Claude found no standalone fact", "claude"


# ---- the held-out sample ------------------------------------------------------------------------------------

class Gate:
    """Re-check helpers on the same retrieval the chat uses, and the held-out quiz sample."""

    def __init__(self, s, embedder=None, n=HELD_OUT_N):
        self.s, self.embedder, self.n = s, embedder, n
        bank = kstore.get("quiz.bank", {}) or {}
        self.bank = sorted((it for it in bank.values() if it.get("active", True)), key=lambda it: it["id"])

    def sample(self, exclude=()):
        pool = [it for it in self.bank if it["id"] not in set(exclude)]
        pool.sort(key=lambda it: hashlib.sha1(it["id"].encode()).hexdigest())
        return pool[:self.n]

    def answered(self, items):
        """Ids of the items retrieval answers now (raises when retrieval is down: nothing can be proved then)."""
        if not items:
            return set()
        from . import quiz
        res = quiz.run(self.s, embedder=self.embedder, bank=items, max_misses=len(items))
        return {it["id"] for it in items} - {m["id"] for m in res["misses"]}

    def reindex(self):
        from . import memory
        return memory.reindex(self.s, self.embedder) if self.embedder else memory.reindex(self.s)

    def hits(self, query):
        """The gated retrieval a turn gets for this message (memory.relevant: what retrieval_block would inject)."""
        from . import memory
        got, _ = memory.relevant(query, self.s, self.embedder)
        return got

    def core(self):
        """The core block every turn gets (memory.safe_core_block, as the engine builds its context)."""
        from . import memory
        return memory.safe_core_block(self.s)


def reindex_quietly(gate):
    try:
        gate.reindex()
        return ""
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:160]


# ---- changing and reverting facts ----------------------------------------------------------------------------

def _best_to_replace(claim_text):
    """The active, non-dated fact the claim most likely contradicts (shares at least two content words, is not a
    near-duplicate), or None."""
    mine, best = learn._words(claim_text), None
    for f in learn.facts("active"):
        if f["kind"] in learn.EVENT_KINDS or f["kind"] == "outcome":
            continue
        theirs = learn._words(f["text"])
        inter = len(mine & theirs)
        if not (mine and theirs) or inter < 2 or inter / len(mine | theirs) >= 0.75:
            continue
        if inter / min(len(mine), len(theirs)) >= 0.5 and (best is None or inter > best[0]):
            best = (inter, f)
    return best[1] if best else None


def _known(claim_text):
    return next((f for f in learn.facts("active") if learn._similar(f["text"], claim_text)), None)


def _refile(f, s):
    """Put a reactivated fact's line back where learn.file_fact had put it."""
    from . import vault
    if f["kind"] in learn.EVENT_KINDS:
        learn.render_upcoming(s)
    elif f["kind"] == "about":
        learn.set_about(f.get("key") or "other", f["text"], s)
    elif learn.FILES.get(f["kind"]) and f["text"] not in vault.read(learn.FILES[f["kind"]], s):
        vault.append(learn.FILES[f["kind"]], f"- {f['text']} ({f.get('valid_from') or ''})".replace(" ()", ""), s)


def apply_fact(fact, s, *, turn=None, source, evidence, origin=None, replace=True):
    """File one fact through learn.file_fact and close the fact it contradicts. Returns (change dict, why not)."""
    f, why = learn.check(fact)
    if not f and ("passing state" in str(why) or "no resolvable date" in str(why))             and not re.search(r"(today|tonight|tomorrow|this|next|now)", fact["text"], re.I):
        # a recurring statement ("meets Tuesday and Thursday") has no date: keep it as a routine, not an event
        fact = {"kind": "about", "key": "routine", "text": fact["text"]}
        f, why = learn.check(fact)
    if not f:
        return None, why
    dup = _known(f["text"])
    if dup:
        return None, f"already a known fact (#{dup['id']})"
    old = _best_to_replace(f["text"]) if replace else None
    ok, got = learn.file_fact(fact, s, source=source, evidence=evidence, turn=turn, origin=origin,
                              supersedes=[old["id"]] if old else ())
    if not ok:
        return None, str(got)
    fid = got
    closed = [r["id"] for r in learn.facts(None) if r.get("replaced_by") == fid]
    return {"kind": "fact", "fact_id": fid, "text": f["text"], "closed": closed,
            "closed_text": [r["text"] for r in learn.facts(None) if r["id"] in closed]}, ""


def revert_change(change, s):
    """Undo one change: take the new fact's line out, reactivate and refile what it replaced. Idempotent."""
    if not change or change.get("kind") != "fact":
        return "nothing to revert"
    con = kstore.db()
    try:
        row = con.execute("SELECT * FROM facts WHERE id=?", (change["fact_id"],)).fetchone()
        mine = dict(row) if row else None
        olds = [dict(r) for r in con.execute(
            f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(change.get('closed') or [0]))})",
            change.get("closed") or [0])]
    finally:
        con.close()
    if mine and mine["status"] != "reverted":
        learn._set_status(mine["id"], "reverted")
        learn._unfile_line(mine, s)
        if mine["kind"] in learn.EVENT_KINDS:
            learn.render_upcoming(s)
    con = kstore.db()
    try:
        for o in olds:
            if o["status"] == "replaced":
                con.execute("UPDATE facts SET status='active', replaced_by=NULL, invalid_at=NULL WHERE id=?",
                            (o["id"],))
        con.commit()
    finally:
        con.close()
    for o in olds:
        if o["status"] == "replaced":
            _refile({**o, "status": "active"}, s)
    return "reverted"


def _held_out_lost(gate, before_ids, sample):
    try:
        after = gate.answered(sample)
    except Exception as e:  # noqa: BLE001
        return None, f"held-out check could not run: {type(e).__name__}: {e}"[:200]
    return sorted(before_ids - after), ""


def settle(gate, s, change, sample, before, passed, passed_note, exclude_note=""):
    """After a change is applied: re-index, run the held-out sample again, keep or revert. Returns
    (status, checks dict, reason)."""
    err = reindex_quietly(gate)
    lost, lerr = _held_out_lost(gate, before, sample)
    checks = {"recheck": passed_note, "recheck_passed": bool(passed), "held_out_n": len(sample),
              "held_out_before": len(before), "held_out_lost": lost}
    if err:
        checks["reindex_error"] = err
    why = ""
    if lost is None:
        why = lerr
    elif not passed:
        why = "the re-check still fails: " + passed_note
    elif lost:
        why = f"{len(lost)} held-out quiz item(s) lost: " + ", ".join(lost[:4])
    if not why and not err:
        return "kept", checks, ""
    revert_change(change, s)
    reindex_quietly(gate)
    return "reverted", checks, why or f"re-index failed: {err}"


def _to_you(text):
    """A fact in the owner's first person, said back to them: "my team meets Tue" -> "your team meets Tue"."""
    for pat, rep in ((r"\bI'm\b", "you're"), (r"\bI am\b", "you are"), (r"\bI was\b", "you were"),
                     (r"\bI(?='ve|'ll|'d)", "you"), (r"\bI\b", "you"), (r"\b[Mm]y\b", "your"), (r"\b[Mm]e\b", "you"),
                     (r"\bmine\b", "yours")):
        text = re.sub(pat, rep, text)
    return text


def _short(text, n=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


# ---- the retrieval path ---------------------------------------------------------------------------------------

def retrieval_check(gate, query, expect):
    """(passed, note) for one question, through what a chat turn is given. A quiz answer (mode "answer") must come
    back in a gated retrieval hit, which is what the quiz scores. A fact (mode "fact": most of its content words)
    may come back in a hit or sit in the core block every turn gets (the profile, the active projects, what is
    coming up), because that is how the chat reaches who the user is."""
    got = gate.hits(query)
    low = [h["text"].lower() for h in got]
    paths = ", ".join(h["path"] for h in got[:3]) or "nothing"
    if expect["mode"] == "answer":
        passed = any(expect["text"].lower() in t for t in low)
        return passed, ("found in " if passed else "retrieved ") + paths
    want = _words(expect["text"])

    def covers(text):
        return bool(want) and len(want & _words(text)) / len(want) >= 0.8
    if covers(gate.core()):
        return True, "found in the core block"
    if any(covers(t) for t in low):
        return True, "found in " + paths
    return False, f"retrieved {paths}, and the core block does not hold it"


def _vault_notes_with(text, s):
    from . import memory, vault
    root, found = vault.root(s), []
    needle = text.lower()
    for p in vault.notes(s):
        rel = p.relative_to(root).as_posix()
        if memory.indexable(rel) and needle in memory.clean(p.read_text(encoding="utf-8", errors="replace")).lower():
            found.append(rel)
    return found[:5]


def record_hint(query, expected, answer):
    """A retrieval miss that only code can fix: remember what should have been found (kstore state)."""
    hints = kstore.get("learnloop.hints", []) or []
    hints = [h for h in hints if h.get("query") != query][-99:]
    hints.append({"query": query[:300], "expected_note": expected, "answer": str(answer)[:120], "time": time.time()})
    kstore.put("learnloop.hints", hints)


# ---- handling one ledger row ----------------------------------------------------------------------------------

class Ctx:
    def __init__(self, s, now, embedder=None, chat=None, asker=None, dry_run=False):
        self.s, self.now, self.embedder, self.chat, self.dry_run = s, now, embedder, chat, dry_run
        self.claude = None if dry_run else Claude(s, asker)
        self.gate = Gate(s, embedder)
        self.changes = 0


def _finish(row, status, reason="", **fields):
    if row.get("evidence") is not None:
        fields.setdefault("evidence", row["evidence"])  # what the handler learned about the signal stays with it
    update(row["id"], status=status, reason=reason, **fields)
    return {"id": row["id"], "status": status, "reason": reason, "summary": fields.get("summary") or row["summary"]}


def _retrieval_route(ctx, row, query, expect, answer, evidence_extra=None):
    """The fact is (or the answer is) in the store but retrieval may not bring it back: re-index and ask again;
    when it still fails, record a hint and wait for code."""
    passed, note = retrieval_check(ctx.gate, query, expect)
    if passed:
        return _finish(row, "noted", f"retrieval already finds it ({note})")
    err = reindex_quietly(ctx.gate)
    passed, note = retrieval_check(ctx.gate, query, expect)
    if passed:
        return _finish(row, "noted", "the index was stale; re-indexed and retrieval finds it now")
    where = _vault_notes_with(str(answer), ctx.s) if expect["mode"] == "answer" else []
    record_hint(query, where[0] if where else "", answer)
    ev = {**(row["evidence"] or {}), **(evidence_extra or {}), "query": query, "expected": str(answer)[:160],
          "expected_in": where, "observed": note, "reindexed": not err}
    return _finish(row, "needs_code", f"retrieval does not bring it back ({note})", kind="retrieval_miss",
                   evidence=ev, summary=f"Retrieval misses a stored answer: {_short(query, 70)}")


def _fact_route(ctx, row, fact, query, *, turn=None, source, evidence, origin=None, replace=True, expect_mode="fact",
                label):
    """File `fact`, re-check with `query`, keep or revert. Returns the result dict."""
    exclude = [(row["evidence"] or {}).get("quiz_item")] if (row["evidence"] or {}).get("quiz_item") else []
    sample = ctx.gate.sample(exclude)
    try:
        before = ctx.gate.answered(sample)
    except Exception as e:  # noqa: BLE001  (retrieval is down: change nothing, try again another night)
        return _finish(row, "deferred", f"cannot prove a change tonight: {type(e).__name__}: {e}"[:200])
    change, why = apply_fact(fact, ctx.s, turn=turn, source=source, evidence=evidence, origin=origin, replace=replace)
    if not change:
        if why.startswith("already a known fact"):
            return _retrieval_route(ctx, row, query, {"mode": "fact", "text": fact["text"]}, fact["text"])
        return _finish(row, "skipped", f"not filed: {why}")
    ctx.changes += 1
    err = reindex_quietly(ctx.gate)
    try:
        passed, note = retrieval_check(ctx.gate, query, {"mode": expect_mode, "text": change["text"]})
    except Exception as e:  # noqa: BLE001
        passed, note = False, f"re-check error: {type(e).__name__}: {e}"[:160]
    if err:
        note += f" (re-index: {err})"
    status, checks, reason = settle(ctx.gate, ctx.s, change, sample, before, passed, note)
    said = _short(_to_you(change["text"]))
    summary = f"{said} ({label})" if status == "kept" else f"{said} ({label}; I put it back)"
    events.log("learnloop", action=status, id=row["id"], text=change["text"][:200], reason=reason[:200])
    return _finish(row, status, reason, change=change, checks=checks, summary=summary)


def handle_correction(ctx, row):
    ev = row["evidence"] or {}
    turn = _turn(ev.get("trace_id") or row["trace_id"])
    prev = _turn(ev.get("answered_trace"))
    if not turn or not prev:
        return _finish(row, "noted", "the turns are gone from the records")
    verdict = {"kind": ev.get("verdict") or "wrong", "claim": ev.get("claim") or ""}
    kind, fact, why, via = word_claim(turn, prev, verdict, ctx.s, chat=ctx.chat, claude=ctx.claude)
    ev = {**ev, "claim_via": via}
    if kind == "deferred":
        if ctx.now - row["created"] > DEFER_GIVE_UP_S:
            return _finish(row, "noted", f"gave up after 3 days: {why}", evidence=ev)
        return _finish(row, "deferred", why, evidence=ev)
    if kind == "style":
        return _finish(row, "needs_code", why or "a change of behaviour, not a fact", kind="route_style", evidence=ev,
                       summary=f"The owner corrected the style or reading of an answer: {_short(prev.get('request'), 70)}")
    if kind != "fact" or not fact:
        failed = [st for st in prev.get("steps", []) if st.get("ok") == 0 and not st.get("denied")]
        if failed or prev.get("turn_outcome") in ("degraded", "failed"):
            tool = (failed[0].get("tool") or "?").split("__")[-1] if failed else "the turn"
            ev["tool_failed"] = tool
            return _finish(row, "needs_code", f"the answer that was corrected had a failed tool ({tool})",
                           kind="tool_failure", evidence=ev,
                           summary=f"A corrected answer came after a failed tool ({tool}): {_short(prev.get('request'), 60)}")
        return _finish(row, "noted", why or "no fact to file", evidence=ev)
    if ctx.changes >= MAX_CHANGES:
        return {"id": row["id"], "status": "open", "reason": "change limit for tonight reached"}
    row = {**row, "evidence": ev}
    return _fact_route(ctx, row, fact, prev.get("request") or "", turn=turn, source="learnloop correction",
                       evidence=turn.get("request") or "", label="you corrected me")


def handle_quiz(ctx, row):
    ev = row["evidence"] or {}
    q, ans = ev.get("question", ""), ev.get("answer", "")
    expect = {"mode": "answer", "text": ans}
    try:
        passed, note = retrieval_check(ctx.gate, q, expect)
    except Exception as e:  # noqa: BLE001
        return _finish(row, "deferred", f"retrieval is down: {type(e).__name__}: {e}"[:200])
    if passed:
        return _finish(row, "noted", f"retrieval answers it now ({note})")
    if _vault_notes_with(ans, ctx.s):  # the answer is in the vault: retrieval is what misses it
        return _retrieval_route(ctx, row, q, expect, ans)
    if ctx.changes >= MAX_CHANGES:
        return {"id": row["id"], "status": "open", "reason": "change limit for tonight reached"}
    sentence = q.replace("___", ans, 1).strip()
    row = {**row, "evidence": {**ev, "quiz_item": ev.get("id")}}
    return _fact_route(ctx, row, {"kind": "fact", "text": sentence}, q, source="learnloop quiz miss",
                       evidence=f"note {ev.get('note')}: {sentence}", origin="memory", replace=False,
                       expect_mode="answer", label="my knowledge quiz missed it")


def _turn(trace_id):
    from . import traces
    return traces.get(int(trace_id)) if trace_id else None


# ---- collecting signals ---------------------------------------------------------------------------------------

def _sig(text):
    return hashlib.sha1(re.sub(r"\d+", "#", (text or "").lower()).strip()[:60].encode()).hexdigest()[:10]


def collect_corrections(rows_, now, lookback, known=frozenset()):
    out = []
    for c in find_corrections(rows_, since=now - lookback * 86400, known=known):
        turn, prev, v = c["turn"], c["prev"], c["verdict"]
        ev = {"trace_id": turn["id"], "answered_trace": prev["id"], "verdict": v["kind"], "rule": v["rule"],
              "claim": v["claim"], "asked": (prev.get("request") or "")[:300], "answered": (prev.get("reply") or "")[:300],
              "said": (turn.get("request") or "")[:300]}
        try:  # context only: the notes the chat had injected around then
            from . import memory
            ev["retrieved_around"] = sorted({h["path"] for r in memory.retrieval_log(prev["time"] - 600, mode="inject")
                                             if r["time"] <= prev["time"] + 5 for h in (r.get("paths") or [])})[:6]
        except Exception:  # noqa: BLE001
            pass
        out.append({"key": f"turn:{turn['id']}", "source": "correction", "kind": "wrong_fact",
                    "summary": f"The owner corrected an answer: {_short(prev.get('request'), 80)}", "evidence": ev,
                    "trace_id": turn["id"], "prev": prev, "turn": turn})
    return out


def collect_quiz(now):
    last = kstore.get("quiz.last") or {}
    if not last or now - float(last.get("time") or 0) > QUIZ_MAX_AGE_S:
        return [], "no quiz run in the last 40 h"
    out = []
    for m in last.get("misses") or []:
        if not m.get("id"):
            continue
        out.append({"key": f"quiz:{m['id']}", "source": "quiz", "kind": "missing_fact" if m.get("in_vault") is False
                    else "retrieval_miss", "summary": f"Quiz miss: {_short(m.get('question'), 80)}",
                    "evidence": {"id": m["id"], "note": m.get("note"), "question": m.get("question"),
                                 "answer": m.get("answer"), "in_vault": m.get("in_vault"), "quiz_day": last.get("day")},
                    "trace_id": None})
    return out, ""


def collect_failures(rows_, now, lookback):
    """One candidate per (tool, error): failed tool steps and failed outcomes of the user's own turns."""
    groups = {}
    since = now - lookback * 86400
    for r in sorted(rows_, key=lambda x: x["id"]):
        if r["time"] < since or not learn.is_user_turn(r):
            continue
        bad = [st for st in r.get("steps", []) if st.get("ok") == 0 and not st.get("denied")]
        for st in bad:
            tool = (st.get("tool") or "?").split("__")[-1]
            key = f"tool:{tool}:{_sig(st.get('result'))}"
            g = groups.setdefault(key, {"key": key, "source": "failed_turn", "kind": "tool_failure", "trace_id": r["id"],
                                        "summary": f"A tool keeps failing: {tool}: {_short(st.get('result'), 60)}",
                                        "evidence": {"tool": tool, "error": (st.get("result") or "")[:200],
                                                     "turns": []}})
            g["evidence"]["turns"].append(r["id"])
        if r.get("turn_outcome") == "failed" and not bad:
            try:
                why = (json.loads(r.get("outcome_why") or "[]") or ["failed"])[0]
            except ValueError:
                why = "failed"
            key = f"outcome:{_sig(why)}"
            g = groups.setdefault(key, {"key": key, "source": "failed_turn", "kind": "tool_failure", "trace_id": r["id"],
                                        "summary": f"A turn failed: {_short(why, 80)}",
                                        "evidence": {"outcome": why, "turns": []}})
            g["evidence"]["turns"].append(r["id"])
    return list(groups.values())


# ---- the run --------------------------------------------------------------------------------------------------

def preview(ctx, c):
    """What one signal would do, without writing anything (the dry run): the claim that would be filed and what it
    would replace, or whether retrieval already answers a quiz item. Local model allowed, never Claude."""
    ev = c["evidence"]
    if c["source"] == "failed_turn":
        return {"would": "record as needs_code", "tool": ev.get("tool") or ev.get("outcome"), "turns": ev.get("turns")}
    if c["source"] == "quiz":
        try:
            passed, note = retrieval_check(ctx.gate, ev.get("question", ""), {"mode": "answer", "text": ev.get("answer", "")})
        except Exception as e:  # noqa: BLE001
            return {"would": "wait: retrieval is down", "error": f"{type(e).__name__}"}
        if passed:
            return {"would": "nothing: retrieval answers it now", "note": note}
        where = _vault_notes_with(ev.get("answer", ""), ctx.s)
        return ({"would": "re-index, then record a hint (needs_code) if it still fails", "in": where} if where else
                {"would": "file the note's sentence as a fact", "fact": (ev.get("question") or "").replace(
                    "___", ev.get("answer", ""), 1)})
    kind, fact, why, via = word_claim(c["turn"], c["prev"], {"kind": ev["verdict"], "claim": ev["claim"]}, ctx.s,
                                      chat=ctx.chat, claude=None)
    out = {"said": ev["said"], "answered": ev["answered"][:120], "verdict": kind, "via": via}
    if fact:
        replaced = _best_to_replace(fact["text"])
        out.update(would="file the fact, re-check, keep or revert", fact=fact["text"],
                   replaces=replaced["text"] if replaced else None)
    else:
        out.update(would={"style": "record as needs_code", "deferred": "wait for a night with budget"}.get(
            kind, "note only"), why=why)
    return out


@vault_git.recorded("learnloop", "learning-loop pass")
def run(settings=None, now=None, embedder=None, chat=None, asker=None, dry_run=False, lookback_days=None):
    """One pass of the loop. Returns a report. With dry_run nothing is written (no ledger row, no trace mark, no
    fact, no re-index, no Claude call): it lists what each new signal would do."""
    from . import traces
    s = settings or Settings.load()
    now = time.time() if now is None else now
    lookback = lookback_days or LOOKBACK_DAYS
    rep = {"signals": {"correction": 0, "quiz": 0, "failed_turn": 0}, "results": [], "notes": []}
    known = known_keys()
    rows_ = traces.all_traces(now - (lookback + 1) * 86400, limit=5000, include_non_user=True)
    found = collect_corrections(rows_, now, lookback, known)
    quiz_items, why = collect_quiz(now)
    if why:
        rep["notes"].append(why)
    failures = collect_failures(rows_, now, lookback)
    ctx = Ctx(s, now, embedder, chat, asker, dry_run)
    new_code = 0
    for c in found + quiz_items + failures:
        if c["key"] in known:
            if c["source"] == "failed_turn" and not dry_run:
                _bump_failure(c)  # the same failure again: counted on its existing row
            continue
        if c["source"] == "failed_turn":
            if new_code >= MAX_NEW_CODE:
                continue
            new_code += 1
        rep["signals"][c["source"]] += 1
        if dry_run:
            try:
                res = preview(ctx, c)
            except Exception as e:  # noqa: BLE001
                res = {"would": "error", "error": f"{type(e).__name__}: {e}"[:200]}
            rep["results"].append({"signal": c["key"], "kind": c["kind"], **res})
            continue
        add(c["key"], c["source"], c["kind"], c["summary"], c["evidence"], c.get("trace_id"),
            "needs_code" if c["source"] == "failed_turn" else "open",
            origin="memory" if c["source"] == "quiz" else "user", now=now)
        if c["source"] == "correction" and not c["prev"].get("feedback"):
            traces.set_feedback(c["prev"]["id"], -1, c["turn"].get("request") or "")  # the mark on the answered turn
    if dry_run:
        return rep
    for row in rows(("open", "deferred"), source=("correction", "quiz")):
        handler = handle_correction if row["source"] == "correction" else handle_quiz
        try:
            res = handler(ctx, row)
        except Exception as e:  # noqa: BLE001  (one bad row must not stop the rest)
            res = {"id": row["id"], "status": "error", "reason": f"{type(e).__name__}: {e}"[:200]}
            events.log("learnloop", action="error", id=row["id"], error=res["reason"])
        rep["results"].append(res)
    rep["claude_calls"] = ctx.claude.used
    rep["changes"] = ctx.changes
    events.log("learnloop", action="run", signals=rep["signals"], changes=ctx.changes,
               statuses=[r.get("status") for r in rep["results"]])
    return rep


def _bump_failure(c):
    con = _con()
    try:
        r = con.execute("SELECT id, evidence FROM improvements WHERE signal_key=?", (c["key"],)).fetchone()
    finally:
        con.close()
    if not r:
        return
    ev = json.loads(r["evidence"] or "{}")
    turns = sorted(set(ev.get("turns") or []) | set(c["evidence"].get("turns") or []))[-20:]
    if turns != sorted(ev.get("turns") or []):
        update(r["id"], evidence={**ev, "turns": turns, "count": len(turns)})


# ---- undo and the evening note --------------------------------------------------------------------------------

UNDO_CMD = re.compile(r"^\s*undo\s+#?L(\d{1,6})\s*[.!]*\s*$", re.I)


@vault_git.recorded("learnloop", lambda r, a, k: f"undo L{a[0]}")
def undo(row_id, settings=None):
    """(ok, message) for the owner's "undo L<id>": reverts a kept change, or dismisses a request that had none."""
    s = settings or Settings.load()
    row = get(row_id)
    if not row:
        return False, f"I have no change L{row_id}. Nothing was done."
    st = row["status"]
    if st == "kept":
        revert_change(row["change"], s)
        reindex_quietly(Gate(s))
        update(row["id"], status="undone", reason="the owner undid it")
        events.log("learnloop", action="undone", id=row["id"])
        ch = row["change"] or {}
        back = f" and put back '{_short(ch['closed_text'][0], 70)}'" if ch.get("closed_text") else ""
        return True, f"Undone L{row['id']}: I removed '{_short(ch.get('text'), 80)}'{back}. I won't try that fix again."
    if st in ("reverted", "undone"):
        return False, f"L{row['id']} is already reverted. Nothing to do."
    if st == "dismissed":
        return False, f"L{row['id']} was already dismissed."
    update(row["id"], status="dismissed", reason="the owner dismissed it")
    return True, f"L{row['id']} had no change to undo; I dismissed it."


def undo_reply(row_id, settings=None):
    try:
        return undo(row_id, settings)[1]
    except Exception as e:  # noqa: BLE001
        events.log("learnloop", action="undo_failed", id=row_id, error=str(e)[:200])
        return f"I could not undo L{row_id}: {type(e).__name__}. Nothing was changed."


def evening_lines(now=None, mark=True, settings=None):
    """The evening note's lines: one per kept or reverted change and per row that needs code, not yet reported (or
    reported earlier today, so a recompose repeats them). "" when there is nothing; never raises."""
    try:
        now = time.time() if now is None else now
        day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        con = _con()
        try:
            got = [_row(r) for r in con.execute(
                "SELECT * FROM improvements WHERE status IN ('kept','reverted','needs_code') AND updated>=? "
                "AND (reported IS NULL OR reported>=?) ORDER BY id", (now - 36 * 3600, day0))]
        finally:
            con.close()
        if not got:
            return ""
        lines, code = [], []
        for r in got:
            if r["status"] == "kept":
                lines.append(f"Fixed: {r['summary']}. Undo: reply 'undo L{r['id']}'.")
            elif r["status"] == "reverted":
                lines.append(f"Tried a fix, put it back: {r['summary']} ({_short(r['reason'], 70)}) [L{r['id']}].")
            else:
                code.append(r)
        lines += [f"Needs a code change: {r['summary']} [L{r['id']}]." for r in code[:CODE_LINES]]
        if len(code) > CODE_LINES:
            lines.append(f"...and {len(code) - CODE_LINES} more code requests are in the improvement ledger "
                         "(python -m waggle.learnloop list).")
        if mark:
            for r in got:
                update(r["id"], now=r["updated"], reported=now)
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        events.log("learnloop", action="evening_failed", error=f"{type(e).__name__}: {e}"[:200])
        return ""


# ---- the core job ---------------------------------------------------------------------------------------------

def _background(fn):
    import contextvars

    from . import budget

    def go():
        budget.set_background(PURPOSE)
        return fn()
    return contextvars.copy_context().run(go)


def job(payload=None, *, settings=None, now=None, embedder=None, chat=None, asker=None):
    """Job learnloop.run: one pass of the loop as background work. Returns a short report; raises when it crashed."""
    rep = _background(lambda: run(settings, now, embedder=embedder, chat=chat, asker=asker))
    return {"signals": rep["signals"], "changes": rep.get("changes", 0), "claude_calls": rep.get("claude_calls", 0),
            "statuses": [r.get("status") for r in rep["results"]], "notes": rep["notes"]}


HANDLERS = {KIND: job}


def cron_spec(settings=None):
    """30 min before the evening note (so it can report the night's changes); 20:30 when the setting is unreadable."""
    s = settings or Settings.load()
    try:
        h, m = (int(x) for x in str(s.evening_time).strip().split(":")[:2])
        total = (h * 60 + m - 30) % 1440
        return f"{total % 60} {total // 60} * * *"
    except (ValueError, TypeError):
        return DEFAULT_CRON


def timer_specs(settings=None):
    """(timer id, kind, spec, job kind, catch_up, max_late_s)."""
    return [(TIMER, "cron", cron_spec(settings), KIND, "latest", MAX_LATE_S)]


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        jobs.register(kind, fn, detached=True, max_attempts=2, lease_s=LEASE_S, timeout_s=TIMEOUT_S)
    return list(HANDLERS)


def register_core(now=None, settings=None):
    """Called by the core process at every start (idempotent): the job kind and the nightly timer."""
    from waggle.core import timers
    report = {"jobs": register_jobs(), "timers": []}
    for tid, kind, spec, job_kind, policy, max_late in timer_specs(settings):
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="learnloop", now=now)
        report["timers"].append(tid)
    return report


# ---- command line ---------------------------------------------------------------------------------------------

def main(argv=None):
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    if cmd == "run":
        days = int(args[args.index("--days") + 1]) if "--days" in args else None
        if "--dry-run" in args:
            print(json.dumps(run(dry_run=True, lookback_days=days), indent=1, default=str, ensure_ascii=False))
            return 0
        from . import budget
        budget.set_background(PURPOSE)
        print(json.dumps(run(lookback_days=days), indent=1, default=str, ensure_ascii=False))
        return 0
    if cmd == "undo" and len(args) > 1 and re.fullmatch(r"L?\d+", args[1], re.I):
        print(undo_reply(int(args[1].lstrip("Ll"))))
        return 0
    for r in rows():
        print(f"L{r['id']:<4} {r['status']:<10} {r['source']:<11} {r['kind']:<14} {r['summary'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
