"""Presence: the owner is at the PC when the session is unlocked and the last input is younger than the limit."""
import json
import os

import pytest

import waggle.config as config
from waggle import presence


class FakeApi:
    def __init__(self, idle=0.0, locked=False, fail=None):
        self.idle, self.lock, self.fail = idle, locked, fail

    def idle_s(self):
        if self.fail:
            raise self.fail
        return self.idle

    def locked(self):
        return self.lock


def test_recent_input_and_unlocked_is_at_the_pc():
    s = presence.state(api=FakeApi(idle=12.0))
    assert s["at_pc"] is True and s["idle_s"] == 12.0 and s["locked"] is False and s["limit_s"] == 300.0


def test_idle_past_the_default_five_minutes_is_away():
    assert presence.at_pc(api=FakeApi(idle=299.0)) is True
    s = presence.state(api=FakeApi(idle=300.0))
    assert s["at_pc"] is False and "no input" in s["why"]


def test_a_locked_session_is_away_even_with_fresh_input():
    s = presence.state(api=FakeApi(idle=1.0, locked=True))
    assert s["at_pc"] is False and s["why"] == "the session is locked"


def test_the_idle_limit_is_an_argument_and_a_setting():
    assert presence.at_pc(idle_limit=60, api=FakeApi(idle=90)) is False
    data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    data["pc_presence_idle_s"] = 30
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert presence.idle_limit_s() == 30.0
    assert presence.at_pc(api=FakeApi(idle=45)) is False
    data["pc_presence_idle_s"] = "nonsense"
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert presence.idle_limit_s() == 300.0


def test_an_api_failure_means_not_known_to_be_here():
    s = presence.state(api=FakeApi(fail=OSError(5, "denied")))
    assert s["at_pc"] is False and s["why"].startswith("cannot read presence")


@pytest.mark.skipif(os.name != "nt", reason="the Windows API")
def test_the_real_api_answers_with_numbers():
    s = presence.state()
    assert isinstance(s["idle_s"], float) and s["idle_s"] >= 0 and isinstance(s["locked"], bool)
