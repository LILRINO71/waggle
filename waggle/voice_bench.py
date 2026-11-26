"""Evaluation of "who is being addressed" (waggle/voice_intent.py) on bench/voice_intent.jsonl.

    python -m waggle.bench voice                 the whole pipeline: echo rule, stop rule, then the local model
    python -m waggle.bench voice --model-only    the model alone (no echo or stop rule); an echo counts as not directed
    python -m waggle.bench voice --no-model      the rules alone; whatever they do not catch counts as sent
    python -m waggle.bench voice --model NAME    another Ollama model than settings.local_model
    python -m waggle.bench voice --timeout 30    the judge's patience per case (the first call loads the model)

Each line of the file is one heard transcript with its context: {"id", "text", "last_spoken", "asked", "want"}, want
being directed (send), not_directed (ignore), stop or echo. The score is the share decided as labelled, overall and per
label, with the cases that were missed and how long the model took. Nothing here opens a microphone or plays a sound;
only the Ollama on this PC is called, and not at all with --no-model."""
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from . import voice_intent

REPO = Path(__file__).resolve().parent.parent
CASES = REPO / "bench" / "voice_intent.jsonl"
ACTION = {"send": "directed", "ignore": "not_directed", "stop": "stop", "echo": "echo", "done": "done"}


def load(path=None):
    cases = []
    for line in Path(path or CASES).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def run(cases, *, ask=None, model="", use_model=True, rules=True, timeout=voice_intent.JUDGE_TIMEOUT_S, url=""):
    rows = []
    for c in cases:
        if not rules and c["want"] == "done":  # a sign-off is decided by rule only; the model alone is not asked it
            continue
        d = voice_intent.decide(c["text"], spoken_texts=[c["last_spoken"]] if c.get("last_spoken") else [],
                                last_spoken=c.get("last_spoken", ""), asked_question=bool(c.get("asked")),
                                model=model, ask=ask, use_model=use_model, rules=rules, timeout=timeout, url=url)
        got = ACTION[d.action]
        want = c["want"]
        ok = got == want or (not rules and want == "echo" and got == "not_directed")
        rows.append({**c, "got": got, "ok": ok, "source": d.source, "ms": d.verdict.ms if d.verdict else 0,
                     "confidence": round(d.verdict.confidence, 2) if d.verdict else None, "reason": d.reason})
    return rows


def score(rows):
    per = defaultdict(lambda: [0, 0])
    for r in rows:
        per[r["want"]][1] += 1
        per[r["want"]][0] += int(r["ok"])
    ms = [r["ms"] for r in rows if r["source"] == "model"]
    return {"cases": len(rows), "correct": sum(r["ok"] for r in rows),
            "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 3) if rows else 0.0,
            "by_label": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per.items())},
            "by_source": dict(Counter(r["source"] for r in rows)),
            "model_ms_median": int(statistics.median(ms)) if ms else None, "model_ms_max": max(ms) if ms else None,
            "wrong": [{"id": r["id"], "text": r["text"], "want": r["want"], "got": r["got"], "source": r["source"],
                       "confidence": r["confidence"]} for r in rows if not r["ok"]]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m waggle.bench voice")
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--model", default="")
    ap.add_argument("--model-only", action="store_true")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--url", default="", help="Ollama base URL (default: settings.ollama_url)")
    a = ap.parse_args(argv)
    model, url = a.model, a.url
    if not a.no_model and not (model and url):
        from .config import Settings
        s = Settings.load()
        model, url = model or s.local_model, url or s.ollama_url
    if not a.no_model:
        voice_intent.warm(model, url=url, timeout=a.timeout)  # load it first, so case 1 is not a cold start
    rows = run(load(a.cases), model=model, use_model=not a.no_model, rules=not a.model_only, timeout=a.timeout, url=url)
    sc = score(rows)
    sc["mode"] = "rules only" if a.no_model else ("model only" if a.model_only else "rules + model")
    sc["model"] = "" if a.no_model else model
    print(json.dumps(sc, indent=1))
    return 0
