"""Who is being addressed: is what the PC microphone just heard meant for Waggle?

The PC voice hears the room. Right after Waggle answers (the follow-up window) and after the wake name alone ("Sir?"),
the owner does not say the wake name again, so a television, a phone call or another person in the room would be
taken for a request. This module decides, for one transcript, what to do with it:

    "send"    it is for Waggle: run the turn
    "ignore"  background talk: drop it (the words are not kept)
    "stop"    "stop", "never mind": cut Waggle's speech off and end the conversation, no turn runs
    "echo"    it mostly matches what Waggle itself just said through the speakers: drop it

Order of the checks, cheapest first: echo (word overlap with what Waggle said in the last minute), the stop phrases
(a whole-utterance rule), then a small local model (Ollama, settings.local_model or pc_voice_judge_model) that is asked
for JSON {"directed": bool, "stop": bool, "confidence": 0..1}. The model is kept loaded (keep_alive). When it does not
answer in time, is down, or answers something unreadable, the result is today's behaviour (source "fallback"): in a
conversation window the words are sent, exactly as before this module existed. A model can only ever make Waggle
ignore more or stop sooner; it can never make Waggle do something it was not asked.

Ideas only from the open-source projects studied in reports/open-source-jarvis-2026-11.md (a "who is being addressed"
judge, echo rejection against the last spoken text, re-listening after a question); no code was taken from them.

    python -m waggle.bench voice        accuracy of this module on bench/voice_intent.jsonl
"""
import difflib
import json
import re
import threading
import time
import urllib.error
import urllib.request

ECHO_WINDOW_S = 60.0  # what Waggle said within this long ago can come back through the microphone
ECHO_MIN_WORDS = 3
ECHO_THRESHOLD = 0.75
# A warm small model answers in about 0.2 s; a slower one is as good as absent, so it is not waited for. The model
# that was here before (settings.local_model, 22 GB) took 20 s to load next to the other Ollama models, so the 3 s
# wait ended in the fallback on 2026-11-21 while the owner waited. gemma4 scored 49/50 on bench/voice_intent.jsonl
# (qwen3:8b 48, qwen3:4b 41), median 0.2 s, 3 GB loaded.
DEFAULT_JUDGE_MODEL = "gemma4:latest"
JUDGE_TIMEOUT_S = 1.5
JUDGE_BACKOFF_S = 60.0  # after a failed judgement the judge is skipped for this long
JUDGE_MIN_CONFIDENCE = 0.5
QUESTION_MIN_CONFIDENCE = 0.3  # Waggle just asked something: an answer is very likely meant for it
STOP_MIN_CONFIDENCE = 0.6
KEEP_ALIVE = "30m"

_WORD = re.compile(r"[a-z0-9']+")
_FILLER = {"ok", "okay", "oh", "no", "please", "sir", "waggle", "hey", "now", "just", "thanks", "thank", "you",
           "alright", "right", "well", "um", "uh"}
STOP_PHRASES = {
    "stop", "stop it", "stop that", "stop talking", "stop speaking", "stop there", "never mind", "nevermind",
    "forget it", "forget that", "cancel", "cancel that", "be quiet", "quiet", "shush", "shut up", "enough",
    "that's enough", "that is enough", "that will do", "that'll do", "that's all", "that is all", "leave it",
    "drop it", "ignore that", "stand down", "hold on", "wait", "never mind that", "stop right there",
}


def words(text):
    """The lower-case words of a transcript, apostrophes kept ("what's")."""
    return _WORD.findall((text or "").lower().replace("’", "'"))


# ---- echo --------------------------------------------------------------------------------------------------

def echo_score(text, spoken):
    """0..1: how much of `text` (a transcript) is in order inside `spoken` (what Waggle said): the share of the
    transcript's words that line up with the spoken words. 1.0 is a word-for-word repeat."""
    a, b = words(text), words(spoken)
    if not a or not b:
        return 0.0
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(x.size for x in blocks) / len(a)


def is_echo(text, spoken_texts, threshold=ECHO_THRESHOLD):
    """True when a transcript of at least ECHO_MIN_WORDS words mostly repeats one of the recently spoken texts."""
    if len(words(text)) < ECHO_MIN_WORDS:
        return False
    return any(echo_score(text, s) >= threshold for s in spoken_texts if s)


class SpokenLog:
    """What Waggle said through the speakers lately (reply sentences, spoken messages, pre-made lines), newest
    last, for echo rejection. add() when speech is handed to the speaker; recent() for the comparison."""

    def __init__(self, window_s=ECHO_WINDOW_S, clock=time.monotonic, keep=8):
        self.window_s, self.clock, self.keep = window_s, clock, keep
        self._items, self._lock = [], threading.Lock()

    def add(self, text):
        text = " ".join(str(text or "").split())
        if text:
            with self._lock:
                self._items.append((self.clock(), text))
                del self._items[:-self.keep]

    def recent(self):
        now = self.clock()
        with self._lock:
            kept = [t for at, t in self._items if now - at <= self.window_s]
        # the last few things said, and the last two said as one (an echo can run across a sentence boundary)
        return kept + ([" ".join(kept[-2:])] if len(kept) > 1 else [])

    def last(self):
        with self._lock:
            return self._items[-1][1] if self._items else ""


# ---- stop --------------------------------------------------------------------------------------------------

def stop_phrase(text):
    """True when the whole utterance is a stop phrase ("stop", "okay never mind", "Waggle, that's enough"). A
    request that only begins with the word ("stop the timer") is not one: it goes on to the judge."""
    w = words(text)
    core = [x for x in w if x not in _FILLER] or w
    if not core or len(w) > 6:
        return False
    return " ".join(core) in STOP_PHRASES or (len(core) == 1 and core[0] in STOP_PHRASES)


# ---- the model ---------------------------------------------------------------------------------------------

SYSTEM = (
    "You listen on a PC microphone for a voice assistant called Waggle and decide whether a transcript was said "
    "TO Waggle. It was NOT if it is background: a television or video, a phone call, the owner talking to another "
    "person, a song, a news reader, or Waggle's own voice coming back through the speakers (it repeats what Waggle "
    "just said). It WAS if it is a request, a question, an answer to what Waggle asked, a thanks or a short reply "
    "to Waggle. stop is true only when the speaker wants Waggle to stop talking or to drop the request "
    "(stop, never mind, be quiet, cancel that, that's enough). confidence is how sure you are, from 0 to 1. "
    'Answer with JSON only: {"directed": true|false, "stop": true|false, "confidence": 0.0}.')

SCHEMA = {"type": "object", "required": ["directed", "stop", "confidence"],
          "properties": {"directed": {"type": "boolean"}, "stop": {"type": "boolean"},
                         "confidence": {"type": "number"}}}


def prompt(text, last_spoken="", asked_question=False):
    said = " ".join((last_spoken or "").split())[:300] or "(nothing just now)"
    return (f'Waggle last said: "{said}"\nWaggle just asked the owner a question: {"yes" if asked_question else "no"}\n'
            f'Heard: "{" ".join((text or "").split())[:400]}"')


def parse_verdict(raw):
    """(directed, stop, confidence) from the model's answer, or None when it cannot be read."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            a, b = raw.find("{"), raw.rfind("}")
            try:
                data = json.loads(raw[a:b + 1]) if a >= 0 < b else None
            except ValueError:
                data = None
    if not isinstance(data, dict) or not isinstance(data.get("directed"), bool):
        return None
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return data["directed"], bool(data.get("stop")) is True, max(0.0, min(1.0, conf))


def ollama_ask(messages, model, timeout, *, url="http://127.0.0.1:11434"):
    """The default model call: Ollama's chat with a JSON schema, thinking off, the model kept loaded. Returns the
    parsed dict; raises on any failure (the caller falls back)."""
    body = {"model": model, "messages": messages, "stream": False, "think": False, "format": SCHEMA,
            "keep_alive": KEEP_ALIVE, "options": {"temperature": 0, "num_predict": 60, "num_ctx": 2048}}
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as r:
        out = json.loads(r.read().decode("utf-8"))
    return json.loads(out["message"]["content"])


def warm(model, *, url="http://127.0.0.1:11434", timeout=60.0):
    """Load the model now (no prompt), so the first real judgement does not wait for it. Never raises."""
    try:
        body = json.dumps({"model": model, "keep_alive": KEEP_ALIVE}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


class Backoff:
    """After the judge fails (down, still loading, too slow) it is skipped for a while, so a judge that is not
    there is never waited for twice in a row. A good answer ends the backoff."""

    def __init__(self, seconds=JUDGE_BACKOFF_S, clock=time.monotonic):
        self.seconds, self.clock, self.until = seconds, clock, 0.0

    def active(self):
        return self.clock() < self.until

    def failed(self):
        self.until = self.clock() + self.seconds

    def worked(self):
        self.until = 0.0


class Verdict:
    def __init__(self, directed, stop, confidence, source, ms=0, error=""):
        self.directed, self.stop, self.confidence, self.source, self.ms, self.error = (
            directed, stop, confidence, source, ms, error)

    def __repr__(self):
        return (f"Verdict(directed={self.directed}, stop={self.stop}, confidence={self.confidence:.2f}, "
                f"source={self.source!r}, ms={self.ms})")


def judge(text, *, last_spoken="", asked_question=False, model="", ask=None, timeout=JUDGE_TIMEOUT_S, url=""):
    """Ask the local model. A failure of any kind (down, slow, unreadable) is Verdict(source="fallback"), whose
    directed is True: today's behaviour."""
    ask = ask or (lambda msgs, m, t: ollama_ask(msgs, m, t, **({"url": url} if url else {})))
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt(text, last_spoken, asked_question)}]
    t0 = time.monotonic()
    try:
        got = parse_verdict(ask(msgs, model, timeout))
        ms = int((time.monotonic() - t0) * 1000)
        if got is None:
            return Verdict(True, False, 0.0, "fallback", ms, "unreadable answer")
        return Verdict(got[0], got[1], got[2], "model", ms)
    except Exception as e:  # noqa: BLE001  (timeout, connection refused, bad JSON: the owner is never blocked by it)
        return Verdict(True, False, 0.0, "fallback", int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {e}"[:120])


# ---- the decision ------------------------------------------------------------------------------------------

class Decision:
    def __init__(self, action, source, reason="", verdict=None):
        self.action, self.source, self.reason, self.verdict = action, source, reason, verdict

    def __repr__(self):
        return f"Decision({self.action!r}, source={self.source!r}, reason={self.reason!r})"


def decide(text, *, spoken_texts=(), last_spoken="", asked_question=False, model="", ask=None, use_model=True,
           timeout=JUDGE_TIMEOUT_S, min_confidence=JUDGE_MIN_CONFIDENCE, rules=True, url=""):
    """What to do with one transcript heard in a conversation window. rules=False skips the echo and stop rules
    (the benchmark measures the model alone with it)."""
    if rules:
        if is_echo(text, spoken_texts):
            return Decision("echo", "rule", "repeats what Waggle just said")
        if stop_phrase(text):
            return Decision("stop", "rule", "a stop phrase")
    if not use_model:
        return Decision("send", "fallback", "no judge")
    v = judge(text, last_spoken=last_spoken or (spoken_texts[-1] if spoken_texts else ""), asked_question=asked_question,
              model=model, ask=ask, timeout=timeout, url=url)
    if v.source == "fallback":
        return Decision("send", "fallback", v.error or "the judge did not answer", v)
    if v.stop and v.confidence >= STOP_MIN_CONFIDENCE:
        return Decision("stop", "model", f"confidence {v.confidence:.2f}", v)
    need = QUESTION_MIN_CONFIDENCE if asked_question else min_confidence
    if v.directed and v.confidence >= need:
        return Decision("send", "model", f"confidence {v.confidence:.2f}", v)
    return Decision("ignore", "model", f"directed={v.directed} confidence {v.confidence:.2f}", v)
