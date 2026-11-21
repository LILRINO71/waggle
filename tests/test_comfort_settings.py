"""Comfort settings by chat (waggle/comfort.py, the MCP tool `settings`): the owner can change voice, speed, quiet
hours, the two daily notes and the mail watch by asking; nothing else, and never in a turn that is not the owner's.

Everything runs in the tests' temporary Waggle home (tests/conftest.py): no real ~/.waggle, timer or voice is touched.
"""
import datetime as dt
import json
import zipfile

import pytest

import waggle.config as config
import waggle.waggle_mcp as mcp
from waggle import comfort, events, guard, notify, policy, tick
from waggle.config import Settings
from waggle.core import timers, voiceline

OWNER = {"owner": True, "origin": "user", "text": "talk faster", "confident": True, "voice": False,
         "chat": "telegram:1", "turn_id": "t1"}
ASK, ALLOW = policy.ASK, policy.ALLOW


@pytest.fixture(autouse=True)
def kokoro(tmp_path, monkeypatch):
    """A voice file with a few English and non-English voices (the real list is read from this file)."""
    d = tmp_path / "kokoro"
    d.mkdir()
    with zipfile.ZipFile(d / "voices-v1.0.bin", "w") as z:
        for v in ("af_heart", "am_adam", "bf_emma", "bm_george", "bm_daniel", "jf_alpha", "zm_yunxi"):
            z.writestr(v + ".npy", b"x")
    monkeypatch.setenv("WAGGLE_KOKORO_MODELS", str(d))
    return d


@pytest.fixture
def owner_turn(monkeypatch):
    monkeypatch.setattr(guard, "current_turn", lambda env=None: dict(OWNER))


def call(**args):
    """The tool as the MCP server runs it: tools/call -> JSON text -> dict."""
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "settings", "arguments": args}})
    assert r["result"]["isError"] is False
    return json.loads(r["result"]["content"][0]["text"])


def cfg():
    return json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))


def changes():
    return events.read("settings_changed")


# ---- the allowlist -------------------------------------------------------------------------------------------

def test_the_tool_is_offered_and_lists_only_the_allowlist():
    assert "settings" in mcp.TOOLS
    spec = mcp.TOOLS["settings"][0]
    assert set(spec["parameters"]["properties"]["key"]["enum"]) == set(comfort.KEYS)
    assert not set(comfort.KEYS) & set(guard.CONFIG_KEYS)


@pytest.mark.parametrize("key", guard.CONFIG_KEYS)
def test_every_safety_key_is_refused(owner_turn, key):
    before = config.CONFIG_FILE.read_text(encoding="utf-8")
    out = call(action="set", key=key, value=1.0)
    assert out["ok"] is False and "safety setting" in out["error"]
    assert config.CONFIG_FILE.read_text(encoding="utf-8") == before
    assert not changes()


@pytest.mark.parametrize("key", ["ollama_url", "claude_bin", "chat_tools", "engine_enabled", "local_model", "ntfy_topic",
                                 "daily_text_cap", "agent_workdir", "", "../config", "voice_note_voice; stop_at"])
def test_unknown_keys_are_refused(owner_turn, key):
    before = config.CONFIG_FILE.read_text(encoding="utf-8")
    out = call(action="set", key=key, value="x")
    assert out["ok"] is False and "not a setting that can be changed" in out["error"]
    assert config.CONFIG_FILE.read_text(encoding="utf-8") == before


def test_an_alias_cannot_reach_a_safety_key():
    for alias, target in comfort.ALIASES.items():
        assert target in comfort.KEYS and target not in guard.CONFIG_KEYS, alias


def test_only_the_one_key_changes_and_the_safety_keys_hash_stays(owner_turn):
    cfg_before = cfg()
    cfg_before.update({"stop_at": 0.7, "self_emails": ["me@example.org"], "waggle_share": 0.25, "vault": "V:/vault"})
    config.CONFIG_FILE.write_text(json.dumps(cfg_before, indent=2), encoding="utf-8")
    h = guard.config_keys_hash()
    assert call(action="set", key="voice_note_speed", value=1.4)["ok"]
    after = cfg()
    assert after["voice_note_speed"] == 1.4
    assert {k: v for k, v in after.items() if k != "voice_note_speed"} == {k: v for k, v in cfg_before.items()
                                                                           if k != "voice_note_speed"}
    assert guard.config_keys_hash() == h


# ---- validation ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,ok,new", [
    ("bm_george", True, "bm_george"), ("BM_Daniel", True, "bm_daniel"), ("george", True, "bm_george"),
    ("emma", True, "bf_emma"), ("jf_alpha", False, None), ("zm_yunxi", False, None), ("nobody", False, None),
    ("", False, None), (7, False, None), ("../x", False, None)])
def test_voice_must_be_an_installed_english_voice(owner_turn, value, ok, new):
    out = call(action="set", key="voice_note_voice", value=value)
    assert out["ok"] is ok
    if ok:
        assert out["new"] == new and Settings.load().voice_note_voice == new
    else:
        assert "Installed:" in out["error"] and Settings.load().voice_note_voice == "af_heart"


def test_voice_list_falls_back_to_the_known_set_when_the_file_is_missing(kokoro):
    (kokoro / "voices-v1.0.bin").unlink()
    assert "bm_george" in comfort.installed_voices() and "jf_alpha" not in comfort.installed_voices()


@pytest.mark.parametrize("value,ok", [(1.3, True), ("1.5", True), ("1.5x", True), (0.7, True), (1.8, True), (0.69, False),
                                      (1.81, False), (0, False), (-1, False), ("fast", False), (True, False),
                                      (None, False), (float("nan"), False), (float("inf"), False), ([1.2], False)])
def test_speed_is_a_number_from_07_to_18(owner_turn, value, ok):
    out = call(action="set", key="voice_note_speed", value=value)
    assert out["ok"] is ok
    assert Settings.load().voice_note_speed == (round(float(str(value).rstrip("x")), 2) if ok else 1.1)


@pytest.mark.parametrize("value,ok,new", [
    ([22, 7], True, [22, 7]), ([0, 23], True, [0, 23]), (["22", "7"], True, [22, 7]), ("22-7", True, [22, 7]),
    ("22 to 7", True, [22, 7]), ([5, 5], True, [5, 5]),
    ([24, 7], False, None), ([-1, 7], False, None), ([22], False, None), ([1, 2, 3], False, None),
    ([22.5, 7], False, None), (["a", "b"], False, None), ([True, 7], False, None), (22, False, None),
    (None, False, None), ({"a": 1}, False, None)])
def test_quiet_hours_are_two_whole_hours(owner_turn, value, ok, new):
    out = call(action="set", key="quiet_hours", value=value)
    assert out["ok"] is ok
    assert Settings.load().quiet_hours == (new if ok else [0, 0])  # conftest starts at [0, 0]


@pytest.mark.parametrize("value,ok,new", [
    ("08:15", True, "08:15"), ("8:15", True, "08:15"), ("7", True, "07:00"), ("7pm", True, "19:00"),
    ("7:30 AM", True, "07:30"), ("12 am", True, "00:00"), ("12pm", True, "12:00"), ("23:59", True, "23:59"),
    ("24:00", False, None), ("7:60", False, None), ("13pm", False, None), ("noon", False, None), ("", False, None),
    ("30 7 * * *", False, None), (None, False, None)])
def test_the_daily_notes_take_a_clock_time(owner_turn, value, ok, new):
    timers.add("outreach.brief", "cron", "30 7 * * *", "outreach.brief")
    out = call(action="set", key="brief_time", value=value)
    assert out["ok"] is ok
    if ok:
        assert out["new"] == new


@pytest.mark.parametrize("value,ok,new", [(False, True, False), ("off", True, False), ("on", True, True), (True, True, True),
                                          ("maybe", False, True), (2, False, True)])
def test_mailwatch_is_on_or_off(owner_turn, value, ok, new):
    timers.add("knowledge.mailwatch", "cron", "*/5 6-22 * * *", "knowledge.mailwatch")
    out = call(action="set", key="mailwatch", value=value)
    assert out["ok"] is ok
    assert timers.get("knowledge.mailwatch")["enabled"] is new


# ---- who may change them -------------------------------------------------------------------------------------

def test_policy_lets_the_owners_turn_change_a_setting_without_a_question():
    args = {"action": "set", "key": "voice_note_speed", "value": 1.4}
    d = policy.decide("mcp__waggle__settings", args, turn=dict(OWNER))
    assert d.action == ALLOW and d.kind == "owner"


@pytest.mark.parametrize("turn", [None, {}, {"owner": False, "origin": "agent", "text": "talk faster"},
                                  {"owner": False, "origin": "test", "text": "set speed 1.5"}])
def test_policy_asks_in_any_other_turn(turn):
    args = {"action": "set", "key": "voice_note_speed", "value": 1.4}
    d = policy.decide("mcp__waggle__settings", args, turn=turn)
    assert d.action == ASK and d.kind == "waggle"
    assert policy.decide("mcp__waggle__settings", {}).action == ASK  # no action named: not a plain read


def test_policy_lets_anyone_read_the_settings():
    assert policy.decide("mcp__waggle__settings", {"action": "get"}).action == ALLOW


def test_the_owners_turn_is_not_a_blank_cheque_for_other_tools():
    assert policy.decide("mcp__waggle__no_such_tool", {}, turn=dict(OWNER)).action == ASK


def test_the_tool_itself_refuses_outside_the_owners_turn(monkeypatch):
    monkeypatch.setattr(guard, "current_turn", lambda env=None: None)
    for key, value in (("voice_note_speed", 1.5), ("quiet_hours", [1, 2]), ("brief_time", "09:00")):
        out = call(action="set", key=key, value=value)
        assert out["ok"] is False and "own chat message" in out["error"]
    monkeypatch.setattr(guard, "current_turn", lambda env=None: {**OWNER, "owner": False, "origin": "agent"})
    assert call(action="set", key="voice_note_speed", value=1.5)["ok"] is False
    assert Settings.load().voice_note_speed == 1.1 and not changes()


def test_a_process_with_no_turn_state_cannot_change_settings(monkeypatch):
    """Mail, web pages, background jobs and agents have no owner turn: guard.current_turn() is None for them."""
    monkeypatch.setenv("WAGGLE_TURN_ORIGIN", "agent")
    monkeypatch.delenv("WAGGLE_TURN_STATE", raising=False)
    assert guard.current_turn() is None
    assert call(action="set", key="voice_note_speed", value=1.8)["ok"] is False


def test_reading_works_in_any_turn(monkeypatch):
    monkeypatch.setattr(guard, "current_turn", lambda env=None: None)
    out = call(action="get")
    assert set(out["settings"]) == set(comfort.KEYS)
    assert "bm_george" in out["voices"] and "jf_alpha" not in out["voices"]
    assert out["settings"]["voice_note_speed"]["now"] == "1.1x"
    assert out["settings"]["quiet_hours"]["now"] == "off"  # the tests' home has [0, 0]


# ---- the reply, the backup, the history ----------------------------------------------------------------------

def test_set_returns_old_and_new_in_plain_words_and_records_an_event(owner_turn):
    out = call(action="set", key="voice_note_speed", value=1.4)
    assert (out["old"], out["new"], out["old_words"], out["new_words"]) == (1.1, 1.4, "1.1x", "1.4x")
    assert "1.1" in out["undo"]
    (e,) = changes()
    assert (e["key"], e["old"], e["new"], e["origin"], e["chat"], e["turn_id"]) == (
        "voice_note_speed", 1.1, 1.4, "user", "telegram:1", "t1")
    assert out["backup"] and (config.CONFIG_FILE.parent / out["backup"]).exists()


def test_quiet_hours_reply_reads_as_clock_times(owner_turn):
    config.CONFIG_FILE.write_text(json.dumps({**cfg(), "quiet_hours": [23, 7]}), encoding="utf-8")
    out = call(action="set", key="quiet_hours", value=[22, 7])
    assert (out["old_words"], out["new_words"]) == ("11 PM to 7 AM", "10 PM to 7 AM")


def test_setting_the_same_value_changes_nothing(owner_turn):
    out = call(action="set", key="voice_note_speed", value=1.1)
    assert out["ok"] and out["unchanged"] and not changes() and "backup" not in out


def test_only_the_newest_five_backups_are_kept(owner_turn):
    for i in range(9):
        assert call(action="set", key="voice_note_speed", value=1.2 + i / 20)["ok"]
    assert len(list(config.CONFIG_FILE.parent.glob("config.json.bak-settings-*"))) == comfort.KEEP_BACKUPS


def test_a_bad_value_changes_nothing_and_leaves_no_backup_or_event(owner_turn):
    before = config.CONFIG_FILE.read_text(encoding="utf-8")
    assert call(action="set", key="voice_note_speed", value=9)["ok"] is False
    assert config.CONFIG_FILE.read_text(encoding="utf-8") == before
    assert not changes() and not list(config.CONFIG_FILE.parent.glob("*.bak-settings-*"))


# ---- it takes effect without a restart ------------------------------------------------------------------------

def test_voice_and_speed_reach_the_speakers_at_once(owner_turn):
    assert (voiceline._voice(), voiceline._speed()) == ("af_heart", 1.1)
    call(action="set", key="voice_note_voice", value="bm_george")
    call(action="set", key="voice_note_speed", value=1.5)
    assert (voiceline._voice(), voiceline._speed()) == ("bm_george", 1.5)
    s = Settings.load()  # what the PC voice, the engine and the call sender read
    assert (s.voice_note_voice, s.voice_note_speed) == ("bm_george", 1.5)


def test_quiet_hours_take_effect_at_once(owner_turn):
    at = dt.datetime(2026, 11, 20, 22, 30)
    assert not notify.in_quiet_hours(None, at)
    call(action="set", key="quiet_hours", value=[22, 7])
    assert notify.in_quiet_hours(None, at)
    call(action="set", key="quiet_hours", value=[23, 7])
    assert not notify.in_quiet_hours(None, at)


def test_the_morning_and_evening_timers_move_with_the_setting(owner_turn):
    tick.register_core()
    assert timers.get("outreach.brief")["spec"] == "30 7 * * *"
    out = call(action="set", key="brief_time", value="8:15 am")
    assert out["ok"] and cfg()["brief_time"] == "08:15"
    t = timers.get("outreach.brief")
    assert t["spec"] == "15 8 * * *" and t["enabled"]
    assert call(action="set", key="evening_time", value="9:45pm")["ok"]
    assert timers.get("outreach.evening")["spec"] == "45 21 * * *"
    # the next core start builds the same timers from config.json
    tick.register_core()
    assert timers.get("outreach.brief")["spec"] == "15 8 * * *"
    assert call(action="get")["settings"]["brief_time"]["now"] == "8:15 AM"


def test_a_cron_brief_time_in_config_reads_back_as_a_clock_time(owner_turn):
    tick.register_core()
    config.CONFIG_FILE.write_text(json.dumps({**cfg(), "brief_time": "45 6 * * *"}), encoding="utf-8")
    out = call(action="set", key="brief_time", value="07:00")
    assert (out["old"], out["new_words"]) == ("06:45", "7:00 AM")


def test_a_timer_failure_puts_config_back(owner_turn, monkeypatch):
    tick.register_core()
    monkeypatch.setattr(timers, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")))
    out = call(action="set", key="evening_time", value="20:00")
    assert out["ok"] is False and "nothing was changed" in out["error"]
    assert Settings.load().evening_time == "21:00" and not changes()


def test_mailwatch_off_and_on_through_its_timer(owner_turn):
    timers.add("knowledge.mailwatch", "cron", "*/5 6-22 * * *", "knowledge.mailwatch")
    assert call(action="get")["settings"]["mailwatch"]["now"] == "on"
    out = call(action="set", key="mailwatch", value=False)
    assert (out["old_words"], out["new_words"]) == ("on", "off")
    assert timers.get("knowledge.mailwatch")["enabled"] is False
    timers.add("knowledge.mailwatch", "cron", "*/5 6-22 * * *", "knowledge.mailwatch")  # the core re-adds it at start
    assert timers.get("knowledge.mailwatch")["enabled"] is False
    assert call(action="set", key="mailwatch", value=True)["ok"]
    assert timers.get("knowledge.mailwatch")["enabled"] is True
    assert [e["new"] for e in changes()] == [False, True]


def test_mailwatch_before_the_core_has_made_its_timer_is_refused(owner_turn):
    out = call(action="set", key="mailwatch", value=False)
    assert out["ok"] is False and "does not exist yet" in out["error"]
    assert call(action="get")["settings"]["mailwatch"]["now"] == "not set up yet"


def test_the_chat_brain_is_told_to_use_the_tool():
    from waggle import chat_session
    assert "settings tool" in chat_session.CHAT_SYSTEM and "how to undo" in chat_session.CHAT_SYSTEM
