"""The keyword spotter and its place in the PC voice's wake path, with frames mocked: the model files and their
checksums, the keyword lines, the fallbacks, and pc_voice_wake_mode "stt" | "kws" | "both" with a fake spotter (no
sherpa-onnx, no model file, no microphone, no network). When the real model is installed
(`python -m waggle.kws fetch`, then WAGGLE_TEST_MODELS=<its models folder>) one test also loads it and scans silence."""
import array
import hashlib
import io
import logging
import math
import os
import tarfile

import pytest

from waggle import kws, mic, pcvoice, vad


# ---- files and keywords ------------------------------------------------------------------------------------

def make_archive(files, root="root"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        extra = tarfile.TarInfo(f"{root}/../evil.txt")  # never unpacked: only the four wanted members are read
        extra.size = 1
        tf.addfile(extra, io.BytesIO(b"x"))
    return buf.getvalue()


@pytest.fixture
def pinned(monkeypatch):
    """Swap the pinned archive and files for tiny ones so fetch() can be run for real."""
    data = {"tokens": b"<blk> 0\n", "encoder": b"E" * 40, "decoder": b"D" * 30, "joiner": b"J" * 20}
    names = {"tokens": "tokens.txt", "encoder": "enc.onnx", "decoder": "dec.onnx", "joiner": "join.onnx"}
    files = {k: (names[k], len(v), hashlib.sha256(v).hexdigest()) for k, v in data.items()}
    archive = make_archive({names[k]: v for k, v in data.items()})
    monkeypatch.setattr(kws, "FILES", files)
    monkeypatch.setattr(kws, "ARCHIVE", {**kws.ARCHIVE, "root": "root", "size": len(archive),
                                         "sha256": hashlib.sha256(archive).hexdigest()})
    return data, archive


def test_fetch_checks_the_archive_and_every_file(tmp_path, pinned):
    data, archive = pinned
    assert kws.verified(tmp_path) is None
    got = kws.fetch(tmp_path, download=lambda url: archive)
    assert set(got) == {"tokens", "encoder", "decoder", "joiner"}
    assert kws.verified(tmp_path) is not None
    assert not list((tmp_path / kws.FOLDER).glob("*.part"))
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / kws.FOLDER / "evil.txt").exists()
    again = kws.fetch(tmp_path, download=lambda url: pytest.fail("a verified copy is not downloaded again"))
    assert again == got


def test_fetch_refuses_the_wrong_archive_and_a_wrong_member(tmp_path, pinned):
    data, archive = pinned
    with pytest.raises(kws.KwsError, match="not the pinned one"):
        kws.fetch(tmp_path, download=lambda url: archive + b"!")
    assert not (tmp_path / kws.FOLDER).exists() or not any((tmp_path / kws.FOLDER).iterdir())
    bad = make_archive({"tokens.txt": b"<blk> 0\n", "enc.onnx": b"X" * 40, "dec.onnx": b"D" * 30, "join.onnx": b"J" * 20})
    import hashlib as h
    kws.ARCHIVE.update(size=len(bad), sha256=h.sha256(bad).hexdigest())  # archive matches, a member does not
    with pytest.raises(kws.KwsError, match="enc.onnx"):
        kws.fetch(tmp_path, download=lambda url: bad)
    assert kws.verified(tmp_path) is None


def test_a_damaged_file_is_not_verified(tmp_path, pinned):
    data, archive = pinned
    kws.fetch(tmp_path, download=lambda url: archive)
    (tmp_path / kws.FOLDER / "enc.onnx").write_bytes(b"Y" * 40)
    assert kws.verified(tmp_path) is None


def test_the_pinned_values_are_complete():
    assert len(kws.ARCHIVE["sha256"]) == 64 and kws.ARCHIVE["size"] < 100 * 1024 * 1024
    assert set(kws.FILES) == {"tokens", "encoder", "decoder", "joiner"}
    for name, size, sha in kws.FILES.values():
        assert size > 0 and len(sha) == 64
    assert sum(size for _, size, _ in kws.FILES.values()) < 10 * 1024 * 1024  # a few MB on disk


TOKENS = {"▁WA", "G", "LE", "▁HE", "Y", "▁JAR", "VIS", "▁J", "A", "R", "▁WE", "AGE", "L"}


def test_waggle_gets_its_spellings_and_hey_waggle():
    text = kws.keyword_lines(["Waggle"], TOKENS, threshold=0.3, score=1.5)
    lines = text.strip().splitlines()
    assert lines[0] == "▁WA G G LE :1.5 #0.3 @WAGGLE"
    assert len(lines) == len(kws.WAGGLE_VARIANTS) + 2
    assert any(line.startswith("▁HE Y ▁WA G G LE") for line in lines)
    assert all(line.endswith("@WAGGLE") for line in lines)
    plain = kws.keyword_lines(["Waggle"], TOKENS, variants=False, greet=False).strip().splitlines()
    assert plain == ["▁WA G G LE :1 #0.25 @WAGGLE"]


def test_another_name_is_split_from_the_token_list_and_unspellable_ones_are_skipped(caplog):
    assert kws.pieces("Jarvis", TOKENS) == ["▁JAR", "VIS"]
    assert kws.pieces("Zzz", TOKENS) is None
    text = kws.keyword_lines(["Jarvis", "Zzz"], TOKENS)
    assert text.strip().splitlines() == ["▁JAR VIS :1 #0.25 @JARVIS"]
    with caplog.at_level(logging.WARNING, logger="waggle.kws"):
        assert kws.keyword_lines(["Zzz"], TOKENS) == ""
    assert "Zzz" in caplog.text


def test_load_without_the_model_returns_none_and_logs_once(tmp_path, caplog):
    kws.reset()
    with caplog.at_level(logging.WARNING, logger="waggle.kws"):
        assert kws.load(["Waggle"], folder=tmp_path) is None
        assert kws.load(["Waggle"], folder=tmp_path) is None
    assert caplog.text.count("no verified model") == 1
    kws.reset()


def test_the_level_is_raised_for_a_quiet_microphone_only():
    np = pytest.importorskip("numpy")
    quiet = (np.sin(np.arange(16000) / 5.0) * 0.01).astype(np.float32)  # 1% of full scale, like the webcam
    loud = (np.sin(np.arange(16000) / 5.0) * 0.5).astype(np.float32)
    assert float(np.abs(kws.leveled(quiet)).max()) == pytest.approx(0.3, abs=0.02)
    assert float(np.abs(kws.leveled(quiet * 0.0001)).max()) <= 0.31 * 1.0  # never more than MAX_GAIN times
    assert np.array_equal(kws.leveled(loud), loud)
    assert len(kws.leveled(np.zeros(0, dtype=np.float32))) == 0


# ---- the wake path with a fake spotter ---------------------------------------------------------------------

class FakeSpotter:
    """Stands in for kws.Spotter: scan() answers from `hits` (start_s, end_s) and remembers what it was given."""
    def __init__(self, hits=(), fail=False):
        self.hits, self.fail, self.scanned = [kws.Hit("WAGGLE", a, b) for a, b in hits], fail, []

    def scan(self, pcm, **kw):
        if self.fail:
            raise RuntimeError("model blew up")
        self.scanned.append(len(pcm))
        return list(self.hits)


class FakeClient:
    def __init__(self, transcripts=()):
        self.transcripts, self.calls = list(transcripts), []

    def transcribe(self, pcm):
        self.calls.append(len(pcm))
        return {"text": self.transcripts.pop(0) if self.transcripts else "", "ok": True}


class FakeSpk:
    speaking = False

    def stop(self):
        pass


def frames(parts):
    """30 ms frames of (seconds, amplitude): amplitude 0 is silence, anything else a 200 Hz voice-like tone."""
    out, i = [], 0
    for seconds, amp in parts:
        for _ in range(int(seconds * 1000 / mic.FRAME_MS)):
            s = array.array("h")
            for _ in range(mic.FRAME_SAMPLES):
                s.append(int(amp * math.sin(2 * math.pi * 200 * i / mic.RATE)))
                i += 1
            out.append(s.tobytes())
    return out


@pytest.fixture
def rig(monkeypatch):
    """A PcVoice in dry-run wake mode on the energy gate; `feed()` runs frames through wake_frame as run() does."""
    monkeypatch.setenv("WAGGLE_MODELS_DIR", "/nonexistent-models")  # no Silero: the energy gate, deterministic
    settings = {"pc_voice_wake_mode": "stt", "pc_voice_kws_trust": True, "pc_voice_kws_head_s": 1.0,
                "pc_voice_smart": False, "pc_voice_vad": "energy"}
    real = pcvoice.setting
    monkeypatch.setattr(pcvoice, "setting", lambda k: settings[k] if k in settings else real(k))

    class Rig:
        def __init__(self):
            self.settings = settings

        def voice(self, mode, spotter=None, transcripts=(), **kw):
            self.client = FakeClient(transcripts)
            self.pv = pcvoice.PcVoice(spk=FakeSpk(), client=self.client, wake=True, dry_wake=True, wake_mode=mode,
                                      spotter=spotter, smart=False, **kw)
            return self.pv

        def say(self, speech_s=1.0):
            """2 s of room, `speech_s` of a voice, 1.2 s of room: one utterance."""
            ep = None
            for f in frames([(2.0, 0), (speech_s, 3000), (1.2, 0)]):
                ep = self.pv.wake_frame(None, ep, f)
            return self.pv
    return Rig()


def test_stt_mode_transcribes_everything_and_never_touches_the_spotter(rig):
    sp = FakeSpotter(hits=[(0.5, 0.9)])
    pv = rig.voice("stt", spotter=sp, transcripts=["Waggle what time is it"])
    rig.say()
    assert pv.stats["wake_triggers"] == 1 and sp.scanned == []
    assert pv.trigger_rests == ["what time is it"]


def test_kws_mode_never_transcribes_speech_without_the_name(rig):
    sp = FakeSpotter(hits=[])
    pv = rig.voice("kws", spotter=sp, transcripts=["pizza tonight"])
    rig.say(1.5)
    rig.say(1.5)
    assert rig.client.calls == []  # room talk: no recogniser call at all
    assert pv.stats["kws_skipped"] == 2 and pv.stats["utterances"] == 2 and pv.stats["transcribed"] == 0
    assert len(sp.scanned) == 2


def test_kws_mode_a_hit_sends_the_utterance_with_a_pre_roll_to_the_recogniser(rig):
    sp = FakeSpotter(hits=[(0.55, 0.95)])
    pv = rig.voice("kws", spotter=sp, transcripts=["Waggle, what time is it?"])
    rig.say(1.2)
    assert pv.stats["kws_hits"] == 1 and pv.stats["wake_triggers"] == 1
    assert pv.trigger_rests == ["what time is it?"]
    sent = rig.client.calls[0] / 2 / mic.RATE
    # the endpointer's own pre-roll (0.3 s) + speech + the kept tail, plus the extra second of earlier sound
    assert sent > 1.2 + 0.3 + 0.2 + 0.9
    assert sent < 1.2 + 1.5 + 0.2 + 0.6


def test_the_spotter_reads_only_the_head_of_the_utterance(rig):
    sp = FakeSpotter(hits=[])
    rig.voice("kws", spotter=sp)
    rig.say(9.0)
    head = sp.scanned[0] / 2 / mic.RATE
    assert head <= 0.3 + 1.0 + pcvoice.KWS_SCAN_TAIL_S + 0.01  # never the whole nine seconds


def test_a_name_that_starts_late_in_a_sentence_is_not_a_wake(rig):
    sp = FakeSpotter(hits=[(2.6, 3.0)])  # "... the waggle dance of bees": the name begins 2.3 s in
    pv = rig.voice("kws", spotter=sp, transcripts=["I read about the waggle dance"])
    rig.say(4.0)
    assert rig.client.calls == [] and pv.stats["kws_skipped"] == 1


def test_hey_waggle_fits_the_head_window(rig):
    sp = FakeSpotter(hits=[(0.3 + 0.9, 0.3 + 1.3)])  # the name starts 0.9 s after the first word, "hey" before it
    pv = rig.voice("kws", spotter=sp, transcripts=["Hey Waggle, what's on today?"])
    rig.say(2.0)
    assert pv.stats["wake_triggers"] == 1 and pv.trigger_rests == ["what's on today?"]


def test_kws_trusts_a_hit_the_recogniser_wrote_as_another_word(rig):
    sp = FakeSpotter(hits=[(0.5, 0.9)])
    pv = rig.voice("kws", spotter=sp, transcripts=["Bagel, what time is it?", "what time is it"])
    rig.say(2.0)
    assert pv.stats["wake_triggers"] == 1 and pv.stats["kws_rescued"] == 1
    assert len(rig.client.calls) == 2  # the whole utterance, then only what came after the name
    assert rig.client.calls[1] < rig.client.calls[0]
    assert pv.trigger_rests == ["what time is it"]


@pytest.mark.parametrize("heard, rest", [
    ("Tay Waggle, what time is it?", "what time is it?"),  # "Hey" misheard, the name written
    ("Wiggle, what time is it?", "what time is it?"),
    ("Okay waggle", ""),
    ("The waggle dance of bees is amazing.", None),  # inside a sentence: not a wake, even with a spotter hit
    ("I read that the waggle dance is how bees talk.", None),
    ("My friend said that waggle sounds funny.", None),
    ("Then he said, waggle your fingers.", None),
])
def test_a_name_like_word_inside_a_sentence_is_not_rescued(rig, heard, rest):
    sp = FakeSpotter(hits=[(0.5, 0.9)])
    pv = rig.voice("kws", spotter=sp, transcripts=[heard, "unused tail"])
    rig.say(2.0)
    assert pv.stats["wake_triggers"] == (0 if rest is None else 1)
    if rest is not None:
        assert pv.trigger_rests == [rest]
    assert len(rig.client.calls) == 1  # the words were enough: no second recogniser call


def test_a_rescued_name_with_nothing_after_it_is_only_the_name(rig):
    sp = FakeSpotter(hits=[(0.6, 1.1)])
    pv = rig.voice("kws", spotter=sp, transcripts=["Bagel."])
    rig.say(0.9)
    assert pv.stats["wake_triggers"] == 1 and pv.trigger_rests == [""]
    assert len(rig.client.calls) == 1  # too little left to ask the recogniser about


def test_strict_mode_needs_the_recogniser_to_write_the_name_too(rig):
    rig.settings["pc_voice_kws_trust"] = False
    sp = FakeSpotter(hits=[(0.5, 0.9)])
    pv = rig.voice("kws", spotter=sp, transcripts=["Bagel, what time is it?"])
    rig.say(2.0)
    assert pv.stats["wake_triggers"] == 0 and pv.stats["not_for_waggle"] == 1 and len(rig.client.calls) == 1


def test_both_mode_transcribes_everything_and_still_trusts_the_spotter(rig):
    sp = FakeSpotter(hits=[])
    pv = rig.voice("both", spotter=sp, transcripts=["Waggle, stretch", "pizza tonight"])
    rig.say(1.5)
    rig.say(1.5)
    assert len(rig.client.calls) == 2 and pv.stats["wake_triggers"] == 1  # no spotter hit, the text alone woke it
    assert pv.stats["kws_skipped"] == 0
    sp.hits = [kws.Hit("WAGGLE", 0.5, 0.9)]
    pv2 = rig.voice("both", spotter=sp, transcripts=["Bagel what time", "what time"])
    rig.say(2.0)
    assert pv2.stats["kws_rescued"] == 1 and pv2.stats["wake_triggers"] == 1


def test_a_missing_model_falls_back_to_stt_and_says_so_once(rig, caplog, tmp_path, monkeypatch):
    kws.reset()
    monkeypatch.setenv("WAGGLE_MODELS_DIR", str(tmp_path))  # no kws files there
    pv = rig.voice("kws", transcripts=["pizza", "Waggle hi"])
    with caplog.at_level(logging.WARNING):
        rig.say()
        rig.say()
    assert pv.wake_mode == "stt" and pv.configured_mode == "kws"
    assert len(rig.client.calls) == 2 and pv.stats["wake_triggers"] == 1  # everything transcribed again
    assert caplog.text.count("falls back to stt") == 1
    kws.reset()


def test_a_spotter_that_raises_falls_back_for_good(rig, caplog):
    sp = FakeSpotter(fail=True)
    pv = rig.voice("kws", spotter=sp, transcripts=["Waggle one", "Waggle two"])
    with caplog.at_level(logging.WARNING):
        rig.say()
        rig.say()
    assert pv.wake_mode == "stt" and pv.stats["wake_triggers"] == 2
    assert caplog.text.count("falls back to stt") == 1


def test_a_bad_mode_is_stt(rig, caplog):
    with caplog.at_level(logging.WARNING):
        pv = rig.voice("kws-ish")
    assert pv.wake_mode == "stt" and "stt, kws or both" in caplog.text


def test_the_default_is_kws_and_falls_back_to_stt_without_the_model(rig, caplog, tmp_path, monkeypatch):
    """kws is the default after the bench (docs/PC_VOICE.md); a PC without the model behaves as before."""
    assert pcvoice.DEFAULTS["pc_voice_wake_mode"] == "kws"
    kws.reset()
    monkeypatch.setenv("WAGGLE_MODELS_DIR", str(tmp_path))
    rig.settings.pop("pc_voice_wake_mode")  # the real default, not the fixture's override
    monkeypatch.setattr(pcvoice, "setting", lambda k: rig.settings[k] if k in rig.settings else pcvoice.DEFAULTS[k])
    pv = pcvoice.PcVoice(spk=FakeSpk(), client=FakeClient(["Waggle hello"]), wake=True, dry_wake=True, smart=False)
    assert pv.configured_mode == "kws"
    with caplog.at_level(logging.WARNING):
        rig.pv = pv
        rig.say()
    assert pv.wake_mode == "stt" and pv.stats["wake_triggers"] == 1
    kws.reset()


def test_a_follow_up_still_needs_no_name(rig):
    """The conversation windows do not go through the wake path at all: wake_mode does not appear in _hear."""
    import inspect
    assert "wake_mode" not in inspect.getsource(pcvoice.PcVoice._hear)
    assert "_kws" not in inspect.getsource(pcvoice.PcVoice._hear)


def test_endpointer_frames_held_counts_the_preroll():
    ep = mic.Endpointer(min_on=100)
    n = 0
    for f in frames([(1.0, 0), (0.5, 3000)]):
        n += 1
        if ep.feed(f) == "start":
            break
    assert ep.frames_held() >= 3  # the pre-roll plus the frames that started it


REAL = os.environ.get("WAGGLE_TEST_MODELS")


@pytest.mark.skipif(not REAL or kws.verified(REAL) is None, reason="WAGGLE_TEST_MODELS has no verified keyword model")
def test_the_real_model_loads_and_scans_silence_without_a_hit():
    pytest.importorskip("sherpa_onnx")
    kws.reset()
    sp = kws.load(["Waggle"], folder=REAL)
    assert sp is not None
    assert sp.scan(bytes(32000)) == []
    kws.reset()
