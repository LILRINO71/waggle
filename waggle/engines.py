"""Inference engines behind one interface (OpenJarvis's engine primitive), so models can be swapped without
touching agents, tools or memory.

- OllamaEngine         local models (Waggle's default local engine)
- OpenAICompatEngine   anything speaking /v1/chat/completions: llama.cpp server, vLLM, SGLang, LM Studio,
                       Hermes's `hermes proxy`, or a cloud endpoint configured by the user
- ClaudeCLIEngine      Claude through the Claude Code CLI on the user's plan (no API key)

`discover()` probes the usual local ports (fast, 127.0.0.1 only) and `chat()` walks a fallback chain, so an
outage of one engine degrades to the next instead of failing the turn.
"""
import json
import time
import urllib.error
import urllib.request

from . import cooldowns
from .config import Settings

DEFAULT_PORTS = {  # kind, base url
    "ollama": ("ollama", "http://127.0.0.1:11434"),
    "llama.cpp": ("openai", "http://127.0.0.1:8080/v1"),
    "lm-studio": ("openai", "http://127.0.0.1:1234/v1"),
    "vllm": ("openai", "http://127.0.0.1:8000/v1"),
    "sglang": ("openai", "http://127.0.0.1:30000/v1"),
    "hermes-proxy": ("openai", "http://127.0.0.1:8645/v1"),
}


def _http(url, body=None, timeout=60, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Engine:
    kind = "base"

    def __init__(self, name, url=""):
        self.name, self.url = name, url.rstrip("/")

    def health(self, timeout=1.0):
        try:
            self.models(timeout=timeout)
            return True
        except Exception:
            return False

    def models(self, timeout=5):
        raise NotImplementedError

    def chat(self, messages, model, timeout=120, **opts):
        raise NotImplementedError

    def describe(self):
        return {"name": self.name, "kind": self.kind, "url": self.url}


class OllamaEngine(Engine):
    kind = "ollama"

    def models(self, timeout=5):
        return [m["name"] for m in _http(f"{self.url}/api/tags", timeout=timeout).get("models", [])]

    def chat(self, messages, model, timeout=120, **opts):
        body = {"model": model, "messages": messages, "stream": False, "keep_alive": opts.pop("keep_alive", "30m"),
                "options": opts}
        return _http(f"{self.url}/api/chat", body, timeout).get("message", {}).get("content", "")


class OpenAICompatEngine(Engine):
    kind = "openai"

    def __init__(self, name, url, api_key=""):
        super().__init__(name, url)
        self.api_key = api_key

    def _h(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def models(self, timeout=5):
        return [m["id"] for m in _http(f"{self.url}/models", timeout=timeout, headers=self._h()).get("data", [])]

    def chat(self, messages, model, timeout=120, **opts):
        body = {"model": model, "messages": messages, **{k: v for k, v in opts.items()
                                                         if k in ("temperature", "max_tokens", "top_p")}}
        out = _http(f"{self.url}/chat/completions", body, timeout, headers=self._h())
        return (out.get("choices") or [{}])[0].get("message", {}).get("content", "")


class ClaudeCLIEngine(Engine):
    kind = "claude-cli"

    def __init__(self, name="claude"):
        super().__init__(name, "claude-code-cli")

    def models(self, timeout=5):
        from . import claude_cli
        if not claude_cli.find_claude():
            raise RuntimeError("Claude Code CLI not found")
        return list(claude_cli.MODEL_IDS)

    def chat(self, messages, model, timeout=120, **opts):
        from . import claude_cli
        system = "\n".join(m["content"] for m in messages if m["role"] == "system") or None
        convo = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages if m["role"] != "system")
        return claude_cli.ask(convo, model=model, system=system, purpose=opts.get("purpose", "engine"),
                              effort=opts.get("effort", "low"), timeout=timeout)["text"]


def make(spec):
    kind = spec.get("kind")
    if kind == "ollama":
        return OllamaEngine(spec.get("name", "ollama"), spec.get("url", DEFAULT_PORTS["ollama"][1]))
    if kind == "openai":
        return OpenAICompatEngine(spec.get("name", "openai"), spec["url"], spec.get("api_key", ""))
    if kind == "claude-cli":
        return ClaudeCLIEngine(spec.get("name", "claude"))
    raise ValueError(f"unknown engine kind {kind}")


def configured(settings=None):
    """Engines from settings (plus the two Waggle always has: Claude CLI and Ollama)."""
    s = settings or Settings.load()
    specs = list(getattr(s, "engines", []) or [])
    names = {e.get("name") for e in specs}
    if "claude" not in names:
        specs.append({"name": "claude", "kind": "claude-cli"})
    if "ollama" not in names:
        specs.append({"name": "ollama", "kind": "ollama", "url": s.ollama_url})
    return {sp["name"]: make(sp) for sp in specs}


def discover(timeout=0.4):
    """Which local inference servers are running right now (127.0.0.1 only)."""
    found = []
    for name, (kind, url) in DEFAULT_PORTS.items():
        eng = make({"name": name, "kind": kind, "url": url})
        t0 = time.time()
        try:
            models = eng.models(timeout=timeout)
            found.append({"name": name, "kind": kind, "url": url, "models": models[:30],
                          "latency_ms": int((time.time() - t0) * 1000)})
        except Exception:
            continue
    return found


def chat(messages, chain=None, settings=None, timeout=120, engines=None, **opts):
    """Try (engine, model) pairs in order; returns (text, engine, model). Raises if all fail. An engine that just
    failed with a limit, credit, busy, auth, missing or down error rests for a while (waggle/cooldowns.py)."""
    s = settings or Settings.load()
    engines = engines or configured(s)
    chain = chain or [("claude", s.router_model), ("ollama", s.local_model)]
    errors = []
    for eng_name, model in chain:
        eng = engines.get(eng_name)
        if not eng:
            errors.append(f"{eng_name}: not configured")
            continue
        rest = cooldowns.describe(eng_name, settings=s)
        if rest:  # it failed a moment ago: go straight to the next engine instead of trying it again
            errors.append(f"{eng_name}/{model}: {rest}")
            continue
        try:
            text = eng.chat(messages, model, timeout=timeout, **opts)
            if text:
                return text, eng_name, model
            errors.append(f"{eng_name}/{model}: empty")
        except Exception as e:
            kind = None if getattr(e, "resting", False) else cooldowns.note_error(
                eng_name, getattr(e, "kind", ""), str(e), exc=e, reset_in_hours=getattr(e, "reset_in_hours", None),
                settings=s)
            errors.append(f"{eng_name}/{model}: {type(e).__name__}: {str(e)[:120]}"
                          + (f" (rests after a {kind} error)" if kind else ""))
    raise RuntimeError("all engines failed: " + "; ".join(errors))
