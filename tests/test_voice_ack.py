"""Spoken fillers: only after the delay, only once, never after the reply is ready."""
import time

from waggle import voice_ack


def test_filler_fires_after_delay_once():
    said = []
    f = voice_ack.Filler(delay=0.05, player=said.append)
    time.sleep(0.15)
    f.now("act")
    f.cancel()
    assert said == ["wait"]


def test_filler_cancelled_before_delay_is_silent():
    said = []
    f = voice_ack.Filler(delay=0.2, player=said.append)
    f.cancel()
    time.sleep(0.3)
    assert said == []


def test_act_speaks_immediately_and_disabled_never_speaks():
    said = []
    f = voice_ack.Filler(delay=5, player=said.append)
    f.now("act")
    f.cancel()
    assert said == ["act"]
    g = voice_ack.Filler(delay=0.01, enabled=False, player=said.append)
    g.now("act")
    assert said == ["act"]


def test_prepare_synthesises_missing_phrases():
    posted = []

    def post(body):
        import json
        out = json.loads(body)["out"]
        open(out, "wb").write(b"RIFF" + b"\0" * 2000)
        posted.append(out)

    n = voice_ack.prepare(post=post)
    assert n == sum(len(v) for v in voice_ack.PHRASES.values())
    assert voice_ack.prepare(post=post) == 0  # cached
