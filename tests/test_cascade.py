"""The cheap-first cascade for background calls (waggle/cascade.py): local model, then Haiku, then Sonnet, the first
answer that passes a self-check wins, usage steers it, and the owner's chat never goes through it. Fakes only: no
Claude, no Ollama, no network, a temp home."""
import pytest

from waggle import agents, autowork, budget, cascade, claude_cli, digest, events, goals, ollama
from waggle.config import Settings

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


@pytest.fixture(autouse=True)
def fresh_pressure():
    cascade._pressure_cache.update(at=0.0, value=0.0)
    yield
    cascade._pressure_cache.update(at=0.0, value=0.0)


@pytest.fixture
def s(settings):
    return Settings.load()


@pytest.fixture
def bg(s):
    with budget.background("test"):
        yield s


class Fakes:
    """A local model and a Claude that answer from queues, and remember every call."""

    def __init__(self, local=(), haiku=(), sonnet=()):
        self.q = {"local": list(local), "haiku": list(haiku), "sonnet": list(sonnet)}
        self.calls = []

    def local(self, messages, schema, settings, timeout):
        self.calls.append(("local", messages))
        out = self.q["local"].pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def asker(self, prompt, model=None, system=None, schema=None, effort=None, purpose=None, timeout=None):
        tier = "haiku" if "haiku" in model else "sonnet"
        self.calls.append((tier, prompt))
        out = self.q[tier].pop(0)
        if isinstance(out, Exception):
            raise out
        return {"json": out, "text": "", "usage": {}, "model_id": model}

    def tiers(self):
        return [c[0] for c in self.calls]

    def run(self, s, **kw):
        kw.setdefault("pressure", 0.2)
        return cascade.ask("t", "question?", SCHEMA, kw.pop("need", "answer"), system="sys", settings=s,
                           asker=self.asker, local=self.local, **kw)


def ans(text="a", conf=0.9):
    return {"answer": text, "confidence": conf}


# ---- acceptance at each tier -------------------------------------------------------------------------------

def test_the_local_answer_is_taken_when_valid_and_sure(bg):
    f = Fakes(local=[ans("local", 0.9)])
    r = f.run(bg)
    assert r["tier"] == "local" and r["json"] == {"answer": "local"} and r["low_confidence"] is False
    assert r["confidence"] == 0.9 and f.tiers() == ["local"]  # confidence is stripped from the answer the caller sees


def test_a_callers_own_confidence_field_is_kept(bg):
    schema = {"type": "object", "required": ["answer", "confidence"],
              "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}}}
    f = Fakes(local=[ans("x", 0.9)])
    r = cascade.ask("t", "q", schema, settings=bg, asker=f.asker, local=f.local, pressure=0.2)
    assert r["json"]["confidence"] == 0.9


def test_a_low_confidence_local_answer_goes_to_haiku(bg):
    f = Fakes(local=[ans("meh", 0.5)], haiku=[ans("better", 0.8)])
    r = f.run(bg)
    assert r["tier"] == "haiku" and r["json"] == {"answer": "better"} and f.tiers() == ["local", "haiku"]


def test_an_invalid_local_answer_goes_to_haiku(bg):
    f = Fakes(local=[{"wrong": "shape", "confidence": 0.99}], haiku=[ans("ok", 0.9)])
    r = f.run(bg)
    assert r["tier"] == "haiku" and f.tiers() == ["local", "haiku"]
    assert "answer is missing" in r["steps"][0]["why"]


def test_a_local_model_that_errors_goes_to_haiku(bg):
    f = Fakes(local=[ollama.OllamaError("down")], haiku=[ans("ok", 0.9)])
    assert f.run(bg)["tier"] == "haiku"


def test_haiku_unsure_goes_to_sonnet_and_sonnet_is_always_taken(bg):
    f = Fakes(local=[ans("a", 0.1)], haiku=[ans("b", 0.3)], sonnet=[{"answer": "c"}])  # sonnet gives no confidence
    r = f.run(bg)
    assert r["tier"] == "sonnet" and r["json"] == {"answer": "c"} and f.tiers() == ["local", "haiku", "sonnet"]
    assert r["low_confidence"] is False


def test_a_claude_answer_without_confidence_is_not_trusted_below_sonnet(bg):
    f = Fakes(local=[ans("a", 0.1)], haiku=[{"answer": "b"}], sonnet=[ans("c", 0.2)])
    assert f.run(bg)["tier"] == "sonnet"


def test_a_failing_rule_check_escalates(bg):
    rule = lambda d: (d["answer"] != "bad", "that answer is bad")  # noqa: E731
    f = Fakes(local=[ans("bad", 0.99)], haiku=[ans("good", 0.9)])
    r = f.run(bg, check=rule)
    assert r["tier"] == "haiku" and r["json"]["answer"] == "good"
    assert r["steps"][0]["why"] == "that answer is bad"


def test_a_rule_that_crashes_is_a_failed_rule(bg):
    n = []

    def rule(d):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("boom")
        return True
    f = Fakes(local=[ans("x", 0.99)], haiku=[ans("y", 0.9)])
    r = f.run(bg, check=rule)
    assert r["tier"] == "haiku" and "the rule check failed: RuntimeError: boom" in r["steps"][0]["why"]


def test_strict_last_false_returns_the_last_tiers_answer_even_when_the_rule_refuses_it(bg):
    rule = lambda d: False  # noqa: E731
    f = Fakes(local=[ans("a", 0.99)], haiku=[ans("b", 0.99)], sonnet=[ans("c", 0.99)])
    r = f.run(bg, check=rule, strict_last=False)
    assert r["tier"] == "sonnet" and r["json"]["answer"] == "c"
    f2 = Fakes(local=[ans("a", 0.99)], haiku=[ans("b", 0.99)], sonnet=[ans("c", 0.99)])
    with pytest.raises(cascade.CascadeError):
        f2.run(bg, check=rule)  # strict (the default): nothing usable at all is an error the caller already handles


def test_no_tier_usable_is_a_claude_error_so_callers_keep_their_handling(bg):
    f = Fakes(local=[{"x": 1}], haiku=[{"x": 1}], sonnet=[{"x": 1}])
    with pytest.raises(claude_cli.ClaudeError):
        f.run(bg)


def test_a_plan_starts_at_haiku(bg):
    f = Fakes(haiku=[ans("plan", 0.9)])
    r = f.run(bg, need="plan")
    assert r["tier"] == "haiku" and f.tiers() == ["haiku"]


def test_start_haiku_skips_the_local_model(bg):
    f = Fakes(haiku=[ans("x", 0.9)])
    assert f.run(bg, start="haiku")["tier"] == "haiku" and f.tiers() == ["haiku"]


def test_cascade_local_off_skips_the_local_model(bg):
    bg.cascade_local = False
    f = Fakes(haiku=[ans("x", 0.9)])
    assert f.run(bg)["tier"] == "haiku" and f.tiers() == ["haiku"]


def test_the_default_local_availability_check_skips_an_absent_ollama(bg, monkeypatch):
    called = []
    monkeypatch.setattr(ollama, "chat_json", lambda *a, **k: called.append(1) or ans())
    f = Fakes(haiku=[ans("x", 0.9)])
    r = cascade.ask("t", "q", SCHEMA, settings=bg, asker=f.asker, pressure=0.2)  # no local= seam: the real check runs
    assert r["tier"] == "haiku" and called == []  # conftest has no Ollama, so the check says "not running"


def test_without_a_schema_or_a_rule_there_is_nothing_to_check_the_local_answer_against(bg):
    f = Fakes(haiku=[{"x": 1}])
    f.asker_text = None
    r = cascade.ask("t", "q", None, settings=bg, pressure=0.2, local=f.local,
                    asker=lambda *a, **k: {"text": "plain", "json": None, "usage": {}, "model_id": "haiku"})
    assert r["tier"] == "haiku" and r["text"] == "plain" and f.calls == []


def test_a_text_answer_with_a_rule_is_taken_from_the_local_model_when_the_rule_passes(bg):
    f = Fakes(local=["Paris is the capital"])
    r = cascade.ask("t", "q", None, settings=bg, pressure=0.2, local=f.local, asker=f.asker,
                    check=lambda text: "Paris" in text)
    assert r["tier"] == "local" and r["text"] == "Paris is the capital" and r["json"] is None


# ---- the thresholds and usage ------------------------------------------------------------------------------

@pytest.mark.parametrize("tier, need, pressure, expect", [
    ("local", "answer", 0.0, 0.70), ("local", "judge", 0.0, 0.80), ("local", "plan", 0.0, 0.85),
    ("haiku", "answer", 0.0, 0.60), ("haiku", "judge", 0.0, 0.70),
    ("local", "answer", 0.50, 0.70),                 # at the line: not yet
    ("local", "answer", 0.55, 0.55),                 # half way to the stop line: half the relief
    ("local", "answer", 0.60, 0.40), ("local", "answer", 0.90, 0.40),   # full relief, never below the floor
    ("local", "judge", 0.60, 0.55), ("haiku", "judge", 0.60, 0.55),
    ("sonnet", "judge", 0.0, 0.0),
])
def test_threshold_tightens_with_usage(tier, need, pressure, expect):
    assert cascade.threshold(tier, need, pressure, 0.6) == pytest.approx(expect)


def test_ceiling_is_haiku_at_the_background_stop_line():
    assert cascade.ceiling(0.59, 0.6) == "sonnet" and cascade.ceiling(0.6, 0.6) == "haiku"
    assert cascade.ceiling(0.95, 0.6) == "haiku"


def test_below_half_usage_a_middling_local_answer_escalates_above_it_stays(bg):
    f = Fakes(local=[ans("a", 0.6)], haiku=[ans("b", 0.9)])
    assert f.run(bg, pressure=0.3)["tier"] == "haiku"
    f = Fakes(local=[ans("a", 0.6)], haiku=[ans("b", 0.9)])
    r = f.run(bg, pressure=0.55)
    assert r["tier"] == "local" and f.tiers() == ["local"]


def test_at_the_background_stop_line_sonnet_is_never_called(bg):
    f = Fakes(local=[ans("a", 0.1)], haiku=[ans("b", 0.1)], sonnet=[ans("c", 0.9)])
    r = f.run(bg, pressure=0.7)
    assert "sonnet" not in f.tiers() and r["tier"] == "haiku"
    assert r["low_confidence"] is True and r["json"] == {"answer": "b"}  # Haiku's best, flagged as not sure enough
    assert f.q["sonnet"]  # untouched


def test_above_the_stop_line_a_sure_enough_answer_is_not_low_confidence(bg):
    f = Fakes(local=[ans("a", 0.1)], haiku=[ans("b", 0.5)])
    r = f.run(bg, pressure=0.7)  # Haiku's threshold falls to the 0.40 floor, 0.5 clears it
    assert r["tier"] == "haiku" and r["low_confidence"] is False


def test_the_pressure_is_the_larger_of_the_five_hour_and_the_scaled_seven_day_usage(s, monkeypatch):
    from waggle import quota
    monkeypatch.setattr(quota, "status", lambda settings=None: {"window_pct": 30.0, "seven_day_pct": 80.0})
    monkeypatch.setattr(quota, "seven_day_limit", lambda settings: 0.85)
    # 80 % of a week whose stop line is 85 % sits at 94 % of that line: scaled onto the 60 % background line
    assert cascade.current_pressure(s, now=1000.0) == pytest.approx(0.8 / 0.85 * 0.6)
    monkeypatch.setattr(quota, "status", lambda settings=None: {"window_pct": 55.0, "seven_day_pct": None})
    cascade._pressure_cache.update(at=0.0)
    assert cascade.current_pressure(s, now=2000.0) == pytest.approx(0.55)


def test_an_unreadable_usage_reads_as_zero_and_the_reading_is_cached(s, monkeypatch):
    from waggle import quota
    n = []

    def boom(settings=None):
        n.append(1)
        raise RuntimeError("no usage")
    monkeypatch.setattr(quota, "status", boom)
    assert cascade.current_pressure(s, now=5000.0) == 0.0
    cascade.current_pressure(s, now=5010.0)
    assert len(n) == 1  # within a minute: no second scan of the transcripts


def test_the_pressure_it_reads_steers_a_call_without_a_pressure_argument(bg, monkeypatch):
    from waggle import quota
    monkeypatch.setattr(quota, "status", lambda settings=None: {"window_pct": 70.0, "seven_day_pct": None})
    f = Fakes(local=[ans("a", 0.1)], haiku=[ans("b", 0.1)], sonnet=[ans("c", 0.9)])
    r = cascade.ask("t", "q", SCHEMA, settings=bg, asker=f.asker, local=f.local)
    assert "sonnet" not in f.tiers() and r["low_confidence"] is True
    assert events.read("cascade")[-1]["pressure"] == pytest.approx(0.7)


# ---- when Claude cannot be reached -------------------------------------------------------------------------

def test_when_claude_is_unavailable_the_best_cheap_answer_is_returned_not_sure(bg):
    f = Fakes(local=[ans("local", 0.5)], haiku=[claude_cli.ClaudeError("limit reached", "limit")])
    r = f.run(bg)
    assert r["tier"] == "local" and r["low_confidence"] is True and f.tiers() == ["local", "haiku"]


def test_a_budget_refusal_stops_the_climb_and_keeps_the_local_answer(bg, monkeypatch):
    def no_room(*a, **k):
        raise budget.Deferred("background budget used: 4.00 of 4.00 units today", 0)
    monkeypatch.setattr(budget, "reserve", no_room)
    f = Fakes(local=[ans("local", 0.5)])
    r = f.run(bg)
    assert r["tier"] == "local" and r["low_confidence"] is True and f.tiers() == ["local"]


def test_a_budget_refusal_with_no_cheap_answer_reaches_the_caller_as_deferred(bg, monkeypatch):
    def no_room(*a, **k):
        raise budget.Deferred("background budget used", 0)
    monkeypatch.setattr(budget, "reserve", no_room)
    f = Fakes(local=[ollama.OllamaError("down")])
    with pytest.raises(budget.Deferred):
        f.run(bg)
    assert f.tiers() == ["local"]  # no Claude call was made


def test_a_claude_error_with_no_cheap_answer_is_the_callers_to_handle(bg):
    f = Fakes(local=[ollama.OllamaError("down")], haiku=[claude_cli.ClaudeError("not logged in", "auth")])
    with pytest.raises(claude_cli.ClaudeError) as e:
        f.run(bg)
    assert e.value.kind == "auth" and f.tiers() == ["local", "haiku"]  # auth: Sonnet would fail the same way


def test_local_still_answers_when_the_background_budget_is_spent(bg, monkeypatch):
    s2 = bg
    s2.background_daily_units = 0.0  # "background Claude use is switched off"
    f = Fakes(local=[ans("local", 0.9)])
    assert f.run(s2)["tier"] == "local"  # the work goes on without a single Claude call


def test_a_claude_tier_is_reserved_in_the_background_budget(bg, monkeypatch):
    seen = []
    real = budget.reserve

    def spy(model, purpose="objective", *a, **k):
        seen.append((model, purpose))
        return real(model, purpose, *a, **k)
    monkeypatch.setattr(budget, "reserve", spy)
    f = Fakes(local=[ans("a", 0.1)], haiku=[ans("b", 0.9)])
    f.run(bg, purpose="digest")
    assert seen == [("haiku", "digest")]


# ---- never the owner's chat --------------------------------------------------------------------------------

def test_outside_background_work_the_cascade_refuses(s):
    f = Fakes(local=[ans()])
    with pytest.raises(cascade.CascadeError, match="background work only"):
        f.run(s)
    assert f.calls == []  # not even the local model was asked
    assert not cascade.applies(s)


def test_applies_follows_background_and_the_switch(s):
    with budget.background("x"):
        assert cascade.applies(s)
        s.cascade_enabled = False
        assert not cascade.applies(s)


# ---- logging and counting ----------------------------------------------------------------------------------

def test_every_step_is_an_event_and_the_last_carries_the_outcome(bg):
    f = Fakes(local=[ans("a", 0.5)], haiku=[ans("b", 0.9)])
    f.run(bg)
    ev = events.read("cascade")
    assert [(e["tier"], e["accepted"], e["final"]) for e in ev] == [("local", False, False), ("haiku", True, True)]
    assert "below" in ev[0]["why"] and ev[0]["score"] == 0.5 and ev[0]["threshold"] == 0.7
    assert ev[1]["avoided"] == 0 and ev[1]["task"] == "t" and ev[1]["need"] == "answer"


def test_counts_add_up_calls_avoided_downgrades_and_escalations(bg):
    Fakes(local=[ans("a", 0.9)]).run(bg)                                              # stays local: 1 avoided
    Fakes(local=[ans("a", 0.9)]).run(bg)                                              # again
    Fakes(local=[ans("a", 0.5)], haiku=[ans("b", 0.9)]).run(bg)                       # Haiku where Sonnet ran before
    Fakes(local=[ans("a", 0.1)], haiku=[ans("b", 0.1)], sonnet=[ans("c")]).run(bg)    # all the way up
    c = cascade.counts()
    assert c["calls"] == 4 and c["by_tier"] == {"local": 2, "haiku": 1, "sonnet": 1}
    assert c["avoided"] == 2 and c["downgraded"] == 1 and c["rejected"] == 3
    line = cascade.evening_line()
    assert "4 background answers" in line and "2 stayed local (2 Claude calls avoided)" in line
    assert "1 Haiku answer where Sonnet used to run" in line


def test_a_call_with_no_usable_answer_is_counted_as_failed(bg):
    f = Fakes(local=[ollama.OllamaError("x")], haiku=[claude_cli.ClaudeError("busy", "timeout")],
              sonnet=[claude_cli.ClaudeError("busy", "timeout")])
    with pytest.raises(claude_cli.ClaudeError):
        f.run(bg)
    assert cascade.counts()["failed"] == 1 and "1 with no usable answer" in cascade.evening_line()


def test_the_evening_line_is_empty_when_nothing_ran(s):
    assert cascade.evening_line() == ""


def test_a_local_call_leaves_a_unit_free_row_in_the_budget_ledger(bg):
    Fakes(local=[ans("a", 0.9)]).run(bg, purpose="autowork-pick")
    con = budget._db()
    try:
        rows = con.execute("SELECT purpose, units, local FROM usage").fetchall()
    finally:
        con.close()
    assert [(r["purpose"], r["units"], r["local"]) for r in rows] == [("autowork-pick", 0.0, 1)]


# ---- the schema reader -------------------------------------------------------------------------------------

@pytest.mark.parametrize("data, schema, ok", [
    ({"a": "x"}, {"type": "object", "required": ["a"]}, True),
    ({}, {"type": "object", "required": ["a"]}, False),
    ({"a": 1}, {"type": "object", "properties": {"a": {"type": "string"}}}, False),
    ({"a": True}, {"type": "object", "properties": {"a": {"type": "number"}}}, False),  # a bool is not a number
    ({"a": 0.5}, {"type": "object", "properties": {"a": {"type": "number", "minimum": 0, "maximum": 1}}}, True),
    ({"a": 1.5}, {"type": "object", "properties": {"a": {"type": "number", "maximum": 1}}}, False),
    ({"a": "z"}, {"type": "object", "properties": {"a": {"enum": ["x", "y"]}}}, False),
    ({"l": [{"k": 1}, {"k": "no"}]},
     {"type": "object", "properties": {"l": {"type": "array", "items": {"type": "object", "properties": {
         "k": {"type": "integer"}}}}}}, False),
    ([1], {"type": "object"}, False),
    ({"a": float("nan")}, {"type": "object", "properties": {"a": {"type": "number"}}}, False),
])
def test_the_schema_reader(data, schema, ok):
    assert cascade.valid(data, schema)[0] is ok


# ---- the callers -------------------------------------------------------------------------------------------

FUTURE = "2099-01-01"


@pytest.fixture
def goal(s, tmp_path):
    root = tmp_path / "goalroot"
    root.mkdir()
    goals.add("g", "Write notes", "because", indicators=[{"name": "proven steps", "measure": {"type": "proven_steps"}}],
              charter={"actions": ["write_note"], "write_roots": [str(root)], "channels": [], "money": 0,
                       "expires": FUTURE}, status="enabled")
    return goals.get("g"), root


def _proposal(root, **over):
    return {"action": "write_note", "title": "Write a note", "instructions": "write it", "workdir": str(root),
            "evidence": [{"type": "file_exists", "path": str(root / "n.md")}], **over}


def test_autowork_pick_stays_local_when_the_local_pick_passes_the_charter(s, goal, monkeypatch):
    g, root = goal
    monkeypatch.setattr(cascade, "local_available", lambda st: (True, ""))
    monkeypatch.setattr(ollama, "chat_json", lambda messages, schema=None, **k: {**_proposal(root), "confidence": 0.9})
    monkeypatch.setattr(claude_cli, "ask", lambda *a, **k: pytest.fail("no Claude call was needed"))
    prop = autowork.pick_step(g, s, now=None)
    assert prop["action"] == "write_note" and "confidence" not in prop
    assert cascade.counts()["avoided"] == 1


def test_autowork_pick_asks_a_stronger_model_when_the_local_pick_is_outside_the_charter(s, goal, monkeypatch, tmp_path):
    g, root = goal
    monkeypatch.setattr(cascade, "local_available", lambda st: (True, ""))
    out = tmp_path / "elsewhere"
    monkeypatch.setattr(ollama, "chat_json", lambda messages, schema=None, **k: {
        **_proposal(root, workdir=str(out)), "confidence": 0.99})
    models = []

    def haiku(prompt, model=None, **k):
        models.append(model)
        return {"json": {**_proposal(root), "confidence": 0.9}, "text": ""}
    monkeypatch.setattr(claude_cli, "ask", haiku)
    prop = autowork.pick_step(g, s)
    assert prop["workdir"] == str(root) and models == ["haiku"]


def test_autowork_pick_keeps_a_sonnet_proposal_the_charter_refuses_for_the_loop_to_record(s, goal, monkeypatch,
                                                                                         tmp_path):
    g, root = goal
    monkeypatch.setattr(cascade, "local_available", lambda st: (False, "off"))
    bad = {**_proposal(root, workdir=str(tmp_path / "elsewhere")), "confidence": 0.99}
    monkeypatch.setattr(claude_cli, "ask", lambda prompt, model=None, **k: {"json": bad, "text": ""})
    prop = autowork.pick_step(g, s)
    assert "refused" not in prop and "error" not in prop  # run_once turns it into charter_refused itself
    assert autowork.check_proposal(g, prop, s)[0] is False


def test_autowork_pick_with_the_cascade_off_takes_the_old_direct_call(s, goal, monkeypatch):
    g, root = goal
    s.cascade_enabled = False
    s.save()
    seen = []

    def old(prompt, model=None, system=None, schema=None, purpose=None, effort=None, **k):
        seen.append((model, purpose, "confidence" in schema["properties"]))
        return {"json": _proposal(root), "text": ""}
    monkeypatch.setattr(claude_cli, "ask", old)
    monkeypatch.setattr(ollama, "chat_json", lambda *a, **k: pytest.fail("the local model must not be asked"))
    prop = autowork.pick_step(g, Settings.load())
    assert prop["action"] == "write_note" and seen == [("sonnet", "autowork-pick", False)]


def test_autowork_pick_with_an_injected_asker_keeps_the_old_path(s, goal, monkeypatch):
    g, root = goal
    monkeypatch.setattr(ollama, "chat_json", lambda *a, **k: pytest.fail("the local model must not be asked"))
    prop = autowork.pick_step(g, s, asker=lambda *a, **k: {"json": _proposal(root), "text": ""})
    assert prop["action"] == "write_note"


def test_the_digest_uses_haiku_first_and_never_the_local_model(s, monkeypatch):
    monkeypatch.setattr(cascade, "local_available", lambda st: pytest.fail("the digest skips the local model"))
    models = []

    def ask(prompt, model=None, **k):
        models.append(model)
        return {"json": {"summary": "A quiet day of work.", "conversations": [], "facts": [], "outcomes": [],
                         "confidence": 0.9}, "text": ""}
    monkeypatch.setattr(claude_cli, "ask", ask)
    import datetime as dt
    out = digest.distill(dt.date(2026, 11, 20), [], s)
    assert out["summary"] == "A quiet day of work." and "confidence" not in out and models == ["haiku"]


def test_the_digest_asks_sonnet_when_haiku_is_unsure(s, monkeypatch):
    models = []

    def ask(prompt, model=None, **k):
        models.append(model)
        conf = 0.2 if model == "haiku" else 0.9
        return {"json": {"summary": "A quiet day of work.", "conversations": [], "facts": [], "outcomes": [],
                         "confidence": conf}, "text": ""}
    monkeypatch.setattr(claude_cli, "ask", ask)
    import datetime as dt
    digest.distill(dt.date(2026, 11, 20), [], s)
    assert models == ["haiku", "sonnet"]


def test_the_digest_with_the_cascade_off_is_one_sonnet_call(s, monkeypatch):
    s.cascade_enabled = False
    s.save()
    models = []
    monkeypatch.setattr(claude_cli, "ask", lambda prompt, model=None, **k: models.append(model) or {"json": {}})
    import datetime as dt
    digest.distill(dt.date(2026, 11, 20), [], Settings.load())
    assert models == ["sonnet"]


def test_the_agent_report_check_goes_local_first_in_background_work(bg, monkeypatch):
    monkeypatch.setattr(cascade, "local_available", lambda st: (True, ""))
    monkeypatch.setattr(ollama, "chat_json", lambda messages, schema=None, **k: {
        "complete": False, "missing": "no dates", "confidence": 0.95})
    monkeypatch.setattr(claude_cli, "ask", lambda *a, **k: pytest.fail("no Claude call was needed"))
    assert agents.verify("find dates", "I looked", settings=bg, strict=True) == (False, "no dates")


def test_the_agent_report_check_in_the_owners_chat_never_touches_the_cascade(s, monkeypatch):
    monkeypatch.setattr(ollama, "chat_json", lambda *a, **k: pytest.fail("not for chat"))
    monkeypatch.setattr(claude_cli, "ask", lambda *a, **k: {"json": {"complete": True, "missing": ""}})
    assert agents.verify("t", "r", settings=s)[0] is True
    assert events.read("cascade") == []
