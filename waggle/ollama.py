"""Tiny Ollama client (stdlib only)."""
import json
import urllib.error
import urllib.request

from .config import Settings


class OllamaError(RuntimeError):
    pass


def _post(path, payload, timeout=300, settings=None):
    s = settings or Settings.load()
    req = urllib.request.Request(s.ollama_url.rstrip("/") + path,  # payload None: a GET (/api/ps)
                                 data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        raise OllamaError(f"Ollama not reachable at {s.ollama_url}: {e}") from e


def chat(messages, model=None, json_mode=False, think=False, settings=None, timeout=300):
    s = settings or Settings.load()
    payload = {"model": model or s.local_model, "messages": messages, "stream": False, "think": think}
    if json_mode:
        payload["format"] = "json"
    return _post("/api/chat", payload, timeout, s)["message"]["content"]


def chat_json(messages, **kw):
    text = chat(messages, json_mode=True, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise OllamaError(f"Model did not return JSON: {text[:200]}")


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
