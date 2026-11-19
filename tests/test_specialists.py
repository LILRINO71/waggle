from waggle import bench, router, specialists, vault


def test_bench_scoring_and_domain_rule():
    cases = [{"text": "hey", "want": "simple", "spec": "general"},
             {"text": "quiz me on limits", "want": "reasoning", "spec": "tutor"},
             {"text": "how many cups in a quart", "want": "simple", "spec": "general"}]
    rows = bench.run_cases(cases, sorter=lambda t, s: ("simple", 0.95))  # the 4B model says "simple" to all
    sc = bench.score(rows)
    assert [r["got"] for r in rows] == ["local", "claude", "local"]  # study question still escalates
    assert sc["routing_accuracy"] == 1.0 and sc["wasted_escalation"] == 0
    assert "Routing accuracy" in bench.report(sc, rows)
from waggle.config import Settings


def test_pick():
    assert specialists.pick("quiz me on derivatives for my unit 3 exam") == "tutor"
    assert specialists.pick("what gear ratio for the shooter motor on our FTC robot") == "robotics"
    assert specialists.pick("why does this python function throw a KeyError") == "coder"
    assert specialists.pick("help me plan my week before the deadline") == "planner"
    assert specialists.pick("tell me a joke") == "general"


def test_guide_includes_private_vault_rules():
    s = Settings.load()
    vault.init(s)
    vault.write("Me/Specialists/robotics", "# Robotics rules\n- plywood only", s)
    g = specialists.guide("robotics", s)
    assert "FTC mentor" in g and "plywood only" in g
    assert specialists.guide("general", s) == ""


LONG = "Use a 3:1 ratio on a 6000 rpm motor. " * 20


def test_review_fixes_reply_when_allowed():
    calls = []

    def asker(prompt, **kw):
        calls.append(kw["purpose"])
        return {"json": {"ok": False, "reply": "Use 2:1 instead.", "issues": "ratio too low"}}

    out, ran = specialists.review("shooter ratio?", LONG, "robotics", asker=asker, allowed=lambda s: (True, "ok"))
    assert (out, ran, calls) == ("Use 2:1 instead.", True, ["review"])


def test_review_skips_short_general_or_over_budget():
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call"))  # noqa: E731
    assert specialists.review("x", "short", "robotics", asker=boom, allowed=lambda s: (True, "")) == ("short", False)
    assert specialists.review("x", LONG, "planner", asker=boom, allowed=lambda s: (True, "")) == (LONG, False)
    assert specialists.review("x", LONG, "coder", asker=boom, allowed=lambda s: (False, "budget")) == (LONG, False)


def test_router_uses_specialist_and_review():
    systems = []

    def asker(prompt, **kw):
        systems.append(kw["system"])
        if kw["purpose"] == "route":
            return {"json": {"route": "sonnet", "reason": "r", "reply": LONG, "build_prompt": "", "title": ""}}
        return {"json": {"ok": True, "reply": LONG, "issues": ""}}

    d = router.decide("what gear ratio for our FTC robot shooter?", asker=asker, sorter=lambda t, s: ("reasoning", .9),
                      review_allowed=lambda s: (True, "ok"))
    assert d["specialist"] == "robotics" and d["reviewed"] is True and d["reply"] == LONG
    assert "FTC mentor" in systems[0] and len(systems) == 2
