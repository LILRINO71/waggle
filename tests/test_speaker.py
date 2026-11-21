"""The PC speaker: one utterance at a time, in order, cut off at once by stop(), files deleted when asked."""
import threading
import time
import wave

from waggle import speaker


class FakeBackend:
    def __init__(self):
        self.played, self.stops, self.events = [], 0, []
        self.lock = threading.Lock()

    def play(self, path):
        with self.lock:
            self.played.append(path.name)
            self.events.append(("play", path.name, time.monotonic()))

    def stop(self):
        with self.lock:
            self.stops += 1
            self.events.append(("stop", None, time.monotonic()))


def tone(tmp_path, name, ms):
    return speaker.tone_wav(tmp_path / name, ms=ms)


def test_tone_wav_is_a_quiet_mono_pcm_file_of_the_asked_length(tmp_path):
    p = speaker.tone_wav(tmp_path / "t.wav", freq=440, ms=500, amp=0.05)
    with wave.open(str(p), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        frames = w.readframes(w.getnframes())
    assert abs(speaker.wav_seconds(p) - 0.5) < 0.001
    import array
    samples = array.array("h", frames)
    assert max(abs(s) for s in samples) <= int(32767 * 0.05) + 1
    assert abs(samples[0]) < 50 and abs(samples[-1]) < 50  # soft edges, no click


def test_utterances_play_one_at_a_time_in_order(tmp_path):
    fb = FakeBackend()
    sp = speaker.Speaker(fb, tail_s=0.0).start()
    a, b = sp.say(tone(tmp_path, "a.wav", 120)), sp.say(tone(tmp_path, "b.wav", 80))
    assert sp.speaking
    assert b.done.wait(3) and a.done.is_set()
    assert fb.played == ["a.wav", "b.wav"] and fb.stops == 0
    (_, _, ta), (_, _, tb) = fb.events
    assert tb - ta >= 0.11  # b waited for a's length
    assert sp.wait_idle(1) and not sp.speaking
    assert not a.cut and not b.cut
    sp.close()


def test_stop_cuts_the_current_one_off_at_once_and_drops_the_queue(tmp_path):
    fb = FakeBackend()
    sp = speaker.Speaker(fb).start()
    long_one = sp.say(tone(tmp_path, "long.wav", 3000), delete=True)
    queued = sp.say(tone(tmp_path, "next.wav", 500), delete=True)
    time.sleep(0.1)
    t0 = time.monotonic()
    assert sp.stop() == 2
    assert long_one.done.wait(1) and time.monotonic() - t0 < 0.5
    assert long_one.cut and queued.cut and queued.done.is_set()
    assert fb.played == ["long.wav"] and fb.stops == 1
    assert not (tmp_path / "long.wav").exists() and not (tmp_path / "next.wav").exists()
    assert sp.wait_idle(1)
    sp.close()


def test_play_now_cuts_off_and_waits_for_the_tone(tmp_path):
    fb = FakeBackend()
    sp = speaker.Speaker(fb, tail_s=0.0).start()
    talking = sp.say(tone(tmp_path, "talk.wav", 3000))
    time.sleep(0.05)
    t0 = time.monotonic()
    beep = sp.play_now(tone(tmp_path, "beep.wav", 100))
    assert beep.done.is_set() and talking.cut and not beep.cut
    assert 0.09 <= time.monotonic() - t0 < 1.0
    assert fb.played == ["talk.wav", "beep.wav"]
    sp.close()


def test_a_broken_file_does_not_stop_the_queue(tmp_path):
    fb = FakeBackend()
    sp = speaker.Speaker(fb, tail_s=0.0).start()
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    first, second = sp.say(bad), sp.say(tone(tmp_path, "ok.wav", 50))
    assert second.done.wait(2)
    assert first.error and not second.error and fb.played == ["ok.wav"]
    sp.close()


def test_on_start_reports_when_each_utterance_begins(tmp_path):
    seen = []
    sp = speaker.Speaker(FakeBackend(), tail_s=0.0, on_start=lambda it: seen.append(it.label)).start()
    sp.say(tone(tmp_path, "x.wav", 30), label="reply").done.wait(2)
    assert seen == ["reply"]
    sp.close()
