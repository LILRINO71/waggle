"""Specialist agents, scheduled routines, model catalogue and engine fallback."""
import datetime as dt
import json

import pytest

from waggle import agent, agents, engines, models, notify, notifycheck, routines, traces, vault
from waggle.config import Settings


@pytest.fixture(autouse=True)
def _local_says_notify(monkeypatch):
    """A routine result asks a local yes/no before it messages (waggle/notifycheck.py); these tests are about
    delivery, so the answer is yes. tests/test_gates.py covers the no and the failure."""
    monkeypatch.setattr(notifycheck, "_ask", lambda m, s, t: {"decision": "yes", "reason": "test"})


def _ok_runner(text):
    return lambda cmd, prompt, env, cwd, timeout: [json.dumps({"type": "result", "is_error": False, "num_turns": 1,
                                                               "result": text, "usage": {}})]


# ---- agents -------------------------------------------------------------------------------------

def test_choose_agent():
    assert agents.choose("research the best filament for gears") == "researcher"
    assert agents.choose("fix the failing test in my python repo") == "coder"
    assert agents.choose("sort my downloads folder") == "general"


def test_agent_instructions_and_isolated_memory(settings):
    vault.write("Agents/coder/Instructions", "Always use type hints.")
    assert agents.note("coder", "The user's repos use pytest, run it with -q")
    assert not agents.note("coder", "The user's repos use pytest, run it with -q")  # no duplicates
    text = agents.instructions("coder", settings)
    assert "type hints" in text and "pytest" in text and "coding agent" in text
    assert "pytest" not in agents.instructions("tutor", settings)  # memory is per agent


def test_agent_run_is_traced(settings):
    res = agents.run("coder", "add a docstring", settings=settings, runner_fn=_ok_runner("Added it; tests pass."))
    assert res.ok and res.model == "claude-sonnet-5-5"
    row = traces.last()
    assert row["agent"] == "coder" and row["route"] == "agent:coder"
    assert agents.status()["coder"]["runs"] == 1


def test_delegate_depth_limit(monkeypatch):
    monkeypatch.setenv("WAGGLE_DEPTH", str(agents.MAX_DEPTH))
    assert "depth limit" in agents._t_delegate({"agent": "coder", "task": "x"})


def test_verify_handles_failure(settings):
    def boom(*a, **k):
        raise RuntimeError("down")
    ok, why = agents.verify("t", "r", asker=boom, settings=settings)
    assert ok and "not verified" in why


# ---- routines -----------------------------------------------------------------------------------

def _ts(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi).timestamp()


@pytest.mark.parametrize("sched,after,want", [
    ("daily 21:30", (2026, 11, 18, 20, 0), (2026, 11, 18, 21, 30)),
    ("daily 21:30", (2026, 11, 18, 22, 0), (2026, 11, 19, 21, 30)),
    ("weekly mon 07:00", (2026, 11, 18, 12, 0), (2026, 11, 23, 7, 0)),  # 2026-11-18 is a Wednesday
    ("weekdays 08:00", (2026, 11, 21, 9, 0), (2026, 11, 23, 8, 0)),     # Saturday -> Monday
    ("0 19 * * 0", (2026, 11, 18, 12, 0), (2026, 11, 22, 19, 0)),        # cron Sunday
    ("*/15 * * * *", (2026, 11, 18, 12, 1), (2026, 11, 18, 12, 15)),
])
def test_next_run(sched, after, want):
    assert routines.next_run(sched, _ts(*after)) == _ts(*want)


def test_every_and_bad_schedule():
    assert routines.next_run("every 2h", 1000.0) == 1000.0 + 7200
    with pytest.raises(ValueError):
        routines.add("x", "whenever you feel like it", "t")


def test_routine_lifecycle(settings, monkeypatch):
    monkeypatch.setattr(routines.quota, "can_escalate", lambda m, s: (True, ""))
    rid = routines.add("Robot Batteries", "daily 07:00", "remind me to charge the robot batteries", "general",
                       "telegram")
    assert routines.listing()[0]["name"] == "robot-batteries"
    data = routines._load()
    data[rid]["next_run"] = 0
    routines._save(data)
    sent = notify.PHONE_LOG
    ran = []
    out = routines.tick(settings, run_fn=lambda a, t, **k: ran.append(a) or agent.AgentResult(text="Charge the batteries!",
                                                                                               ok=True),
                        verify_fn=lambda *a, **k: (True, ""))
    assert out == (rid, "ok") and ran == ["general"]
    assert any("Charge the batteries" in m for m in sent)
    assert routines._load()[rid]["next_run"] > 0 and routines._load()[rid]["runs"] == 1
    assert routines.tick(settings) is None  # nothing due now
    assert routines.set_enabled("robot-batteries", False) and not routines._load()[rid]["enabled"]
    assert routines.remove(rid) and routines.listing() == []


def test_quiet_monitor_and_vault_delivery(settings, monkeypatch):
    monkeypatch.setattr(routines.quota, "can_escalate", lambda m, s: (True, ""))
    rid = routines.add("repo-watch", "every 1h", "check repos", "monitor", "both")
    before = len(notify.PHONE_LOG)
    status = routines.run_one(rid, settings, run_fn=lambda *a, **k: agent.AgentResult(text="NOTHING_NEW", ok=True))
    assert status == "quiet" and len(notify.PHONE_LOG) == before
    routines.run_one(rid, settings, run_fn=lambda *a, **k: agent.AgentResult(text="waggle has 3 unpushed commits",
                                                                             ok=True),
                     verify_fn=lambda *a, **k: (False, "didn't check ftc-platform"))
    assert "unpushed" in vault.read("Routines/repo-watch")
    assert "may be incomplete" in notify.PHONE_LOG[-1]


def test_interrupted_run_is_recovered(settings, monkeypatch):
    monkeypatch.setattr(routines.quota, "can_escalate", lambda m, s: (True, ""))
    rid = routines.add("r", "daily 09:00", "t")
    data = routines._load()
    import time
    now = time.time()
    data[rid].update(running_since=now - 7200, next_run=now + 10 ** 6)
    routines._save(data)
    out = routines.tick(settings, run_fn=lambda *a, **k: agent.AgentResult(text="done", ok=True),
                        verify_fn=lambda *a, **k: (True, ""), now=now)
    assert out == (rid, "ok")


def test_quota_skip(settings, monkeypatch):
    monkeypatch.setattr(routines.quota, "can_escalate", lambda m, s: (False, "budget used"))
    rid = routines.add("r2", "daily 09:00", "t")
    assert routines.run_one(rid, settings, run_fn=lambda *a, **k: 1 / 0) == "skipped"
    assert "budget" in routines._load()[rid]["last_status"]


def test_defaults_installed_once(settings):
    routines.ensure_defaults()
    routines.ensure_defaults()
    names = [r["name"] for r in routines.listing()]
    assert names.count("nightly-project-review") == 1 and "weekly-review" in names


# ---- models + engines ---------------------------------------------------------------------------

CAT = {"hardware": {"vram_gb": 12.0}, "servers": [], "cloud": [],
       "local": [{"name": "waggle-4b", "size_gb": 2.5, "params_b": 4, "fits_gpu": True, "tools": True, "vision": False,
                  "embedding": False},
                 {"name": "gemma4", "size_gb": 9.6, "params_b": 8, "fits_gpu": True, "tools": True, "vision": True,
                  "embedding": False},
                 {"name": "big-coder", "size_gb": 18.6, "params_b": 30, "fits_gpu": False, "tools": True,
                  "vision": False, "embedding": False},
                 {"name": "nomic-embed-text", "size_gb": 0.3, "params_b": 0.1, "fits_gpu": True, "tools": False,
                  "vision": False, "embedding": True}]}


def test_model_pick(settings):
    assert models.pick("vision", CAT, settings)[:2] == ("ollama", "gemma4")
    assert models.pick("embed", CAT, settings)[:2] == ("ollama", "nomic-embed-text")
    assert models.pick("offline", CAT, settings)[:2] == ("ollama", "gemma4")  # biggest that fits, not big-coder
    assert models.pick("reasoning", CAT, settings)[:2] == ("claude", "sonnet")
    assert any("big-coder" in t for t in models.recommendations(CAT, settings))
    with pytest.raises(ValueError):
        models.pick("dance", CAT, settings)


class _Eng(engines.Engine):
    def __init__(self, name, reply=None, fail=False):
        super().__init__(name)
        self.reply, self.fail = reply, fail

    def models(self, timeout=5):
        return ["m"]

    def chat(self, messages, model, timeout=120, **opts):
        if self.fail:
            raise ConnectionError("down")
        return self.reply


def test_engine_fallback_chain(settings):
    engs = {"claude": _Eng("claude", fail=True), "ollama": _Eng("ollama", reply="pong")}
    text, eng, model = engines.chat([{"role": "user", "content": "ping"}], chain=[("claude", "haiku"), ("ollama", "x")],
                                    engines=engs, settings=settings)
    assert (text, eng) == ("pong", "ollama")
    with pytest.raises(RuntimeError, match="all engines failed"):
        engines.chat([], chain=[("claude", "haiku")], engines=engs, settings=settings)


def test_engine_specs(settings):
    e = engines.make({"name": "lm", "kind": "openai", "url": "http://127.0.0.1:1234/v1/"})
    assert e.url == "http://127.0.0.1:1234/v1" and e.kind == "openai"
    assert set(engines.configured(settings)) >= {"claude", "ollama"}
    with pytest.raises(ValueError):
        engines.make({"kind": "quantum"})


def test_learned_cap_never_locks_waggle_out(settings, monkeypatch):
    from waggle import quota
    settings.window_units_estimate = 10.0
    settings.save()
    quota._save_state({"samples": [10.0]})  # calibrated
    s = Settings.load()
    cap = quota.learned_cap(s, 20.0)
    assert 20.0 / cap < s.stop_at  # after raising the cap, usage sits below the stop line
    ok, why = quota.can_escalate("haiku", s, used=20.0)
    assert ok, why


def test_scheduling_goes_to_planner():
    assert agents.choose("Every weekday at 6:45am, remind me to pack my robot batteries") == "planner"
    assert agents.choose("design a better intake for our FTC robot") == "robotics"
