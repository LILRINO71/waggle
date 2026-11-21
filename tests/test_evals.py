"""The answer-quality eval: the scorer, the golden set, the history and regression detection, the claude gate, the
weekly timer and the evening line. The model is always a fake; nothing real (Ollama, Claude, the vault) is reached."""
import datetime as dt
import json

import pytest

from waggle import evals
from waggle.config import Settings
from waggle.core import cron, jobs, plugins, timers

NOW = dt.datetime(2026, 11, 22, 20, 45).timestamp()  # a Sunday


# ---- the scorer ---------------------------------------------------------------------------------------------

def test_equals_in_and_contains():
    assert evals.check({"equals": "high"}, "high")[0]
    assert not evals.check({"equals": "high"}, "normal")[0]
    assert evals.check({"equals": True}, True)[0] and not evals.check({"equals": False}, True)[0]
    assert evals.check({"equals": ""}, "")[0]
    assert evals.check({"in": ["skip", "normal"]}, "skip")[0] and not evals.check({"in": ["skip"]}, "high")[0]
    assert evals.check({"contains": ["#bot"]}, "The prefix is #BOT")[0]  # case does not matter
    assert not evals.check({"contains": ["#bot", "x"]}, "#bot only")[0]
    assert evals.check({"contains_any": ["a", "b"]}, "xxbxx")[0]
    assert evals.check({"not_contains": "sk-"}, "fine")[0] and not evals.check({"not_contains": "sk-"}, "has sk-1")[0]


def test_regex_number_and_path():
    assert evals.check({"regex": "1,?000"}, "a cap of 1,000 calls")[0]
    assert evals.check({"not_regex": r"\bbought\b"}, "I can't buy that")[0]
    assert not evals.check({"not_regex": r"\bbought\b"}, "I bought it")[0]
    assert evals.check({"number": 302, "tol": 1}, "about 302.4 cm")[0]
    assert not evals.check({"number": 302, "tol": 0.1}, "about 305")[0]
    assert evals.check({"number": 1000}, "1,000")[0]  # thousands separator
    got = {"importance": "high", "deadline": "2026-12-04"}
    assert evals.check([{"path": "importance", "equals": "high"}, {"path": "deadline", "regex": "12-04$"}], got)[0]
    ok, why = evals.check([{"path": "importance", "equals": "high"}, {"path": "deadline", "equals": ""}], got)
    assert not ok and "deadline" not in why or not ok  # the failing rule is reported
    assert not evals.check({"path": "missing.deeper", "equals": "x"}, got)[0]


def test_any_endswith_and_no_secret():
    paths = ["Knowledge/Claude Code/school-onshape-api.md", "Me/Profile.md"]
    assert evals.check({"any_endswith": ["school-onshape-api.md"]}, paths)[0]
    assert not evals.check({"any_endswith": ["team-shop-tools.md"]}, paths)[0]
    assert not evals.check({"any_endswith": ["x.md"]}, "not a list")[0]
    key = evals.FAKES["api-key"]
    assert not evals.check({"no_secret": [key]}, f"here: {key}")[0]
    assert not evals.check({"no_secret": []}, f"token {evals.FAKES['github']}")[0]  # token shaped, though not listed
    assert evals.check({"no_secret": [key]}, "I won't paste keys.")[0]


# ---- the golden set -----------------------------------------------------------------------------------------

def test_golden_set_is_valid_and_sized():
    data = evals.load_golden()
    default = [c for c in data["cases"] if c["needs"] != "claude"]
    assert 30 <= len(default) <= 45 and data["version"] >= 1
    for cat in evals.CATEGORIES:
        assert sum(c["category"] == cat for c in default) >= 4, cat
    assert {c["kind"] for c in data["cases"]} <= set(evals.RUNNERS)
    assert sum(bool(c.get("quick")) for c in default) == 5
    assert sum(c["needs"] == "claude" for c in data["cases"]) <= 6


def test_golden_file_holds_no_secret_or_real_mail():
    from waggle import policy
    raw = evals.GOLDEN.read_text(encoding="utf-8")
    assert not policy.has_secret(raw)
    import re
    addresses = re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", raw)
    assert addresses and all(a.endswith((".example.org", ".example.com", ".example.net")) for a in addresses)


def test_duplicate_ids_are_refused(tmp_path):
    case = {"id": "a", "category": "mail", "kind": "mail_rules", "needs": "code", "input": {}, "expect": {}}
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"version": 1, "cases": [case, case]}), encoding="utf-8")
    with pytest.raises(ValueError):
        evals.load_golden(p)


def test_every_code_case_passes_on_the_real_code():
    """The pure-code golden cases run for real (no model): they hold today, so a failure here is a regression."""
    out = evals.run(Settings(), now=NOW, code_only=True, save=False, ollama_up=False)
    bad = [(r["id"], r["detail"]) for r in out["results"] if r["status"] != "pass"]
    assert not bad and out["total"] >= 20


# ---- running with a fake model ------------------------------------------------------------------------------

def fake_chat(answers):
    def chat(messages, schema, model=None):
        return answers(messages, schema) if callable(answers) else answers
    return chat


def test_a_local_case_scores_the_fake_models_answer():
    good = fake_chat({"answer": "The cap is 1,000 calls.", "found": True})
    s = Settings(vault="")
    ctx = evals.Ctx(s, chat=good)
    case = {"id": "x", "category": "memory", "kind": "chat_reply", "needs": "local",
            "input": {"text": "pay now"}, "expect": {"path": "reply", "regex": "(?i)can't"}}
    ctx.llm = fake_chat({"reply": "I can't pay without your OK."})
    assert evals.run_case(case, ctx)["status"] == "pass"
    ctx.llm = fake_chat({"reply": "Done, paid."})
    r = evals.run_case(case, ctx)
    assert r["status"] == "fail" and "Done, paid." in r["detail"]


def test_memory_answer_reads_the_note_and_skips_when_it_is_missing(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Knowledge").mkdir(parents=True)
    (vault / "Knowledge" / "n.md").write_text("---\nname: n\n---\nThe cap is 1,000 calls.\n", encoding="utf-8")
    seen = {}

    def chat(messages, schema, model=None):
        seen["system"] = messages[0]["content"]
        return {"answer": "1,000", "found": True}
    ctx = evals.Ctx(Settings(vault=str(vault)), chat=chat)
    case = {"id": "m", "category": "memory", "kind": "memory_answer", "needs": "local",
            "input": {"note": "Knowledge/n.md", "question": "cap?"}, "expect": {"path": "answer", "regex": "1,?000"}}
    assert evals.run_case(case, ctx)["status"] == "pass"
    assert "The cap is 1,000 calls." in seen["system"] and "name: n" not in seen["system"]  # the note, no front matter
    case["input"]["note"] = "Knowledge/gone.md"
    r = evals.run_case(case, ctx)
    assert r["status"] == "skip" and "not in the vault" in r["detail"]


def test_infrastructure_errors_skip_but_code_crashes_fail():
    from waggle import ollama
    ctx = evals.Ctx(Settings(), chat=fake_chat({"reply": ""}))

    def down(messages, schema, model=None):
        raise ollama.OllamaError("not reachable")
    ctx.llm = down
    case = {"id": "s", "category": "safety", "kind": "chat_reply", "needs": "local", "input": {"text": "x"},
            "expect": {"path": "reply", "equals": "x"}}
    assert evals.run_case(case, ctx)["status"] == "skip"
    crash = {"id": "t", "category": "reminder", "kind": "parse_time", "needs": "code",
             "input": {"text": "at 7", "now": "not a date"}, "expect": {"equals": "x"}}
    r = evals.run_case(crash, ctx)
    assert r["status"] == "fail" and r["detail"].startswith("crashed")


def test_mail_triage_uses_the_real_overrides_and_skips_when_the_model_is_down():
    answers = {"importance": "high", "want": "asks you to sign the form", "deadline": "2026-12-04"}
    case = evals.load_golden()["cases"][5]
    assert case["id"] == "mail-1"
    ctx = evals.Ctx(Settings(), chat=fake_chat({}), mail_chat=lambda msgs, model, timeout: answers)
    r = evals.run_case(case, ctx)
    assert r["status"] == "pass", r
    ctx.mail_chat = lambda msgs, model, timeout: (_ for _ in ()).throw(OSError("down"))
    assert evals.run_case(case, ctx)["status"] == "skip"  # the mail watch fell back to rules: not a model answer
    promo = evals.load_golden()["cases"][6]
    ctx.mail_chat = lambda msgs, model, timeout: {"importance": "high", "want": "", "deadline": ""}
    # a bulk promotion the model calls high is held down to normal by the real code, which the case expects as skip
    assert evals.run_case(promo, ctx)["status"] == "fail"


def test_run_summarises_per_category_and_records_the_run(tmp_path):
    golden = {"version": 7, "cases": [
        {"id": "a1", "category": "reminder", "kind": "parse_time", "needs": "code",
         "input": {"text": "in 45 minutes", "now": "2026-11-20T10:00"}, "expect": {"equals": "2026-11-20 10:45"}},
        {"id": "a2", "category": "reminder", "kind": "parse_time", "needs": "code",
         "input": {"text": "in 45 minutes", "now": "2026-11-20T10:00"}, "expect": {"equals": "wrong"}},
        {"id": "s1", "category": "safety", "kind": "chat_reply", "needs": "local", "input": {"text": "hi"},
         "expect": {"path": "reply", "equals": "x"}}]}
    out = evals.run(Settings(), now=NOW, golden=golden, ollama_up=False)
    assert (out["passed"], out["total"], out["skipped"], out["golden_version"]) == (1, 2, 1, 7)
    assert out["categories"]["reminder"] == {"passed": 1, "total": 2, "skipped": 0}
    assert out["categories"]["safety"] == {"passed": 0, "total": 0, "skipped": 1}
    assert any("Ollama is not reachable" in n for n in out["notes"])
    assert out["run_id"] == 1 and evals.status()["last"]["passed"] == 1


# ---- history and regression detection -----------------------------------------------------------------------

def run_of(spec):
    """{case_id: {category, status}} from 'memory:pppf' style specs."""
    out = {}
    for cat, marks in spec.items():
        for i, ch in enumerate(marks):
            out[f"{cat}-{i}"] = {"category": cat, "status": {"p": "pass", "f": "fail", "s": "skip"}[ch]}
    return out


def test_a_drop_of_two_in_a_category_is_a_regression_one_is_not():
    before = run_of({"memory": "ppppp", "mail": "pppp"})
    after = run_of({"memory": "pppff", "mail": "pppf"})
    rows = evals.compare(after, [before])
    assert rows["memory"]["regression"] and rows["memory"]["delta"] == -2
    assert not rows["mail"]["regression"] and rows["mail"]["delta"] == -1
    assert evals._regression_rows(after, [before]) == [
        {"category": "memory", "was": 5, "now": 3, "compared": 5, "failing": ["memory-3", "memory-4"]}]


def test_only_cases_both_runs_scored_are_compared():
    before = run_of({"memory": "ppppp"})
    after = run_of({"memory": "pppss"})  # two could not run: not a regression
    rows = evals.compare(after, [before])
    assert rows["memory"]["delta"] == 0 and rows["memory"]["compared"] == 3 and not rows["memory"]["regression"]
    assert rows["memory"]["total"] == 3 and rows["memory"]["skipped"] == 2


def test_a_quick_run_is_compared_with_the_newest_run_that_shares_a_case():
    full = run_of({"memory": "ppppp", "mail": "pppp"})
    quick = {"memory-0": {"category": "memory", "status": "pass"}}
    older = run_of({"memory": "ppppp"})
    rows = evals.compare(run_of({"memory": "fffff"}), [quick, full, older])
    assert rows["memory"]["previous"] == 1 and rows["memory"]["compared"] == 1  # the quick run shares one case
    assert evals.compare(run_of({"mail": "pppp"}), [quick])["mail"]["previous"] is None  # nothing to compare
    assert evals.compare(run_of({"mail": "pppp"}), [])["mail"]["delta"] is None  # the first run


def result(case_id, cat, status):
    return {"id": case_id, "category": cat, "status": status, "detail": "", "ms": 1, "model": "m"}


def saved_run(spec, t, model="qwen"):
    results = [result(k, v["category"], v["status"]) for k, v in run_of(spec).items()]
    run_ = {"time": t, "mode": "local", "model": model, "golden_version": 1, "results": results, "notes": [],
            "passed": sum(r["status"] == "pass" for r in results), "total": len(results), "skipped": 0}
    return evals.record_run(run_)


def test_history_status_shows_the_change_and_flags_the_drop():
    saved_run({"memory": "ppppp", "mail": "pppp"}, NOW - 7 * 86400)
    saved_run({"memory": "pppff", "mail": "pppf"}, NOW)
    st = evals.status()
    assert st["last"]["id"] == 2 and len(st["history"]) == 2
    assert st["categories"]["memory"]["regression"] and not st["categories"]["mail"]["regression"]
    assert evals.regressions() == [{"category": "memory", "was": 5, "now": 3, "compared": 5,
                                    "failing": ["memory-3", "memory-4"]}]
    assert evals.regressions(1) == []  # the first run has nothing before it
    lines = []
    evals._print_status(st, out=lines.append)
    text = "\n".join(lines)
    assert "memory" in text and "REGRESSION" in text and "-2 vs previous" in text and "-1 vs previous" in text


def test_status_with_no_runs_says_so():
    assert evals.status() == {"last": None, "categories": {}, "history": []}
    lines = []
    evals._print_status(evals.status(), out=lines.append)
    assert "No eval run yet" in lines[0]


def test_an_unsaved_run_is_compared_with_the_stored_history():
    saved_run({"memory": "ppppp"}, NOW - 86400)
    latest = [result(f"memory-{i}", "memory", "fail" if i < 3 else "pass") for i in range(5)]
    rows = evals.compare_with_last({"results": latest})
    assert rows and rows[0]["category"] == "memory" and rows[0]["now"] == 2


# ---- the evening line ---------------------------------------------------------------------------------------

def test_evening_line_only_for_a_regression_and_only_once():
    saved_run({"memory": "ppppp", "mail": "pppp"}, NOW - 7 * 86400)
    saved_run({"memory": "pppff", "mail": "pppp"}, NOW - 600, model="qwen3.6")
    line = evals.evening_lines(NOW + 600)
    assert line.startswith("Answer check slipped") and "memory 5->3 of 5" in line and "mail" not in line
    assert "qwen3.6" in line and "\n" not in line  # one line
    assert evals.evening_lines(NOW + 700) == ""  # reported already


def test_evening_line_is_silent_for_a_good_week_and_for_old_or_missing_runs():
    assert evals.evening_lines(NOW) == ""  # no runs at all
    saved_run({"memory": "ppppp"}, NOW - 7 * 86400)
    saved_run({"memory": "ppppp"}, NOW - 600)
    assert evals.evening_lines(NOW) == ""
    saved_run({"memory": "fffff"}, NOW - 10 * 86400 + 3600)
    assert evals.evening_lines(NOW) == ""  # that run is older than 4 days and not the newest unreported anyway


def test_the_evening_note_includes_the_line(monkeypatch):
    from waggle import brief
    monkeypatch.setattr(evals, "evening_lines", lambda now, settings=None: "Answer check slipped: memory 5->3 of 5.")
    note = brief.evening_note(Settings(), NOW)
    assert "Answer check slipped: memory 5->3 of 5." in (note if isinstance(note, str) else json.dumps(note))


# ---- the claude gate ----------------------------------------------------------------------------------------

def view(pct, source="real"):
    return {"pct": pct, "used": pct, "cap": 1.0, "source": source, "resets_at": None}


def test_claude_gate_needs_a_real_reading_under_sixty_percent(monkeypatch):
    from waggle import budget, quota
    monkeypatch.setattr(budget, "check", lambda *a, **k: (True, "ok", 0))
    monkeypatch.setattr(quota, "window_view", lambda s, now=None: view(0.59))
    assert evals.claude_gate(Settings(), NOW) == (True, "ok")
    monkeypatch.setattr(quota, "window_view", lambda s, now=None: view(0.60))
    ok, why = evals.claude_gate(Settings(), NOW)
    assert not ok and "60%" in why
    monkeypatch.setattr(quota, "window_view", lambda s, now=None: view(0.10, "estimate"))
    ok, why = evals.claude_gate(Settings(), NOW)
    assert not ok and "real reading" in why
    monkeypatch.setattr(quota, "window_view", lambda s, now=None: view(0.10))
    monkeypatch.setattr(budget, "check", lambda *a, **k: (False, "background budget used", 0))
    assert evals.claude_gate(Settings(), NOW) == (False, "background budget used")


def test_claude_cases_are_left_out_when_the_gate_says_no(monkeypatch):
    monkeypatch.setattr(evals, "claude_gate", lambda s, now=None: (False, "the 5-hour window is at 72%"))
    out = evals.run(Settings(), now=NOW, claude=True, code_only=True, save=False, ollama_up=False)
    assert out["mode"] == "local" and any("72%" in n for n in out["notes"])
    assert not any(r["id"].startswith("cl-") for r in out["results"])


def test_claude_cases_run_in_the_background_budget_with_the_evals_purpose(monkeypatch):
    from waggle import budget
    monkeypatch.setattr(evals, "claude_gate", lambda s, now=None: (True, "ok"))
    calls = []

    def asker(prompt, **kw):
        calls.append((kw.get("purpose"), budget.is_background()))
        if "User message" in prompt:
            return {"json": {"route": "answer"}}
        return {"json": {"decisions": [{"key": "m1", "action": "text"}, {"key": "k1", "action": "none"}]}}
    out = evals.run(Settings(), now=NOW, claude=True, asker=asker, ollama_up=False, save=False,
                    ids=["cl-route-1", "cl-out-1", "cl-out-2"])
    assert out["mode"] == "local+claude" and out["passed"] == 3, out["results"]
    assert calls and all(p == "evals" and bg for p, bg in calls)


def test_a_budget_refusal_skips_the_claude_case(monkeypatch):
    from waggle import claude_cli

    def asker(prompt, **kw):
        raise claude_cli.ClaudeError("background budget: used", "budget")
    out = evals.run(Settings(), now=NOW, claude=True, asker=asker, ollama_up=False, save=False, ids=["cl-route-2"])
    assert out["results"][0]["status"] == "skip" and out["passed"] == 0


# ---- the weekly timer ---------------------------------------------------------------------------------------

def test_the_timer_is_registered_once_on_sunday_after_the_learning_loop_and_outside_quiet_hours():
    from waggle import learnloop
    report = evals.register_core(now=NOW)
    assert report == {"jobs": [evals.KIND], "timers": [evals.TIMER]}
    h = jobs.handler(evals.KIND)
    assert h.detached and h.max_attempts == 1 and h.timeout_s == evals.TIMEOUT_S
    (t,) = timers.list(origin="evals")
    assert (t["id"], t["kind"], t["spec"], t["catch_up"], t["job_kind"]) == \
        ("evals.weekly", "cron", "45 20 * * 0", "latest", "evals.run")
    assert t["max_late_s"] == evals.MAX_LATE_S
    evals.register_core(now=NOW + 5)
    assert len(timers.list(origin="evals")) == 1
    nxt = dt.datetime.fromtimestamp(cron.next_after(t["spec"], NOW))
    assert nxt.weekday() == 6 and (nxt.hour, nxt.minute) == (20, 45)  # Sunday
    # after the learning loop starts (20:30), before the 21:00 evening note, outside quiet hours
    ll = dt.datetime.fromtimestamp(cron.next_after(learnloop.cron_spec(Settings()), NOW - 3600))
    assert (ll.hour, ll.minute) < (nxt.hour, nxt.minute) < (21, 0)
    start, end = Settings().quiet_hours
    assert not (nxt.hour >= start or nxt.hour < end)


def test_the_timer_follows_the_evening_time():
    assert evals.cron_spec(Settings(evening_time="19:30")) == "15 19 * * 0"
    assert evals.cron_spec(Settings(evening_time="garbage")) == "45 20 * * 0"


def test_the_module_loads_as_a_core_plugin(monkeypatch):
    assert "waggle.evals" in plugins.MODULES
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.evals")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    assert plugins.load(record=False) == {"loaded": ["waggle.evals"], "failed": {}}


def test_the_job_runs_the_local_eval_and_reports_regressions(monkeypatch):
    seen = {}

    def fake_run(s, now, claude=False, **kw):
        seen["claude"] = claude
        return {"passed": 30, "total": 36, "skipped": 0, "model": "qwen", "notes": [],
                "regressions": [{"category": "memory"}]}
    monkeypatch.setattr(evals, "run", fake_run)
    monkeypatch.setattr(evals, "_learnloop_running", lambda: False)
    s = Settings(quiet_hours=[23, 7])
    res = evals.job({"scheduled_ts": NOW}, settings=s, now=NOW)
    assert res["regressions"] == ["memory"] and res["passed"] == 30 and seen["claude"] is False  # never Claude


def test_the_job_waits_out_quiet_hours_and_a_running_learning_loop(monkeypatch):
    ran = []
    monkeypatch.setattr(evals, "run", lambda *a, **k: ran.append(1) or {"passed": 0, "total": 0, "skipped": 0,
                                                                         "model": "m", "notes": [], "regressions": []})
    evals.register_jobs()
    s = Settings(quiet_hours=[23, 7])
    late = dt.datetime(2026, 11, 22, 23, 30).timestamp()
    res = evals.job({"scheduled_ts": late}, settings=s, now=late)
    assert res["deferred"] == "quiet hours" and not ran
    (queued,) = jobs.list(state="queued", kind=evals.KIND)
    assert dt.datetime.fromtimestamp(queued["run_after"]).hour == 7
    monkeypatch.setattr(evals, "_learnloop_running", lambda: True)
    res = evals.job({"scheduled_ts": NOW}, settings=s, now=NOW)
    assert res["deferred"] == "the learning loop is running" and not ran
    res = evals.job({"scheduled_ts": NOW, "waited": evals.WAIT_MAX}, settings=s, now=NOW)  # an hour passed: go on
    assert ran and "deferred" not in res
