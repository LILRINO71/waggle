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


def chat(messages, model=None, json_mode=False, think=False, settings=None, timeout=300, schema=None):
    """schema: a JSON schema dict; Ollama then constrains the reply to it (structured output) and samples it cold.
    Without it, json_mode asks for any JSON."""
    s = settings or Settings.load()
    payload = {"model": model or s.local_model, "messages": messages, "stream": False, "think": think}
    if schema is not None:
        payload["format"] = schema
        payload["options"] = {"temperature": 0}
    elif json_mode:
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


# Embedding models want different task markers on the query and on the stored text. Matched by model name prefix;
# nomic-embed-text is the default and the fallback.
EMBED_STYLES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "qwen3-embedding": ("Instruct: Given a question, retrieve the notes that answer it\nQuery: ", ""),
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
}


def embed_prefixes(model):
    name = (model or "").lower()
    for key, pair in EMBED_STYLES.items():
        if name.startswith(key):
            return pair
    return EMBED_STYLES["nomic-embed-text"]


def embed(texts, kind="document", settings=None):
    """Vectors for texts; each input gets its model's task prefix (query or document)."""
    s = settings or Settings.load()
    q, d = embed_prefixes(s.embed_model)
    prefix = q if kind == "query" else d
    out = _post("/api/embed", {"model": s.embed_model, "input": [prefix + t for t in texts]}, 300, s)
    return out["embeddings"]


def is_up(settings=None):
    s = settings or Settings.load()
    try:
        with urllib.request.urlopen(s.ollama_url.rstrip("/") + "/api/tags", timeout=3):
            return True
    except Exception:
        return False
