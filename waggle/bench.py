"""Waggle benchmark: is the router keeping the right things local, and how fast is it?

    python -m waggle.bench            # free: heuristics + local sorter + specialist pick on bench/cases.json
    python -m waggle.bench --live     # also summarise real usage from ~/.waggle/events.jsonl

Each case is labelled simple / reasoning / build. The local half of the router either keeps a message
local or escalates it to Claude (Sonnet then decides between answering and an Opus build), so the scores are:
  - correct local:      simple -> local                  (free and fast, as intended)
  - correct escalate:   reasoning/build -> Claude
  - missed escalation:  reasoning/build kept local       (quality risk: a 4B model answers a hard question)
  - wasted escalation:  simple sent to Claude            (costs Pro quota and ~10 s for nothing)
Writes docs/BENCHMARK.md.
"""
import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from . import router, specialists
from .config import Settings, data_path

REPO = Path(__file__).resolve().parent.parent


def local_half(text, s, sorter=None):
    """What the free part of the router does: ('local'|'claude', how, seconds)."""
    t0 = time.time()
    if router.heuristic(text) == "trivial":
        return "local", "heuristic", time.time() - t0
    cat, conf = (sorter or router.local_sort)(text, s)
    cat = router.adjust(text, cat)
    out = "local" if cat == "simple" and conf >= 0.7 else "claude"
    return out, f"sort:{cat}:{conf:.2f}", time.time() - t0


def run_cases(cases, s=None, sorter=None):
    s = s or Settings.load()
    rows = []
    for c in cases:
        got, how, secs = local_half(c["text"], s, sorter)
        rows.append({**c, "got": got, "how": how, "secs": secs, "spec_got": specialists.pick(c["text"])})
    return rows


def score(rows):
    k = Counter()
    for r in rows:
        hard = r["want"] != "simple"
        k[("correct_escalate" if r["got"] == "claude" else "missed_escalation") if hard
          else ("correct_local" if r["got"] == "local" else "wasted_escalation")] += 1
    n = len(rows)
    lat = sorted(r["secs"] for r in rows)
    return {
        "cases": n,
        "routing_accuracy": (k["correct_local"] + k["correct_escalate"]) / n,
        **{key: k[key] for key in ("correct_local", "correct_escalate", "missed_escalation", "wasted_escalation")},
        "kept_local_share": sum(r["got"] == "local" for r in rows) / n,
        "specialist_accuracy": sum(r["spec_got"] == r["spec"] for r in rows) / n,
        "latency_p50_s": statistics.median(lat),
        "latency_p95_s": lat[min(n - 1, int(0.95 * n))],
    }


def live_stats(path=None):
    """Real usage from the event log: route mix, latency per route, reviews, screen looks, calls."""
    p = Path(path or data_path("events.jsonl"))
    if not p.exists():
        return {}
    ev = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    routes = [e for e in ev if e.get("kind") == "route"]
    by = {}
    for e in routes:
        by.setdefault(e.get("route"), []).append(e.get("latency_s", 0) or 0)
    return {
        "messages": len(routes),
        "routes": {k: {"count": len(v), "median_s": round(statistics.median(v), 1)} for k, v in by.items()},
        "reviews": sum(1 for e in ev if e.get("kind") == "review" and e.get("ran")),
        "screen_looks": sum(1 for e in ev if e.get("kind") == "screen_look"),
        "calls": sum(1 for e in ev if e.get("kind") == "call" and e.get("ok")),
    }


def report(sc, rows, live=None):
    pct = lambda x: f"{100 * x:.0f}%"  # noqa: E731
    out = ["# Waggle benchmark", "",
           f"_{time.strftime('%Y-%m-%d %H:%M')} · {sc['cases']} labelled requests · local model "
           f"`{Settings.load().local_model}` · run with `python -m waggle.bench`_", "",
           "## Router (local half: heuristics + local sorter)", "",
           "| Metric | Result |", "|---|---|",
           f"| Routing accuracy | **{pct(sc['routing_accuracy'])}** |",
           f"| Simple requests kept local | {sc['correct_local']} |",
           f"| Hard requests escalated to Claude | {sc['correct_escalate']} |",
           f"| Missed escalations (hard request answered locally) | {sc['missed_escalation']} |",
           f"| Wasted escalations (simple request sent to Claude) | {sc['wasted_escalation']} |",
           f"| Share handled locally for free | {pct(sc['kept_local_share'])} |",
           f"| Specialist pick accuracy | {pct(sc['specialist_accuracy'])} |",
           f"| Routing latency p50 / p95 | {sc['latency_p50_s']:.2f} s / {sc['latency_p95_s']:.2f} s |", ""]
    wrong = [r for r in rows if (r["want"] == "simple") != (r["got"] == "local")]
    if wrong:
        out += ["### Misrouted", "", "| Request | Label | Got | Why |", "|---|---|---|---|"]
        out += [f"| {r['text']} | {r['want']} | {r['got']} | {r['how']} |" for r in wrong] + [""]
    if live:
        out += ["## Real usage (event log)", "", f"{live['messages']} messages routed.", "",
                "| Route | Count | Median latency |", "|---|---|---|"]
        out += [f"| {k} | {v['count']} | {v['median_s']} s |" for k, v in live["routes"].items()]
        out += ["", f"Second-opinion reviews: {live['reviews']} · screen looks: {live['screen_looks']} · "
                f"calls placed: {live['calls']}", ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--cases", default=str(REPO / "bench" / "cases.json"))
    ap.add_argument("--out", default=str(REPO / "docs" / "BENCHMARK.md"))
    a = ap.parse_args(argv)
    rows = run_cases(json.loads(Path(a.cases).read_text(encoding="utf-8")))
    sc = score(rows)
    md = report(sc, rows, live_stats() if a.live else None)
    hold = Path(a.cases).with_name("holdout.json")
    if hold.exists():  # cases written after the last router change and never used to tune it
        hrows = run_cases(json.loads(hold.read_text(encoding="utf-8")))
        hsc = score(hrows)
        sc["holdout_accuracy"] = hsc["routing_accuracy"]
        md += "\n" + report(hsc, hrows).replace("# Waggle benchmark", "## Held-out set (not used for tuning)", 1)
        md = md.replace("## Router (local half: heuristics + local sorter)\n",
                        "## Router (local half: heuristics + local sorter)\n\n_The study/code/robot escalation "
                        "rule was added after an earlier run missed 2 of these 40, so this set is not a fair test of "
                        "it; the held-out set at the bottom is._\n", 1)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(md, encoding="utf-8")
    print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in sc.items()}, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
