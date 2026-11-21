"""Tiny Ollama client (stdlib only)."""
import json
import re
import urllib.error
import urllib.request

from .config import Settings


class OllamaError(RuntimeError):
    status = None   # the HTTP status when Ollama answered with an error (a 400 on a schema is retried, see chat())


def _post(path, payload, timeout=300, settings=None):
    s = settings or Settings.load()
    req = urllib.request.Request(s.ollama_url.rstrip("/") + path,  # payload None: a GET (/api/ps)
                                 data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        err = OllamaError(f"Ollama not reachable at {s.ollama_url}: {e}")
        err.status = getattr(e, "code", None)
        raise err from e


def chat(messages, model=None, json_mode=False, think=False, settings=None, timeout=300, schema=None):
    """One chat call. schema: a JSON schema (a dict) sent as Ollama's `format`, so the answer is shaped by the
    server and not only by the prompt; temperature is 0 then. If the server refuses the schema (HTTP 400, an
    old Ollama), the call is repeated once with plain JSON mode, so a schema never makes a call fail that would
    have worked."""
    s = settings or Settings.load()
    payload = {"model": model or s.local_model, "messages": messages, "stream": False, "think": think}
    if schema:
        payload["format"] = schema
        payload["options"] = {"temperature": 0}
        try:
            return _post("/api/chat", payload, timeout, s)["message"]["content"]
        except OllamaError as e:
            if e.status != 400:
                raise
            payload["format"] = "json"  # the server does not take a schema: ask for plain JSON as before
    elif json_mode:
        payload["format"] = "json"
    return _post("/api/chat", payload, timeout, s)["message"]["content"]


def parse_json(text):
    """The first JSON object or array in a model answer, tolerating code fences and prose around it. Raises
    OllamaError when there is none. Kept tolerant on purpose: a schema narrows the answer, it never replaces this."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    for cand in ([fenced.group(1)] if fenced else []) + [text]:
        # whichever of an object or an array starts first is the answer (an array of objects must not lose its [ ])
        pairs = sorted((("{", "}"), ("[", "]")), key=lambda p: cand.find(p[0]) if p[0] in cand else len(cand))
        for o, c in pairs:
            a, b = cand.find(o), cand.rfind(c)
            if a >= 0 and b > a:
                try:
                    return json.loads(cand[a:b + 1])
                except json.JSONDecodeError:
                    continue
    raise OllamaError(f"Model did not return JSON: {text[:200]}")


def chat_json(messages, schema=None, **kw):
    """chat() whose answer is parsed JSON. With schema=<JSON schema> Ollama is asked to follow it; without, plain
    JSON mode as before. The parser is tolerant either way (parse_json)."""
    return parse_json(chat(messages, json_mode=True, schema=schema, **kw))


def embed(texts, kind="document", settings=None):
    """nomic-embed-text wants a task prefix on every input."""
    s = settings or Settings.load()
    prefix = "search_query: " if kind == "query" else "search_document: "
    out = _post("/api/embed", {"model": s.embed_model, "input": [prefix + t for t in texts]}, 300, s)
    return out["embeddings"]


def is_up(settings=None):
    s = settings or Settings.load()
    try:
        with urllib.request.urlopen(s.ollama_url.rstrip("/") + "/api/tags", timeout=3):
            return True
    except Exception:
        return False
