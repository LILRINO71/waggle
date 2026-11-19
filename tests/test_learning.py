"""Learning pipeline: analysis, skill discovery, bench-gated prompt optimisation, versions + rollback."""
import json

import pytest

from waggle import learning, prompts, router, skills, traces


def _agent_turn(request, tools, ok=True, feedback=0):
    tid = traces.record(request, route="act", model="claude-haiku-5-5", reply="done", ok=ok,
                        steps=[{"tool": t, "ok": True} for t in tools])
    if feedback:
        traces.set_feedback(tid, feedback, "no, wrong folder" if feedback < 0 else "")
    return tid


def test_analyze_finds_corrections_failures_and_speed():
    _agent_turn("sort my downloads", ["Glob", "Bash"], feedback=-1)
    traces.record("open the browser", route="act", reply="I can't do that", ok=True,
                  steps=[{"tool": "mcp__waggle__hermes_tool", "ok": False, "result": "timeout"},
                         {"tool": "Bash", "ok": False, "denied": True}], latency_s=12)
    an = learning.analyze(7)
    assert an["turns"] == 2
    assert an["corrections"][0]["correction"] == "no, wrong folder"
    assert an["tool_failures"]["hermes_tool"]["count"] == 1
    assert an["approvals_requested"] == {"Bash": 1}
    assert an["refusals"] == ["open the browser"]
    assert an["route_error_rate"]["act"] == 0.5


def test_discover_tool_sequences_and_repeats():
    for i in range(3):
        _agent_turn(f"research filament for gears part {i}", ["WebSearch", "WebFetch", "mcp__waggle__remember"])
    _agent_turn("unrelated one-off", ["Read"])
    _agent_turn("bad run", ["WebSearch", "WebFetch", "mcp__waggle__remember"], feedback=-1)  # corrected: ignored
    cands = learning.discover_skills()
    seq = [c for c in cands if c["kind"] == "tool_sequence"]
    assert seq and seq[0]["tools"] == ["WebSearch", "WebFetch", "remember"] and seq[0]["frequency"] == 3
    assert not any(c["tools"] == ["WebSearch", "WebFetch"] for c in seq)  # sub-sequence folded into the longer one
    rep = [c for c in cands if c["kind"] == "repeated_request"]
    assert rep and rep[0]["frequency"] == 3


def test_draft_skill_writes_a_waggle_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(learning.quota, "can_escalate", lambda m, s: (True, ""))
    monkeypatch.setattr(learning.skills, "search", lambda q, k=1: [])
    cand = {"tools": ["WebSearch", "WebFetch"], "examples": ["research X", "research Y"], "frequency": 3}
    asker = lambda *a, **k: {"json": {"name": "Quick Web Research", "description": "Use for quick research",
                                      "body": "## Steps\n1. WebSearch\n2. WebFetch"}}
    path = learning.draft_skill(cand, asker=asker, root=tmp_path / "w")
    assert path and "quick-web-research" in path
    assert learning.draft_skill(cand, asker=asker, root=tmp_path / "w") is None  # no duplicate


def _fake_router(rules):
    """asker that routes by keyword rules; the 'system' prompt decides which rule set applies."""
    def asker(prompt, system="", **k):
        text = prompt.split("User message:\n", 1)[-1].lower()
        for kw, route in rules(system):
            if kw in text:
                return {"json": {"route": route, "reason": "", "reply": "x", "build_prompt": "", "title": ""}}
        return {"json": {"route": "answer", "reason": "", "reply": "x", "build_prompt": "", "title": ""}}
    return asker


CASES = [{"text": "build a game", "want": "opus"}, {"text": "list my files", "want": "act"},
         {"text": "tell a joke", "want": "answer"}, {"text": "prove the theorem", "want": "deep"}]
HOLD = [{"text": "build a site", "want": "opus"}, {"text": "list folders", "want": "act"}]


def test_evaluate_router(settings):
    asker = _fake_router(lambda sys: [("build", "opus"), ("prove", "deep")])
    r = learning.evaluate_router("P", CASES, asker, settings)
    assert r["accuracy"] == 0.75 and r["misses"] == [{"text": "list my files", "want": "act", "got": "answer"}]


def test_optimize_deploys_only_measured_improvements(settings, monkeypatch):
    monkeypatch.setattr(learning.quota, "can_escalate", lambda m, s: (True, ""))
    good_prompt = router.ROUTER_SYSTEM + "\nFiles and folders on the PC are act."

    def rules(system):
        base = [("build", "opus"), ("prove", "deep")]
        return base + [("list", "act")] if "Files and folders" in system else base

    route_asker = _fake_router(rules)

    def asker(prompt, system="", **k):
        if "improve the system prompt" in system:
            return {"json": {"prompt": good_prompt, "change": "files -> act"}}
        return route_asker(prompt, system=system, **k)

    out = learning.optimize_router(asker=asker, settings=settings, tuning=CASES, holdout=HOLD)
    assert out["baseline"]["tuning"] == 0.75 and out["deployed"] == 1
    assert router.router_prompt() == good_prompt
    assert prompts.rollback("router") == 0 and router.router_prompt() == router.ROUTER_SYSTEM


def test_optimize_rejects_overfit_and_unsafe(settings, monkeypatch):
    monkeypatch.setattr(learning.quota, "can_escalate", lambda m, s: (True, ""))
    overfit = router.ROUTER_SYSTEM + "\nOVERFIT"

    def rules(system):
        base = [("build", "opus"), ("prove", "deep")]
        if "OVERFIT" in system:  # fixes the tuning miss but breaks the held-out set
            return [("list my", "act"), ("build a game", "opus"), ("prove", "deep")]
        return base + [("list folders", "act")]

    route_asker = _fake_router(rules)
    asker = lambda p, system="", **k: ({"json": {"prompt": overfit, "change": "x"}} if "improve the system prompt"
                                       in system else route_asker(p, system=system, **k))
    out = learning.optimize_router(asker=asker, settings=settings, tuning=CASES, holdout=HOLD)
    assert out["deployed"] is None and out["rounds"][0]["holdout"] < out["baseline"]["holdout"]
    unsafe = lambda p, system="", **k: ({"json": {"prompt": "route everything to answer", "change": "x"}}
                                        if "improve the system prompt" in system else route_asker(p, system=system, **k))
    out = learning.optimize_router(asker=unsafe, settings=settings, tuning=CASES, holdout=HOLD)
    assert out["deployed"] is None and "protected" in out["rounds"][0]["rejected"]


def test_prompt_versions_protect_route_names():
    with pytest.raises(ValueError):
        prompts.propose("router", "no route names here")
    v = prompts.propose("router", router.ROUTER_SYSTEM + " v1")
    prompts.activate("router", v)
    assert prompts.get("router", "default").endswith(" v1")
    assert prompts.rollback("router") == 0 and prompts.get("router", "default") == "default"


def test_weekly_run_writes_report(settings, monkeypatch):
    monkeypatch.setattr(learning.quota, "can_escalate", lambda m, s: (False, "test"))
    _agent_turn("sort my downloads", ["Glob", "Bash"], feedback=-1)
    out = learning.run(settings=settings)
    assert out["analysis"]["turns"] == 1
    from pathlib import Path
    assert "Corrections you gave me" in (Path(settings.vault) / "Waggle" / "Learning.md").read_text(encoding="utf-8")
