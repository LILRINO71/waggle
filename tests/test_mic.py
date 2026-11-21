"""The microphone helpers: the energy endpointer, the file source used in place of the microphone, WAV helpers.
No test here opens the real microphone."""
import array
import math
import random
import wave

from waggle import mic


def frames_of(seconds, amp=0, freq=220.0, noise=0, seed=1):
    """`seconds` of 30 ms frames: a sine of amplitude `amp` plus uniform noise of `noise`."""
    rng = random.Random(seed)
    n = int(seconds * 1000 / mic.FRAME_MS)
    out, i = [], 0
    for _ in range(n):
        s = array.array("h")
        for _ in range(mic.FRAME_SAMPLES):
            v = amp * math.sin(2 * math.pi * freq * i / mic.RATE) + (rng.uniform(-noise, noise) if noise else 0)
            s.append(max(-32768, min(32767, int(v))))
            i += 1
        out.append(s.tobytes())
    return out


def run(ep, frames):
    marks = []
    for n, f in enumerate(frames):
        r = ep.feed(f)
        if r:
            marks.append((r, n))
        if r == "end":
            break
    return marks


def test_level_is_rms_and_peak():
    f = frames_of(0.03, amp=1000)[0]
    rms, peak = mic.level(f)
    assert 650 < rms < 760 and 990 <= peak <= 1000
    assert mic.level(b"\x00\x00" * 10) == (0.0, 0)


def test_speech_between_silences_is_one_utterance_ending_after_the_silence():
    ep = mic.Endpointer(end_silence_s=1.2)
    marks = run(ep, frames_of(1.0, noise=10) + frames_of(1.5, amp=2000, noise=10) + frames_of(2.0, noise=10))
    assert [m[0] for m in marks] == ["start", "end"]
    start, end = marks[0][1], marks[1][1]
    assert abs(start * 30 - 1090) <= 60  # 1.0 s of silence, then 90 ms of sound to be sure
    assert abs((end - 83) * 30 - 1200) <= 60  # the speech ends at frame 83; 1.2 s of silence ends the utterance
    pcm = ep.pcm(keep_tail_s=0.3)
    assert abs(len(pcm) / 2 / mic.RATE - (0.3 + 1.5 + 0.3)) < 0.1  # pre-roll + speech + kept tail
    assert ep.speech_ms >= 1400 and ep.peak >= 1990 and ep.speech_rms() > 500


def test_a_click_is_not_speech():
    ep = mic.Endpointer()
    quiet = frames_of(1.0, noise=10)
    click = frames_of(0.03, amp=20000)
    assert run(ep, quiet + click + quiet) == [] and not ep.in_speech


def test_quiet_sound_under_the_minimum_never_starts():
    ep = mic.Endpointer(min_on=120)
    assert run(ep, frames_of(2.0, amp=100)) == []


def test_a_louder_room_raises_the_bar():
    quiet_room, loud_room = mic.Endpointer(), mic.Endpointer()
    run(quiet_room, frames_of(1.0, noise=10))
    run(loud_room, frames_of(1.0, noise=600))
    assert loud_room.on_bar() > quiet_room.on_bar() == 120.0
    assert run(loud_room, frames_of(0.5, amp=700, noise=600)) == []  # barely above the room: not speech


def test_an_utterance_is_cut_at_max_length():
    ep = mic.Endpointer(max_s=2.0)
    marks = run(ep, frames_of(0.3) + frames_of(5.0, amp=3000))
    assert marks[-1][0] == "end" and len(ep.pcm()) / 2 / mic.RATE <= 2.05


def test_the_file_source_gives_16k_mono_frames_then_silence_then_none(tmp_path):
    p = tmp_path / "stereo24k.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(array.array("h", [1000, 3000] * 24000).tobytes())  # one second, both channels
    src = mic.FileSource(p, trail_s=0.3, lead_s=0.3).open()
    got = []
    while (f := src.read(0.01)) is not None:
        got.append(f)
    assert all(len(f) == mic.FRAME_BYTES for f in got)
    pcm = b"".join(got)
    assert abs(len(pcm) / 2 / mic.RATE - 1.6) < 0.04
    s = mic.samples(pcm)
    assert s[0] == 0 and s[int(0.5 * mic.RATE)] == 2000 and s[-1] == 0  # mono = the mean of the channels


def test_wav_helpers_round_trip(tmp_path):
    pcm = frames_of(0.3, amp=500)
    data = b"".join(pcm)
    p = mic.write_wav(tmp_path / "a.wav", data)
    with wave.open(str(p)) as w:
        assert (w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())) == (16000, 1, data)
    assert mic.wav_bytes(data)[:4] == b"RIFF"
    r = mic.report(data)
    assert r["frames"] == 10 and r["peak"] <= 500 and r["rms_median"] > 300


def test_a_steady_noise_that_starts_becomes_the_floor_and_stops_counting_as_speech():
    ep = mic.Endpointer(end_silence_s=0.8, max_s=10)
    marks = run(ep, frames_of(1.0, noise=10) + frames_of(12.0, noise=600))
    assert [m[0] for m in marks] == ["start", "end"] and marks[1][1] * 30 < 8000  # ended well before max_s
    again = mic.Endpointer(end_silence_s=0.8)
    assert run(again, frames_of(10.0, noise=600, seed=3)) == []  # a room that is already noisy: no speech
