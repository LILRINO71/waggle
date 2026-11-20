"""The Hermes plugin's engine path (off by default): a CLI turn through the engine, recorded as a test turn."""
import importlib.util
import json
from pathlib import Path

import pytest
from engine_fakes import FakeClaude

from waggle import chat_engine, traces
from waggle.chat_session import ChatSession, SessionPool
from waggle.core import store


@pytest.fixture(autouse=True)
def _clean():
    yield
    chat_engine.set_pool(None)
    store.close_all()


def _plugin():
    path = Path(__file__).parents[1] / "hermes_plugin" / "waggle" / "__init__.py"
    spec = importlib.util.spec_from_file_location("wp_engine", path)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    return p


def test_engine_path_answers_a_cli_turn_and_marks_it_as_test(settings, monkeypatch):
    settings.engine_enabled = True
    settings.edit_interval_s = 0.05
    settings.save()
    p = _plugin()
    fake = FakeClaude()
    chat_engine.set_pool(SessionPool(factory=lambda chat: ChatSession(chat, settings=settings, popen=fake,
                                                                      claude="claude"), settings=settings))
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    monkeypatch.setattr(p, "_start_reaper", lambda: None)
    learned = []
    monkeypatch.setattr(p.learn, "learn_async", lambda *a: learned.append(a))
    out = p.on_pre_llm_call(session_id="s1", user_message="hello there", platform="cli")
    assert out == {"action": "block", "message": "Hello from Claude."}
    row = traces.all_traces(include_non_user=True)[0]
    assert row["origin"] == "test" and row["turn_outcome"] == "ok" and row["answered_by"] == "claude-haiku-5-5"
    assert row["delivery"] == "block" and json.loads(row["extra"])["voice"] is False
    assert traces.all_traces() == [] and learned == []  # nothing learns from a CLI turn


def test_legacy_path_stays_the_default_and_traces_carry_the_origin(settings, monkeypatch):
    assert settings.engine_enabled is False
    p = _plugin()
    p._turn["s2"] = {"t0": 0, "platform": "cli"}
    p._trace("s2", "dev prompt", "reply")
    assert traces.all_traces(include_non_user=True)[0]["origin"] == "test" and traces.all_traces() == []
