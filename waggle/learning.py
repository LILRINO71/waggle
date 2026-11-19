"""Learning: Waggle gets better from its own traces, measured, versioned and reversible.

Adapted from OpenJarvis's trace -> learn -> eval loop (LearningOrchestrator, SkillDiscovery, eval-gated
acceptance), sized for one person's assistant:

1. analyze()            what went wrong and where time/tokens go: corrections, failed tool steps, approvals the
                        user keeps granting, slow routes, capability refusals, routes that got corrected
2. discover_skills()    recurring successful tool sequences (OpenJarvis SkillDiscovery) and repeated requests
                        -> skill candidates; draft_skill() writes one as a Waggle skill (Claude, budgeted)
3. evaluate_router()    score a router prompt on the labelled benchmark (bench/routes*.json)
   optimize_router()    GEPA-style reflective step: show the current prompt its misroutes (tuning set only),
                        let Sonnet propose a revision, keep it only if it beats the active prompt on the tuning
                        set without losing on the held-out set. Versioned in prompts.py, one-call rollback.
4. run()                weekly cron: analyze + discover (+ optimize when there is evidence) -> vault report

What learning can never touch: policy.py, security.py, the agent safety rules, SOUL.md (D-08).
Untrusted content is never learned from: only the user's own requests, Waggle's own tool steps and the user's
feedback feed the pipeline.
"""
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import claude_cli, events, prompts, quota, router, skills, traces, vault
from .config import Settings, data_path

BENCH = Path(__file__).resolve().parent.parent / "bench"
REFUSAL = re.compile(r"\b(i can'?t|i cannot|i'?m unable|i don'?t have (access|the ability)|not able to)\b", re.I)


def _short_tool(name):
    return (name or "").split("__")[-1]


# ---- 1. analysis ---------------------------------------------------------------------------------

def analyze(since_days=7, rows=None):
    since = time.time() - since_days * 86400
    rows = rows if rows is not None else traces.all_traces(since)
    out = {"window_days": since_days, "turns": len(rows)}
    if not rows:
        return out
    corrections = [r for r in rows if r.get("feedback") == -1]
    out["corrections"] = [{"request": r["request"][:160], "route": r["route"], "model": r["model"],
                           "correction": (r.get("correction") or "")[:200]} for r in corrections[:15]]
    route_bad = Counter(r["route"] for r in corrections)
    route_all = Counter(r["route"] for r in rows)
    out["route_error_rate"] = {k: round(route_bad[k] / v, 3) for k, v in route_all.items() if v}
    fails = defaultdict(list)
    for r in rows:
        for s in r.get("steps", []):
            if s.get("ok") == 0 and not s.get("denied"):
                fails[_short_tool(s["tool"])].append((s.get("result") or "")[:120])
    out["tool_failures"] = {k: {"count": len(v), "examples": v[:3]} for k, v in
                            sorted(fails.items(), key=lambda x: -len(x[1]))[:10]}
    denied = Counter(_short_tool(s["tool"]) for r in rows for s in r.get("steps", []) if s.get("denied"))
    out["approvals_requested"] = dict(denied.most_common(8))
    lat = defaultdict(list)
    for r in rows:
        if r.get("latency_s"):
            lat[r["route"]].append(r["latency_s"])
    out["latency_by_route"] = {k: {"median": sorted(v)[len(v) // 2], "p90": sorted(v)[int(len(v) * 0.9)],
                                   "n": len(v)} for k, v in lat.items()}
    toks = defaultdict(list)
    for r in rows:
        try:
            u = json.loads(r.get("tokens") or "{}")
        except ValueError:
            u = {}
        if u:
            toks[r["route"]].append(sum(int(u.get(k, 0) or 0) for k in
                                        ("input_tokens", "cache_creation_input_tokens", "output_tokens")))
    out["tokens_by_route"] = {k: int(sum(v) / len(v)) for k, v in toks.items() if v}
    out["refusals"] = [r["request"][:160] for r in rows if REFUSAL.search((r.get("reply") or "")[:400])][:10]
    out["errors"] = Counter((r.get("error") or "")[:80] for r in rows if not r["ok"] and r.get("error")).most_common(5)
    return out


# ---- 2. skill discovery --------------------------------------------------------------------------

def _sequence(r):
    seq = []
    for s in r.get("steps", []):
        if s.get("ok") == 0 or s.get("denied"):
            continue
        t = _short_tool(s["tool"])
        if t in ("TodoWrite", "skill_search", "skill_load") or (seq and seq[-1] == t):
            continue
        seq.append(t)
    return seq


def discover_skills(rows=None, min_freq=3, min_len=2, max_len=4, since_days=30):
    """Recurring tool sequences in successful agent turns, plus clusters of similar successful requests."""
    rows = rows if rows is not None else traces.all_traces(time.time() - since_days * 86400)
    good = [r for r in rows if r["ok"] and r.get("feedback", 0) >= 0 and r.get("steps")]
    grams = defaultdict(list)
    for r in good:
        seq = _sequence(r)
        seen = set()
        for n in range(min_len, max_len + 1):
            for i in range(len(seq) - n + 1):
                g = tuple(seq[i:i + n])
                if g not in seen:
                    seen.add(g)
                    grams[g].append(r)
    found = []
    for g, rs in grams.items():
        if len(rs) < min_freq:
            continue
        # skip a sub-sequence if a longer one with the same support exists
        if any(len(g2) > len(g) and len(rs2) >= len(rs) and _contains(g2, g) for g2, rs2 in grams.items()):
            continue
        ok = [r for r in rs if r.get("feedback", 0) >= 0]
        found.append({"kind": "tool_sequence", "tools": list(g), "frequency": len(rs),
                      "success_rate": round(len(ok) / len(rs), 2),
                      "examples": [r["request"][:200] for r in rs[:5]], "trace_ids": [r["id"] for r in rs[:10]]})
    found += _request_clusters(good, min_freq)
    found.sort(key=lambda c: -c["frequency"])
    path = data_path("skill_candidates.json")
    path.write_text(json.dumps(found, indent=1), encoding="utf-8")
    return found


def _contains(big, small):
    return any(tuple(big[i:i + len(small)]) == small for i in range(len(big) - len(small) + 1))


_STOP = {"the", "and", "for", "with", "my", "me", "to", "a", "an", "of", "in", "on", "is", "it", "can", "you", "please",
         "what", "this", "that", "from", "into", "i", "do", "how", "all", "any", "are", "be", "at", "by", "or"}


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in _STOP}


def _request_clusters(rows, min_freq):
    """Greedy clustering of similar requests (Jaccard >= 0.5) that the agent handled with tools."""
    clusters = []
    for r in rows:
        w = _words(r["request"])
        if len(w) < 2:
            continue
        for c in clusters:
            inter, union = len(w & c["words"]), len(w | c["words"])
            if union and inter / union >= 0.5:
                c["rows"].append(r)
                c["words"] &= w if len(c["words"] & w) >= 2 else c["words"]
                break
        else:
            clusters.append({"words": set(w), "rows": [r]})
    out = []
    for c in clusters:
        if len(c["rows"]) >= min_freq:
            tools = Counter(t for r in c["rows"] for t in _sequence(r))
            out.append({"kind": "repeated_request", "keywords": sorted(c["words"])[:8], "frequency": len(c["rows"]),
                        "tools": [t for t, _ in tools.most_common(5)],
                        "examples": [r["request"][:200] for r in c["rows"][:5]],
                        "trace_ids": [r["id"] for r in c["rows"][:10]]})
    return out


DRAFT_SYSTEM = """You turn a procedure an AI assistant has repeated successfully into a reusable skill (Agent Skills
format). You get example requests and the tools used. Write JSON: {"name": lowercase-with-dashes (2-5 words),
"description": one sentence saying WHEN to use it, "body": markdown with sections When to use, Steps (concrete,
numbered, naming the tools), Pitfalls, Done when}. Generalise from the examples; no personal data, no secrets."""
DRAFT_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                                                "body": {"type": "string"}},
                "required": ["name", "description", "body"]}


def draft_skill(candidate, asker=None, settings=None, root=None):
    """Write a candidate up as a Waggle skill (origin 'discovered'). Returns the path, or None."""
    s = settings or Settings.load()
    ok, why = quota.can_escalate(s.router_model, s)
    if not ok:
        return None
    existing = skills.search(" ".join(candidate.get("examples", [])[:2]), k=1)
    asker = asker or claude_cli.ask
    prompt = json.dumps({"examples": candidate.get("examples", []), "tools": candidate.get("tools", []),
                         "times_seen": candidate.get("frequency"),
                         "closest_existing_skill": existing[0]["name"] if existing else None})
    res = asker(prompt, model=s.router_model, system=DRAFT_SYSTEM, schema=DRAFT_SCHEMA, purpose="learn")
    d = res.get("json") or {}
    if not d.get("name") or not d.get("body"):
        return None
    name = re.sub(r"[^a-z0-9-]+", "-", d["name"].lower()).strip("-")[:60] or "discovered-skill"
    try:
        path = skills.create(name, d.get("description", ""), d["body"], category="discovered",
                             tags=candidate.get("tools", [])[:5], origin="discovered", root=root)
    except FileExistsError:
        return None
    events.log("learning", action="skill_drafted", name=name, frequency=candidate.get("frequency"))
    return path


# ---- 3. router prompt: evaluate + optimise -------------------------------------------------------

def load_cases(name):
    p = BENCH / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def evaluate_router(system=None, cases=None, asker=None, settings=None):
    """Accuracy of a router prompt on labelled cases. Returns {"accuracy", "n", "misses": [...]}."""
    cases = cases if cases is not None else load_cases("routes.json")
    misses, hits = [], 0
    for c in cases:
        try:
            got = router.classify(c["text"], settings=settings, asker=asker, system=system)
        except claude_cli.ClaudeError as e:
            got = f"error:{e.kind}"
        if got == c["want"]:
            hits += 1
        else:
            misses.append({"text": c["text"], "want": c["want"], "got": got})
    n = len(cases)
    return {"accuracy": round(hits / n, 3) if n else None, "n": n, "misses": misses}


def evaluate_skills(cases=None, k=3, search=None):
    """Skill discovery quality: share of queries whose expected skill is in the top k (no model calls)."""
    cases = cases if cases is not None else load_cases("skills.json")
    search = search or (lambda q: skills.search(q, k=k))
    hits, misses = 0, []
    for c in cases:
        got = [h["name"] for h in search(c["query"])][:k]
        if any(w in got for w in c["want"]):
            hits += 1
        else:
            misses.append({"query": c["query"], "want": c["want"], "got": got})
    n = len(cases)
    return {"top_k": k, "accuracy": round(hits / n, 3) if n else None, "n": n, "misses": misses}


OPT_SYSTEM = """You improve the system prompt of a request router for a personal AI assistant. You see the current
prompt and requests it routed wrongly (with the right answer). Rewrite the prompt so those cases would route
correctly WITHOUT breaking others: keep every route name and its meaning, keep it concise, keep the JSON fields
it mentions, and do not add example sentences copied from the failures (generalise the rule instead).
Return JSON {"prompt": the full new prompt, "change": one sentence on what you changed}."""
OPT_SCHEMA = {"type": "object", "properties": {"prompt": {"type": "string"}, "change": {"type": "string"}},
              "required": ["prompt", "change"]}


def optimize_router(asker=None, settings=None, tuning=None, holdout=None, max_rounds=1, deploy=True):
    """One reflective optimisation round. Deploys only on a measured, non-overfit improvement."""
    s = settings or Settings.load()
    tuning = tuning if tuning is not None else load_cases("routes.json")
    holdout = holdout if holdout is not None else load_cases("routes_holdout.json")
    current = router.router_prompt()
    base_t = evaluate_router(current, tuning, asker, s)
    base_h = evaluate_router(current, holdout, asker, s)
    result = {"baseline": {"tuning": base_t["accuracy"], "holdout": base_h["accuracy"]}, "deployed": None,
              "rounds": []}
    if not base_t["misses"]:
        result["note"] = "no misroutes on the tuning set; nothing to optimise"
        return result
    ok, why = quota.can_escalate(s.deep_model, s)
    if not ok:
        result["note"] = f"skipped: {why}"
        return result
    asker_ = asker or claude_cli.ask
    for _ in range(max_rounds):
        prop = asker_(json.dumps({"current_prompt": current, "misrouted": base_t["misses"][:15]}),
                      model=s.deep_model, system=OPT_SYSTEM, schema=OPT_SCHEMA, purpose="learn").get("json") or {}
        cand = (prop.get("prompt") or "").strip()
        if not cand or prompts.violations("router", cand):
            result["rounds"].append({"rejected": "candidate dropped protected route names" if cand else "empty"})
            continue
        t = evaluate_router(cand, tuning, asker, s)
        h = evaluate_router(cand, holdout, asker, s)
        rnd = {"change": prop.get("change", ""), "tuning": t["accuracy"], "holdout": h["accuracy"]}
        better = (t["accuracy"] > base_t["accuracy"] and h["accuracy"] >= base_h["accuracy"]) or \
                 (t["accuracy"] >= base_t["accuracy"] and h["accuracy"] > base_h["accuracy"])
        v = prompts.propose("router", cand, note=prop.get("change", ""), scores={"tuning": t["accuracy"],
                                                                                 "holdout": h["accuracy"]})
        rnd["version"] = v
        if better and deploy:
            prompts.activate("router", v)
            result["deployed"] = v
            events.log("learning", action="router_prompt_deployed", version=v, **{k: rnd[k] for k in
                                                                                  ("tuning", "holdout")})
        result["rounds"].append(rnd)
    return result


# ---- 4. the weekly run ---------------------------------------------------------------------------

def report_markdown(an, cands, opt=None):
    lines = [f"# Waggle learning report", "", f"_{time.strftime('%Y-%m-%d %H:%M')} · last {an.get('window_days')} days · "
             f"{an.get('turns', 0)} turns_", ""]
    if an.get("corrections"):
        lines += ["## Corrections you gave me", ""] + [
            f"- **{c['route']}** · {c['request']} → _{c['correction']}_" for c in an["corrections"]] + [""]
    if an.get("tool_failures"):
        lines += ["## Tools that failed", ""] + [f"- `{k}` ×{v['count']}: {v['examples'][0] if v['examples'] else ''}"
                                                 for k, v in an["tool_failures"].items()] + [""]
    if an.get("approvals_requested"):
        lines += ["## Approvals I asked for", ""] + [f"- `{k}` ×{v}" for k, v in an["approvals_requested"].items()] + [""]
    if an.get("latency_by_route"):
        lines += ["## Speed", "", "| Route | Median | p90 | Turns |", "|---|---|---|---|"] + [
            f"| {k} | {v['median']} s | {v['p90']} s | {v['n']} |" for k, v in an["latency_by_route"].items()] + [""]
    if an.get("refusals"):
        lines += ["## Things I said I couldn't do", ""] + [f"- {r}" for r in an["refusals"]] + [""]
    if cands:
        lines += ["## Procedures I keep repeating (skill candidates)", ""] + [
            f"- ×{c['frequency']} {' → '.join(c.get('tools', []))} · e.g. _{c['examples'][0]}_" for c in cands[:8]] + [""]
    if opt:
        lines += ["## Router prompt", "", f"Baseline: tuning {opt['baseline']['tuning']}, held-out "
                  f"{opt['baseline']['holdout']}. " + (f"Deployed v{opt['deployed']}." if opt.get("deployed") else
                                                         opt.get("note", "No change deployed.")), ""]
    return "\n".join(lines)


def run(optimize=False, draft=True, settings=None):
    s = settings or Settings.load()
    an = analyze(7)
    cands = discover_skills()
    drafted = []
    if draft:
        known = {c.get("name") for c in _load_drafted()}
        for c in cands[:2]:  # at most two new skills a week
            key = "|".join(c.get("tools", [])) + "|" + "|".join(c.get("examples", [])[:1])
            if key in known:
                continue
            path = draft_skill(c, settings=s)
            if path:
                drafted.append(path)
                _save_drafted({"name": key, "path": path, "time": time.time()})
    opt = None
    if optimize or len(an.get("corrections", [])) >= 3:
        opt = optimize_router(settings=s)
    md = report_markdown(an, cands, opt)
    vault.write("Waggle/Learning", md)
    events.log("learning", action="weekly_run", turns=an.get("turns", 0), candidates=len(cands), drafted=len(drafted),
               deployed=(opt or {}).get("deployed"))
    return {"analysis": an, "candidates": len(cands), "drafted": drafted, "optimize": opt}


def _load_drafted():
    p = data_path("skills_drafted.json")
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except ValueError:
        return []


def _save_drafted(item):
    items = _load_drafted() + [item]
    data_path("skills_drafted.json").write_text(json.dumps(items[-200:], indent=1), encoding="utf-8")


def main():
    import sys
    if "bench" in sys.argv:
        for name in ("routes.json", "routes_holdout.json"):
            r = evaluate_router(cases=load_cases(name))
            print(f"{name}: {r['accuracy']} ({r['n']} cases)")
            for m in r["misses"]:
                print(f"  want {m['want']:<9} got {m['got']:<9} {m['text']}")
        return
    out = run(optimize="optimize" in sys.argv)
    if out["drafted"] or (out["optimize"] or {}).get("deployed"):
        print(f"Waggle learned this week: {len(out['drafted'])} new skill(s)"
              + (f", router prompt v{out['optimize']['deployed']}" if (out['optimize'] or {}).get('deployed') else "")
              + ". Report: Waggle/Learning in your vault.")


# ---- MCP -----------------------------------------------------------------------------------------

def _t_learning_report(args):
    return json.dumps({"analysis": analyze(int(args.get("days", 7))),
                       "skill_candidates": discover_skills()[:8], "router_prompt": prompts.versions("router")},
                      default=str)


MCP_TOOLS = [
    ({"name": "learning_report", "description": "What Waggle has learned from recent conversations: corrections, "
      "failed tools, slow routes, repeated procedures that could become skills, router prompt versions.",
      "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": []}},
     _t_learning_report),
]


if __name__ == "__main__":
    main()
