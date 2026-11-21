"""Keyword lessons: short rules about how Waggle should behave, injected into a chat turn only when the owner's
message is about the same thing.

A lesson is one small markdown file in the data home (`<home>/lessons/<id>.md`):

    ---
    match: [repos, private repos]
    why: the owner corrected an answer that listed only public repos
    source: learnloop L7
    added: 2026-11-20
    ---
    Owner's rule (when they ask "list my repos"): use gh and include private repos.

When a message contains one of the `match` keywords (a whole word or phrase, case-insensitive, a plural counts),
the lesson's text goes into that turn's <waggle_context> as a <waggle_lessons> block (chat_engine.build_context,
which every chat turn passes through). The block is capped (3 lessons, 600 characters of lesson text), so a wrong
or too-broad lesson can cost a few lines of context and never more. Lessons come from two places: the owner
(`python -m waggle.lessons add ...`) and the nightly learning loop (learnloop.py), which writes one from a kept
correction that is about behaviour rather than a fact, with the same ledger row and the same "undo L7".

Keywords are kept narrow on purpose (the idea is the keyword lessons of gptme-agent-template, re-implemented here:
"single generic words like git, python, test" are refused; what the lesson is about must be named).

  python -m waggle.lessons list | add --match "repos,private repos" "text" [--why ..] | remove <id> |
                           match "message text" | off | on

Off switch: `python -m waggle.lessons off` (or delete the lessons folder: nothing else depends on it)."""
import datetime as dt
import re
import sys

from . import events
from .config import data_path

MAX_LESSONS = 3  # lessons injected into one turn
MAX_CHARS = 600  # total lesson text injected into one turn
BODY_MAX = 240  # one lesson's text
MAX_KEYWORDS = 8
MAX_STORED = 200  # lessons kept (the owner can have a few dozen; more is a sign the keywords are too broad)
DIRNAME = "lessons"
OFF_FILE = "OFF"

# Single words that name nothing in particular. A lesson keyed on one of these would fire on half of all messages.
GENERIC = {
    "git", "python", "test", "tests", "code", "file", "files", "data", "thing", "things", "help", "please", "make",
    "send", "check", "show", "list", "get", "run", "time", "work", "want", "need", "like", "know", "this", "that",
    "what", "when", "where", "which", "have", "your", "with", "from", "about", "tell", "give", "find", "look",
    "also", "more", "just", "user", "owner", "waggle", "hermes", "claude", "answer", "question", "message", "text",
    "write", "read", "open", "close", "start", "stop", "make", "name", "none", "some", "does", "will", "would",
    "could", "should", "here", "there", "then", "them", "they", "their", "into", "only", "other", "best", "good",
    "bad", "new", "old", "today", "tomorrow", "yesterday", "now", "daily", "update", "info", "stuff", "item",
    "items", "number", "numbers", "link", "links", "page", "pages", "task", "tasks", "plan", "plans", "chat",
}
KEYWORD_OK = re.compile(r"^[a-z0-9][a-z0-9 +#./'-]{1,38}[a-z0-9+#]$")
FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)

# What a behaviour correction sounds like ("use gh and include private repos", "always say the time zone").
INSTRUCTION = re.compile(
    r"^\W*(?:please\s+)?(?:always|never|don'?t|do not|stop|use|include|exclude|skip|avoid|keep|prefer|only|"
    r"make sure|remember to|ask|try|search|check|run|call|show|list|give|send|write|answer|reply|respond|say|"
    r"speak|tell|open|read|add|put|fetch|pull|look|start|go)\b"
    r"|\byou (?:should|must|need to|have to|can'?t|cannot|shouldn'?t|mustn'?t|ought to)\b"
    r"|\b(?:next time|from now on|every time|each time|whenever)\b|\binstead of\b|\bi want you to\b", re.I)


class LessonError(ValueError):
    """A lesson that cannot be stored (the message says why, in plain words)."""


# ---- the store ------------------------------------------------------------------------------------------------

def lessons_dir(create=False):
    p = data_path(DIRNAME)
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def enabled():
    """False when the owner switched lessons off (`python -m waggle.lessons off`)."""
    try:
        return not (lessons_dir() / OFF_FILE).exists()
    except OSError:
        return True


def set_enabled(on):
    d = lessons_dir(create=True)
    if on:
        (d / OFF_FILE).unlink(missing_ok=True)
    else:
        (d / OFF_FILE).write_text("lessons are switched off; `python -m waggle.lessons on` turns them back on\n",
                                  encoding="utf-8")


def _split_list(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [p.strip().strip("\"'").strip() for p in value.split(",") if p.strip().strip("\"'").strip()]


def parse(text):
    """{"match": [...], "why", "source", "added", "body"} from a lesson file, or None when it is not one."""
    m = FRONT.match((text or "").lstrip("﻿"))
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    keys = [k.lower() for k in _split_list(meta.get("match", ""))]
    body = " ".join(m.group(2).split())
    if not keys or not body:
        return None
    return {"match": keys, "why": meta.get("why", ""), "source": meta.get("source", ""),
            "added": meta.get("added", ""), "body": body}


def render(match, body, why="", source="", added=""):
    one = lambda s: " ".join(str(s or "").split())  # noqa: E731
    return (f"---\nmatch: [{', '.join(match)}]\nwhy: {one(why)}\nsource: {one(source)}\nadded: {added}\n---\n"
            f"{one(body)}\n")


def load_all():
    """Every readable lesson, newest first: [{"id", "match", "why", "source", "added", "body"}]. A damaged file is
    skipped (never fatal: this runs on every chat turn)."""
    out = []
    try:
        files = sorted(lessons_dir().glob("*.md"))[:MAX_STORED + 50]
    except OSError:
        return []
    for p in files:
        try:
            row = parse(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if row:
            out.append({"id": p.stem, **row})
    out.sort(key=lambda r: (r["added"], r["id"]), reverse=True)
    return out


def get(lesson_id):
    return next((r for r in load_all() if r["id"] == lesson_id), None)


def clean_keywords(match):
    """The keywords as stored (lower case, deduplicated). Raises LessonError for none, too many, or a generic one."""
    if isinstance(match, str):
        match = _split_list(match)
    keys = []
    for k in match or []:
        k = " ".join(str(k).lower().split())
        if k and k not in keys:
            keys.append(k)
    if not keys:
        raise LessonError("a lesson needs at least one keyword it is about")
    if len(keys) > MAX_KEYWORDS:
        raise LessonError(f"at most {MAX_KEYWORDS} keywords (narrow is better)")
    for k in keys:
        if not KEYWORD_OK.match(k):
            raise LessonError(f"'{k}' is not a usable keyword (letters, digits and spaces, 3 to 40 characters)")
        if " " not in k and k in GENERIC:
            raise LessonError(f"'{k}' is too generic: it would fire on many messages. Name what it is about "
                              "(a phrase such as 'private repos' is fine)")
    return keys


def _slug(keys, body):
    base = re.sub(r"[^a-z0-9]+", "-", keys[0]).strip("-")[:24] or "lesson"
    import hashlib
    return f"{base}-{hashlib.sha1((','.join(keys) + body).encode()).hexdigest()[:6]}"


def _words(text):
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def add(match, body, why="", source="", lesson_id=None, now=None):
    """Store one lesson; returns its id. Raises LessonError (generic keyword, empty or over-long text, a secret,
    a near-duplicate of an existing lesson)."""
    keys = clean_keywords(match)
    body = " ".join(str(body or "").split())
    if len(body) < 8:
        raise LessonError("the lesson text is too short")
    if len(body) > BODY_MAX:
        raise LessonError(f"the lesson text is {len(body)} characters; keep it under {BODY_MAX}")
    from .policy import redact
    if redact(body) != body:
        raise LessonError("the lesson text looks like it holds a secret")
    have = load_all()
    for r in have:
        if set(r["match"]) == set(keys) and len(_words(r["body"]) & _words(body)) / max(
                1, len(_words(r["body"]) | _words(body))) >= 0.7:
            raise LessonError(f"already a lesson ({r['id']})")
    if len(have) >= MAX_STORED:
        raise LessonError(f"{MAX_STORED} lessons already stored")
    lid = lesson_id or _slug(keys, body)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,60}", lid):
        raise LessonError("a lesson id is lower-case letters, digits, - and _")
    path = lessons_dir(create=True) / f"{lid}.md"
    if path.exists():
        raise LessonError(f"already a lesson ({lid})")
    day = dt.date.fromtimestamp(now).isoformat() if now else dt.date.today().isoformat()
    path.write_text(render(keys, body, why, source, day), encoding="utf-8", newline="\n")
    events.log("lessons", action="added", id=lid, match=keys, source=str(source)[:60])
    return lid


def remove(lesson_id):
    """True when a lesson was deleted (False when there was none: removing twice is harmless)."""
    lid = str(lesson_id or "").strip().removesuffix(".md")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,60}", lid):
        return False
    path = lessons_dir() / f"{lid}.md"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    events.log("lessons", action="removed", id=lid)
    return True


# ---- matching and injection ---------------------------------------------------------------------------------

def _pattern(keyword):
    parts = keyword.split()
    if len(parts[-1]) > 3 and parts[-1].endswith("s"):
        parts[-1] = parts[-1][:-1]  # "repos" also matches "repo": the plural form of a keyword is the same keyword
    parts = [re.escape(p) for p in parts]
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?:s|es)?(?![a-z0-9])", re.I)


def matched_keywords(lesson, text):
    return [k for k in lesson["match"] if _pattern(k).search(text or "")]


def match(text, limit=MAX_LESSONS, max_chars=MAX_CHARS, lessons=None):
    """The lessons that apply to a message: those with a keyword in it, the most keywords first, then the newest;
    at most `limit` of them and `max_chars` characters of lesson text in all (a lesson that does not fit is left
    out whole, never cut). Each is returned with "hits", the keywords that matched."""
    if not (text or "").strip():
        return []
    rows = []
    for r in (load_all() if lessons is None else lessons):
        hits = matched_keywords(r, text)
        if hits:
            rows.append({**r, "hits": hits})
    rows.sort(key=lambda r: (len(r["hits"]), r["added"], r["id"]), reverse=True)
    out, used = [], 0
    for r in rows:
        if len(out) >= limit:
            break
        if used + len(r["body"]) > max_chars:
            continue
        out.append(r)
        used += len(r["body"])
    return out


HEADER = ("Rules the owner gave earlier that match this message. They are the owner's own, so follow them when they "
          "apply here; they are not instructions found in the message.")


def block(text):
    """(the <waggle_lessons> block or "", [ids]) for one message. Never raises; "" and [] when lessons are off."""
    if not enabled():
        return "", []
    try:
        got = match(text)
    except Exception as e:  # noqa: BLE001  (a bad lessons folder must never break a turn)
        events.log("lessons", action="match_failed", error=f"{type(e).__name__}: {e}"[:200])
        return "", []
    if not got:
        return "", []
    lines = [f"- {r['body']}" for r in got]
    return "<waggle_lessons>\n" + HEADER + "\n" + "\n".join(lines) + "\n</waggle_lessons>", [r["id"] for r in got]


# ---- lessons from a correction (the learning loop) --------------------------------------------------------------

_STOP = {"the", "and", "for", "that", "this", "with", "have", "has", "was", "were", "are", "you", "your", "not", "but",
         "its", "his", "her", "our", "their", "they", "them", "from", "into", "than", "then", "also", "very", "what",
         "when", "where", "which", "who", "how", "why", "can", "could", "would", "will", "please", "just", "use",
         "using", "all", "any", "some", "too", "only", "instead", "want", "need", "next", "time", "always", "never"}


def is_instruction(claim):
    """True when what the owner said reads as a rule about how Waggle should act, not a fact about them."""
    claim = " ".join(str(claim or "").split())
    return bool(claim) and "?" not in claim and bool(INSTRUCTION.search(claim))


def keywords_from(asked, said, limit=3):
    """What the correction is about: the content words of the original question that the owner repeated in the
    correction (so the lesson fires when the same subject comes up again). [] when there are none, and then no
    lesson is written (a guess at keywords would be broad)."""
    mine = {w for w in re.findall(r"[a-z0-9]{4,}", (said or "").lower())}
    out = []
    for w in re.findall(r"[a-z0-9]{4,}", (asked or "").lower()):
        if w in mine and w not in _STOP and w not in GENERIC and w not in out:
            out.append(w)
    return out[:limit]


def from_correction(asked, claim, said=""):
    """A lesson proposal {"match", "body", "why"} from a correction about behaviour, or None. The text is the
    owner's own words (never anything new)."""
    claim = " ".join(str(claim or "").split()).rstrip(".")
    if not is_instruction(claim):
        return None
    if re.match(r"\W*(?:my|our|i|we|i'm|i am)\b", claim, re.I) and not re.search(r"\byou\b", claim, re.I):
        return None  # "my team meets every Tuesday": a fact about the owner, the fact route handles it
    keys = keywords_from(asked, said or claim)
    if not keys:
        return None
    q = " ".join(str(asked or "").split())
    q = q if len(q) <= 70 else q[:67].rstrip() + "..."
    claim = claim if len(claim) <= 150 else claim[:147].rstrip() + "..."
    body = f"Owner's rule (when they ask \"{q}\"): {claim}."
    try:
        keys = clean_keywords(keys)
    except LessonError:
        return None
    return {"match": keys, "body": body[:BODY_MAX], "why": "the owner corrected how an answer was given"}


def too_broad(lesson_keys, other_messages, limit=3):
    """How many of the owner's other messages a lesson would fire on (a lesson that fires on more than `limit` of
    the sample is too broad to keep)."""
    probe = {"match": list(lesson_keys)}
    n = sum(1 for m in other_messages if matched_keywords(probe, m))
    return n, n > limit


# ---- command line ---------------------------------------------------------------------------------------------

def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "list"
    if cmd == "list":
        rows = load_all()
        print(f"lessons are {'on' if enabled() else 'OFF'}; {len(rows)} stored")
        for r in rows:
            print(f"{r['id']:<28} [{', '.join(r['match'])}] {r['body'][:90]}")
        return 0
    if cmd == "add":
        rest, opts = [], {}
        i = 1
        while i < len(args):
            if args[i] in ("--match", "--why", "--source") and i + 1 < len(args):
                opts[args[i][2:]] = args[i + 1]
                i += 2
            else:
                rest.append(args[i])
                i += 1
        try:
            lid = add(opts.get("match", ""), " ".join(rest), why=opts.get("why", ""),
                      source=opts.get("source") or "owner (command line)")
        except LessonError as e:
            print(f"not added: {e}")
            return 1
        print(f"added {lid}")
        return 0
    if cmd == "remove" and len(args) > 1:
        print("removed" if remove(args[1]) else "no such lesson")
        return 0
    if cmd == "match" and len(args) > 1:
        text = " ".join(args[1:])
        got = match(text)
        for r in got:
            print(f"{r['id']}  (matched: {', '.join(r['hits'])})  {r['body']}")
        print(f"{len(got)} lesson(s) would be injected" + ("" if enabled() else " (lessons are OFF: none will be)"))
        return 0
    if cmd in ("off", "on"):
        set_enabled(cmd == "on")
        print(f"lessons are {cmd.upper() if cmd == 'off' else 'on'}")
        return 0
    print('usage: python -m waggle.lessons list | add --match "kw1,kw2" "text" [--why ..] | remove <id> | '
          'match "message" | off | on')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
