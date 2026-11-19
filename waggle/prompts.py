"""Versioned prompts, so learning can change them safely.

Each named prompt (e.g. "router") has numbered versions in ~/.waggle/prompts/<name>/. The code's built-in
text is version 0. A version only becomes active after it passed the benchmark (learning.py), and the previous
one is always one call away (`rollback`). Protected phrases must survive every version: learning can rephrase
how Waggle routes, never what it is allowed to do.
"""
import json
import time

from .config import data_path

PROTECTED = {
    "router": ['"answer"', '"act"', '"connected"', '"deep"', '"opus"', '"local"'],
}


def _dir(name):
    d = data_path("prompts") / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta(name):
    p = _dir(name) / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"active": 0, "versions": {}}
    except ValueError:
        return {"active": 0, "versions": {}}


def _save_meta(name, meta):
    (_dir(name) / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


def get(name, default):
    """The active version's text (the built-in default when none was ever activated)."""
    meta = _meta(name)
    v = int(meta.get("active", 0))
    if v <= 0:
        return default
    p = _dir(name) / f"v{v}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else default


def violations(name, text):
    return [p for p in PROTECTED.get(name, []) if p not in (text or "")]


def propose(name, text, note="", scores=None):
    """Store a candidate version (inactive). Returns its number."""
    bad = violations(name, text)
    if bad:
        raise ValueError(f"candidate drops protected text: {bad}")
    meta = _meta(name)
    v = max([0] + [int(k) for k in meta["versions"]]) + 1
    (_dir(name) / f"v{v}.txt").write_text(text, encoding="utf-8")
    meta["versions"][str(v)] = {"created": time.time(), "note": note[:500], "scores": scores or {}}
    _save_meta(name, meta)
    return v


def activate(name, v, scores=None):
    meta = _meta(name)
    if v and str(v) not in meta["versions"]:
        raise KeyError(f"no version {v}")
    meta.setdefault("history", []).append({"from": meta.get("active", 0), "to": v, "time": time.time()})
    meta["active"] = v
    if scores and v:
        meta["versions"][str(v)]["scores"] = scores
    _save_meta(name, meta)


def rollback(name):
    """Back to the version that was active before the last activation. Returns it."""
    meta = _meta(name)
    hist = meta.get("history") or []
    prev = hist[-1]["from"] if hist else 0
    activate(name, prev)
    return prev


def versions(name):
    meta = _meta(name)
    return {"active": meta.get("active", 0), "versions": meta.get("versions", {}),
            "history": meta.get("history", [])[-10:]}
