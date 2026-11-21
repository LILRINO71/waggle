"""Speech detection (Silero VAD) and end-of-turn detection (Smart Turn) for the PC voice: the model files and their
checksums, the endpointers with fake models (no model file needed), the fallbacks, the settings, and, when the two
official model files are available (WAGGLE_TEST_MODELS=<folder>), the real models on a few synthesised
sentences. Nothing here opens the microphone, plays a sound or touches the network."""
import array
import hashlib
import json
import logging
import math
import os
import random
import time
import wave
from pathlib import Path

import pytest

from waggle import mic, pcvoice, vad

FIXTURES = Path(__file__).parent / "fixtures" / "endturn"


# ---- helpers ----------------------------------------------------------------------------------------------

def tone(seconds, amp, freq=200.0, noise=0, seed=1):
    """30 ms frames of a sine of `amp` (plus uniform noise): amp 0 is silence."""
    rng = random.Random(seed)
    out, i = [], 0
    for _ in range(int(seconds * 1000 / mic.FRAME_MS)):
        s = array.array("h")
        for _ in range(mic.FRAME_SAMPLES):
            v = amp * math.sin(2 * math.pi * freq * i / mic.RATE) + (rng.uniform(-noise, noise) if noise else 0)
            s.append(max(-32768, min(32767, int(v))))
            i += 1
        out.append(s.tobytes())
    return out


class FakeStream:
    """A Silero stream whose probability is decided by the frame's loudness: `voice_above` (RMS) means speech."""

    def __init__(self, voice_above=60.0, p_voice=0.9, p_other=0.02, fail=False):
        self.voice_above, self.p_voice, self.p_other, self.fail, self.frames = voice_above, p_voice, p_other, fail, 0

    def feed(self, frame):
        self.frames += 1
        if self.fail:
            raise RuntimeError("model broke")
        return self.p_voice if mic.level(frame)[0] >= self.voice_above else self.p_other


class FakeTurn:
    """An end-of-turn model: `answer(pcm)` gives P(finished); every call is recorded."""

    def __init__(self, answer=0.95):
        self.answer, self.calls = answer, []

    def complete(self, pcm):
        self.calls.append(len(pcm) / 2 / mic.RATE)
        if callable(self.answer):
            return self.answer(pcm)
        return self.answer


def run(ep, frames):
    """Feed frames until the endpointer ends. Returns (index of the frame that started, index that ended)."""
    start = end = None
    for n, f in enumerate(frames):
        r = ep.feed(f)
        if r == "start":
            start = n
        if r == "end":
            end = n
            break
    return start, end


def smart_ep(turn, **kw):
    kw.setdefault("end_silence_s", 1.2)
    return mic.SileroGate(FakeStream(), turn=turn, min_on=120.0, min_off=80.0, **kw)


QUIET = tone(1.0, 0, noise=5)


# ---- the neural gate --------------------------------------------------------------------------------------

def test_the_silero_gate_ends_after_the_fixed_silence_like_the_energy_gate():
    ep = mic.SileroGate(FakeStream(), end_silence_s=1.2)
    start, end = run(ep, QUIET + tone(1.5, 2000, noise=10) + tone(3.0, 0, noise=5))
    assert start is not None and end is not None and ep.ended_by == "silence" and ep.mode == "energy+silence"
    speech_end = len(QUIET) + 50
    assert abs((end - speech_end) * 30 - 1200) <= 90
    assert 1.4 < len(ep.pcm(0.3)) / 2 / mic.RATE < 2.6


def test_silero_never_starts_what_the_energy_gate_would_not_but_a_lower_floor_lets_a_quiet_voice_in():
    quiet_voice = QUIET + tone(1.5, 55, noise=2) + tone(2.0, 0, noise=2)  # RMS about 40: below the level floor of 120
    assert run(mic.Endpointer(), quiet_voice) == (None, None)
    assert run(mic.SileroGate(FakeStream(voice_above=25.0)), quiet_voice) == (None, None)  # same floor: no new sources
    start, end = run(mic.SileroGate(FakeStream(voice_above=25.0), min_on=20.0), quiet_voice)  # pc_voice_min_level: 20
    assert start is not None and end is not None


def test_a_loud_steady_noise_the_model_calls_not_speech_never_starts_an_utterance():
    noisy = QUIET + tone(10.0, 3000, freq=90.0, noise=800)  # loud music or a fan
    assert run(mic.Endpointer(), noisy)[0] is not None  # the energy gate is fooled
    assert run(mic.SileroGate(FakeStream(voice_above=1e9)), noisy) == (None, None)


def test_probabilities_between_the_two_bars_do_not_count_as_a_pause():
    probs = [0.05] * 10 + [0.9] * 20 + [0.42] * 60 + [0.1] * 200  # 0.42 is neither speech (0.5) nor a pause (< 0.35)
    stream = type("S", (), {"feed": lambda self, f, it=iter(probs): next(it)})()
    ep = mic.SileroGate(stream, end_silence_s=1.2)
    start, end = run(ep, QUIET[:10] + tone(6.0, 1500))
    assert start == 10 + 1  # two frames of speech start it (60 ms); a single frame would not
    assert abs((end - (10 + 20 + 60) + 1) * 30 - 1200) <= 60  # the 0.42 stretch was not silence: the pause starts at 0.1


def test_one_frame_the_model_likes_is_a_click_not_speech():
    probs = [0.05] * 10 + [0.95] + [0.05] * 300
    stream = type("S", (), {"feed": lambda self, f, it=iter(probs): next(it)})()
    assert run(mic.SileroGate(stream), QUIET[:10] + tone(0.03, 3000) + tone(8.0, 0, noise=5)) == (None, None)


def test_a_failing_model_never_stops_the_microphone():
    ep = mic.SileroGate(FakeStream(fail=True))
    assert run(ep, QUIET + tone(2.0, 2000)) == (None, None)  # every frame reads as "not speech"; nothing raised


# ---- Smart Turn as the end of the turn ----------------------------------------------------------------------

def test_a_finished_sentence_ends_at_the_short_pause_not_after_the_fixed_silence():
    turn = FakeTurn(0.95)
    ep = smart_ep(turn)
    start, end = run(ep, QUIET + tone(1.5, 2000) + tone(3.0, 0, noise=5))
    speech_end = len(QUIET) + 50
    assert ep.ended_by == "turn" and ep.mode == "energy+silence"
    assert 0.2 <= (end - speech_end + 1) * 0.03 <= 0.32  # about 200 ms of pause, not 1.2 s
    assert len(turn.calls) == 1 and turn.calls[0] > 1.5  # asked once, with the whole utterance so far
    assert 1.4 < len(ep.pcm(0.3)) / 2 / mic.RATE < 2.6


def test_an_unfinished_sentence_keeps_listening_until_the_hard_limit():
    turn = FakeTurn(0.05)
    ep = smart_ep(turn, turn_max_silence_s=1.6)
    start, end = run(ep, QUIET + tone(1.5, 2000) + tone(5.0, 0, noise=5))
    speech_end = len(QUIET) + 50
    assert ep.ended_by == "silence"
    assert abs((end - speech_end + 1) * 30 - 1600) <= 60  # waited the full 1.6 s (longer than the old 1.2 s)
    assert 5 <= len(turn.calls) <= 20  # asked every ~90 ms, not every frame


def test_a_pause_that_the_model_calls_unfinished_and_then_more_speech_is_one_utterance():
    turn = FakeTurn(lambda pcm: 0.05 if len(pcm) / 2 / mic.RATE < 2.0 + 0.9 else 0.97)  # unfinished until the 2nd half
    ep = smart_ep(turn)
    frames = QUIET + tone(1.0, 2000) + tone(0.9, 0, noise=5) + tone(1.0, 2000, freq=300.0) + tone(3.0, 0, noise=5)
    start, end = run(ep, frames)
    assert ep.ended_by == "turn" and ep.speech_ms >= 1900
    assert len(ep.pcm(0.3)) / 2 / mic.RATE > 2.9  # both halves and the pause between them
    assert end * 30 > (len(QUIET) + 33 + 30 + 33) * 30 - 100  # it did not end at the first pause


def test_the_model_threshold_is_respected():
    ep = smart_ep(FakeTurn(0.8), turn_min_p=0.9, turn_max_silence_s=1.6)
    start, end = run(ep, QUIET + tone(1.0, 2000) + tone(5.0, 0, noise=5))
    assert ep.ended_by == "silence"
    ep = smart_ep(FakeTurn(0.8), turn_min_p=0.7)
    start, end = run(ep, QUIET + tone(1.0, 2000) + tone(5.0, 0, noise=5))
    assert ep.ended_by == "turn"


def test_a_model_that_raises_falls_back_to_the_fixed_silence():
    def boom(pcm):
        raise RuntimeError("onnx broke")
    ep = smart_ep(FakeTurn(boom), end_silence_s=1.2, turn_max_silence_s=1.6)
    start, end = run(ep, QUIET + tone(1.0, 2000) + tone(5.0, 0, noise=5))
    speech_end = len(QUIET) + 33
    assert ep.turn is None and ep.ended_by == "silence"
    assert abs((end - speech_end + 1) * 30 - 1200) <= 60


def test_without_a_model_nothing_changes():
    ep = smart_ep(None)
    start, end = run(ep, QUIET + tone(1.0, 2000) + tone(5.0, 0, noise=5))
    assert ep.ended_by == "silence" and abs((end - (len(QUIET) + 33) + 1) * 30 - 1200) <= 60


def test_the_model_only_gets_the_last_eight_seconds_and_the_maximum_length_still_applies():
    turn = FakeTurn(0.01)
    ep = smart_ep(turn, max_s=12.0)
    frames = QUIET + tone(6.0, 2000) + tone(0.3, 0, noise=5) + tone(6.0, 2000, freq=300.0) + tone(3.0, 0, noise=5)
    start, end = run(ep, frames)
    assert max(turn.calls) <= 8.0 + 0.03
    assert ep.ended_by == "max length" and end is not None


def test_the_hotkey_style_energy_gate_with_a_model_also_works():
    ep = mic.Endpointer(turn=FakeTurn(0.9), min_on=120.0, min_off=80.0)
    start, end = run(ep, QUIET + tone(1.5, 2000, noise=10) + tone(3.0, 0, noise=5))
    assert ep.ended_by == "turn" and end is not None


# ---- choosing the endpointer, with fallbacks --------------------------------------------------------------

def test_make_endpointer_uses_the_models_when_they_are_there(monkeypatch):
    model = type("M", (), {"stream": lambda self: FakeStream()})()
    turn = FakeTurn()
    monkeypatch.setattr(vad, "silero", lambda: model)
    monkeypatch.setattr(vad, "smart_turn", lambda: turn)
    ep = mic.make_endpointer(1.2, 30.0, vad="silero", endturn="smart", turn_max_silence_s=1.7, turn_min_p=0.6)
    assert isinstance(ep, mic.SileroGate) and ep.mode == "silero+smart"
    assert ep.turn is turn and ep.turn_max_silence_s == 1.7 and ep.turn_min_p == 0.6


def test_make_endpointer_falls_back_when_a_model_is_missing(monkeypatch):
    monkeypatch.setattr(vad, "silero", lambda: None)
    monkeypatch.setattr(vad, "smart_turn", lambda: None)
    notes = []
    ep = mic.make_endpointer(1.2, 30.0, vad="silero", endturn="smart", note=notes.append)
    assert type(ep) is mic.Endpointer and ep.mode == "energy+silence" and ep.turn is None
    assert len(notes) == 2 and "energy gate" in notes[0] and "fixed silence" in notes[1]
    only_turn = type("M", (), {"stream": lambda self: FakeStream()})()
    monkeypatch.setattr(vad, "silero", lambda: only_turn)
    assert mic.make_endpointer(vad="silero", endturn="smart").mode == "silero+silence"


def test_a_broken_silero_stream_falls_back_to_the_energy_gate(monkeypatch):
    def broken():
        raise RuntimeError("no session")
    monkeypatch.setattr(vad, "silero", lambda: type("M", (), {"stream": lambda self: broken()})())
    notes = []
    ep = mic.make_endpointer(vad="silero", note=notes.append)
    assert ep.mode == "energy+silence" and notes


def test_the_default_settings_never_load_a_model(monkeypatch):
    def boom():
        raise AssertionError("a model was loaded")
    monkeypatch.setattr(vad, "silero", boom)
    monkeypatch.setattr(vad, "smart_turn", boom)
    assert mic.make_endpointer().mode == "energy+silence"
    assert mic.make_endpointer(vad="energy", endturn="silence").mode == "energy+silence"


# ---- the model files and their checksums ------------------------------------------------------------------

def fake_spec(monkeypatch, name="silero_vad", data=b"the pinned bytes"):
    spec = dict(vad.MODELS[name], size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(vad.MODELS, name, spec)
    vad.reset()
    return spec, data


def test_the_registry_pins_official_files_by_size_and_sha256():
    for name, spec in vad.MODELS.items():
        assert len(spec["sha256"]) == 64 and int(spec["sha256"], 16) >= 0 and spec["size"] > 1_000_000
        assert spec["url"].startswith(("https://raw.githubusercontent.com/snakers4/silero-vad/v",
                                       "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/"))
        assert "/main/" not in spec["url"] and "latest" not in spec["url"]  # a tag or a commit, never a branch
        assert spec["licence"] in ("MIT", "BSD-2-Clause")


def test_models_live_in_the_homes_models_folder_unless_overridden(monkeypatch, tmp_path):
    from waggle import config
    monkeypatch.delenv("WAGGLE_MODELS_DIR", raising=False)
    assert vad.models_dir() == Path(config.HOME) / "models"
    monkeypatch.setenv("WAGGLE_MODELS_DIR", str(tmp_path / "elsewhere"))
    assert vad.models_dir() == tmp_path / "elsewhere"


def test_a_file_with_the_wrong_content_is_not_used(monkeypatch, tmp_path):
    spec, data = fake_spec(monkeypatch)
    assert vad.verified_path("silero_vad", tmp_path) is None  # missing
    (tmp_path / spec["file"]).write_bytes(b"x" * len(data))  # right size, wrong bytes
    assert vad.verified_path("silero_vad", tmp_path) is None
    (tmp_path / spec["file"]).write_bytes(data[:-1])  # truncated
    assert vad.verified_path("silero_vad", tmp_path) is None
    (tmp_path / spec["file"]).write_bytes(data)
    assert vad.verified_path("silero_vad", tmp_path) == tmp_path / spec["file"]


def test_fetch_refuses_a_download_that_is_not_the_pinned_file_and_leaves_nothing(monkeypatch, tmp_path):
    spec, data = fake_spec(monkeypatch)
    folder = tmp_path / "models"
    for bad in (b"<html>rate limited</html>", b"x" * len(data), data + b"!"):
        with pytest.raises(vad.ModelError):
            vad.fetch("silero_vad", folder, download=lambda url, bad=bad: bad)
        assert not folder.exists() or list(folder.iterdir()) == []  # no model file, no .part file


def test_fetch_writes_a_verified_file_once(monkeypatch, tmp_path):
    spec, data = fake_spec(monkeypatch)
    folder = tmp_path / "models"
    urls = []
    p = vad.fetch("silero_vad", folder, download=lambda url: urls.append(url) or data)
    assert p.read_bytes() == data and urls == [spec["url"]] and [x.name for x in folder.iterdir()] == [spec["file"]]
    vad.fetch("silero_vad", folder, download=lambda url: pytest.fail("downloaded again"))


def test_nothing_in_the_tests_reaches_the_network(monkeypatch, tmp_path):
    monkeypatch.setattr(vad, "_download", lambda url, timeout=0: pytest.fail("network"))
    fake_spec(monkeypatch)
    monkeypatch.setenv("WAGGLE_MODELS_DIR", str(tmp_path))
    assert vad.silero() is None and vad.smart_turn() is None  # absent files: no download, just the fallback


def test_loading_a_missing_or_broken_model_gives_none_and_is_retried_later(monkeypatch, tmp_path):
    spec, data = fake_spec(monkeypatch)
    monkeypatch.setenv("WAGGLE_MODELS_DIR", str(tmp_path))
    built = []
    real_build = vad.SileroVAD

    class Boom:
        def __init__(self, path):
            built.append(path)
            raise RuntimeError("onnxruntime is not installed")
    monkeypatch.setattr(vad, "SileroVAD", Boom)
    assert vad.silero() is None and built == []  # no file yet
    (tmp_path / spec["file"]).write_bytes(data)
    assert vad.silero() is None and built == []  # the failure is remembered: not retried at once
    clock = [time.monotonic()]
    monkeypatch.setattr(vad.time, "monotonic", lambda: clock[0] + 1000)
    assert vad.silero() is None and len(built) == 1  # retried after the wait, and failed again, quietly
    monkeypatch.setattr(vad, "SileroVAD", real_build)


def test_the_command_line_status_and_unknown_names(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WAGGLE_MODELS_DIR", str(tmp_path))
    vad.reset()
    assert vad.main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["models"]) == {"silero_vad", "smart_turn"} and not out["models"]["silero_vad"]["verified"]
    monkeypatch.setattr(vad, "_download", lambda url, timeout=0: pytest.fail("network"))
    assert vad.main(["fetch", "nope"]) == 2


# ---- Smart Turn's features, against the reference extractor -----------------------------------------------

def test_the_numpy_features_match_whispers_reference_extractor():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(7)
    t = np.arange(48000) / 16000
    sig = (0.3 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 1.5 * t) > 0) + 0.02 * rng.standard_normal(48000)).astype(np.float32)
    f = vad.turn_features(sig)
    assert f.shape == (1, 80, 800) and f.dtype == np.float32
    # (mel bin, frame): values of transformers' WhisperFeatureExtractor(chunk_length=8) on this same signal, which the
    # extractor zero-pads in front to 8 s (so the first frames are the padding's floor)
    ref = [(0, 0, -0.0784), (0, 600, 1.4066), (0, 700, 1.3844), (0, 799, 0.9942), (20, 0, -0.1895),
           (20, 600, 0.9301), (20, 799, 0.821), (40, 650, 0.7146), (40, 700, 0.5906), (60, 600, 0.7101),
           (60, 799, 0.9074), (79, 300, -0.1895), (79, 650, 0.7542), (79, 799, 0.7736)]
    for m, k, want in ref:
        assert float(f[0, m, k]) == pytest.approx(want, abs=1e-3)


# ---- the real models on real (synthesised) sentences, when the official files are present -------------------

def read_pcm(name):
    with wave.open(str(FIXTURES / name), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return w.readframes(w.getnframes())


@pytest.fixture
def real_models(monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    d = os.environ.get("WAGGLE_TEST_MODELS")  # a folder with the two official files; the other tests never see it
    if not d or not all(vad.verified_path(n, d) for n in vad.MODELS):
        pytest.skip("set WAGGLE_TEST_MODELS to a folder with the two official model files (python -m waggle.vad fetch)")
    monkeypatch.setenv("WAGGLE_MODELS_DIR", d)
    vad.reset()
    yield
    vad.reset()


def frames_of_pcm(pcm):
    return [pcm[i:i + mic.FRAME_BYTES].ljust(mic.FRAME_BYTES, b"\x00") for i in range(0, len(pcm), mic.FRAME_BYTES)]


def test_real_silero_hears_speech_and_not_silence_or_a_steady_hum(real_models):
    pcm = read_pcm("complete.wav")
    stream = vad.silero().stream()
    probs = [stream.feed(f) for f in frames_of_pcm(b"\x00\x00" * 8000 + pcm + b"\x00\x00" * 8000)]
    assert max(probs[:10]) < 0.2 and max(probs) > 0.9 and max(probs[-10:]) < 0.2
    hum = b"".join(tone(3.0, 3000, freq=90.0, noise=800))
    s2 = vad.silero().stream()
    assert max(s2.feed(f) for f in frames_of_pcm(hum)) < 0.5


def test_real_smart_turn_separates_a_finished_request_from_a_stopped_one(real_models):
    st = vad.smart_turn()
    done = read_pcm("complete.wav") + b"\x00\x00" * int(0.3 * 16000)
    half = read_pcm("unfinished.wav") + b"\x00\x00" * int(0.3 * 16000)
    assert st.complete(done) > 0.8 and st.complete(half) < 0.2
    assert st.last_ms < 400


def test_real_models_end_a_finished_request_early_and_wait_through_a_pause(real_models):
    pcm = b"\x00\x00" * 24000 + read_pcm("complete.wav") + b"\x00\x00" * 64000
    new = mic.make_endpointer(1.2, 30.0, vad="silero", endturn="smart")
    old = mic.make_endpointer(1.2, 30.0, vad="energy", endturn="silence", min_on=120.0, min_off=80.0)
    assert new.mode == "silero+smart" and old.mode == "energy+silence"
    n_new = run(new, frames_of_pcm(pcm))[1]
    n_old = run(old, frames_of_pcm(pcm))[1]
    assert new.ended_by == "turn" and n_new is not None and n_old is not None
    assert (n_old - n_new) * 0.03 > 0.7  # about a second earlier
    # a request that stops in the middle ("... look up the, uh,"), then goes on after 1.5 s: the fixed 1.2 s silence
    # cuts it in two and loses "opening hours of the museum"; the model hears that it is not finished and waits
    first, rest = read_pcm("unfinished.wav"), read_pcm("rest.wav")
    audio = b"\x00\x00" * 24000 + first + b"\x00\x00" * int(1.5 * 16000) + rest + b"\x00\x00" * 64000
    cut = mic.make_endpointer(1.2, 30.0, vad="silero", endturn="silence")
    run(cut, frames_of_pcm(audio))
    assert cut.ended_by == "silence" and len(cut.pcm(0.3)) / 2 / 16000 < (len(first) + len(rest)) / 2 / 16000 - 0.5
    smart = mic.make_endpointer(1.2, 30.0, vad="silero", endturn="smart")
    run(smart, frames_of_pcm(audio))
    assert smart.ended_by == "turn" and smart.turn_asks >= 3
    assert len(smart.pcm(0.3)) / 2 / 16000 > (len(first) + len(rest)) / 2 / 16000 + 1.4  # both halves and the pause


# ---- the PC voice settings --------------------------------------------------------------------------------

def test_the_new_settings_have_defaults_and_read_the_config(settings):
    from waggle import config
    for key, default in (("pc_voice_vad", "silero"), ("pc_voice_endturn", "silence"), ("pc_voice_endturn_max_s", 1.6),
                         ("pc_voice_endturn_pause_ms", 200.0), ("pc_voice_endturn_min_p", 0.5)):
        assert pcvoice.setting(key) == default
    data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    data.update(pc_voice_vad="energy", pc_voice_endturn="smart", pc_voice_endturn_min_p="0.7")
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert pcvoice.setting("pc_voice_vad") == "energy" and pcvoice.setting("pc_voice_endturn") == "smart"
    assert pcvoice.setting("pc_voice_endturn_min_p") == 0.7


def configure(**kw):
    from waggle import config
    data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    data.update(kw)
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


def bare_pcvoice():
    return pcvoice.PcVoice(mic_factory=lambda: None, spk=None, client=None, sounds=None, wake=False, smart=False)


def test_the_pc_voice_builds_the_endpointer_the_settings_ask_for(settings, monkeypatch):
    model = type("M", (), {"stream": lambda self: FakeStream()})()
    monkeypatch.setattr(vad, "silero", lambda: model)
    monkeypatch.setattr(vad, "smart_turn", lambda: FakeTurn())
    pv = bare_pcvoice()
    assert pv.new_endpointer(1.2, 30.0).mode == "silero+silence"  # the defaults: the neural gate, the fixed silence
    configure(pc_voice_vad="energy")
    assert pv.new_endpointer(1.2, 30.0).mode == "energy+silence"  # the off switch
    configure(pc_voice_vad="silero", pc_voice_endturn="smart", pc_voice_endturn_max_s=2.0, pc_voice_endturn_min_p=0.7,
              pc_voice_endturn_pause_ms=250)
    ep = pv.new_endpointer(1.2, 30.0)
    assert isinstance(ep, mic.SileroGate) and ep.mode == "silero+smart" and pv.stats["hearing"] == "silero+smart"
    assert (ep.turn_max_silence_s, ep.turn_min_p, ep.turn_pause_ms) == (2.0, 0.7, 250)
    assert ep.min_on == 120.0 and ep.min_off == 80.0


def test_missing_models_mean_the_old_behaviour_and_one_log_line(settings, monkeypatch, caplog):
    monkeypatch.setattr(vad, "silero", lambda: None)
    monkeypatch.setattr(vad, "smart_turn", lambda: None)
    configure(pc_voice_vad="silero", pc_voice_endturn="smart")
    pv = bare_pcvoice()
    with caplog.at_level(logging.WARNING, logger="waggle.pcvoice"):
        for _ in range(3):
            assert pv.new_endpointer(1.2, 30.0).mode == "energy+silence"
    assert pv.stats["hearing"] == "energy+silence"
    assert len([r for r in caplog.records if "hearing" in r.getMessage()]) == 2  # once for each model, not per call


def test_listening_to_a_file_ends_the_turn_early_when_the_model_says_finished(settings, monkeypatch, tmp_path):
    from test_pcvoice import speech_file
    model = type("M", (), {"stream": lambda self: FakeStream(voice_above=150.0)})()
    monkeypatch.setattr(vad, "silero", lambda: model)
    monkeypatch.setattr(vad, "smart_turn", lambda: FakeTurn(0.97))
    path = speech_file(tmp_path / "in.wav", [(0.6, 0), (1.5, 3000), (3.0, 0)])
    pv = bare_pcvoice()
    pv.stopping = type("E", (), {"is_set": lambda self: False, "set": lambda self: None})()
    configure(pc_voice_endturn="smart")
    pcm, info = pv.listen(mic.FileSource(path, trail_s=0.0).open(), 6.0)
    assert info["ended_by"] == "turn" and pcm is not None
    assert len(pcm) / 2 / mic.RATE < 0.3 + 1.5 + 0.5
    configure(pc_voice_vad="energy", pc_voice_endturn="silence")
    pcm2, info2 = pv.listen(mic.FileSource(path, trail_s=0.0).open(), 6.0)
    assert info2["ended_by"] == "silence" and pcm2 is not None
