"""Manual check (not collected by pytest): a voice note answered end to end WITHOUT Telegram, with the real speech
server (127.0.0.1:8899), the real ffmpeg, the real local speech-to-text and the real `claude` CLI, in a throwaway
Waggle home, into a delivery that collects the voice file and the text.

usage: python tests/real_voice_check.py <repo> <empty_temp_home> "<what the sample note says>" [more notes ...]
       (needs `python -m waggle.guard approve --yes` in the repo for this WAGGLE_HOME for a run with tools)
Each note is one real Claude call. The sample note is made with the speech server and ffmpeg (no personal audio).
Prints one JSON line per note: the stage times, the spoken text, the audio length and size, the text sent.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

repo, home = Path(sys.argv[1]), Path(sys.argv[2])
notes = sys.argv[3:] or ["Remind me to stand up in ten seconds."]
os.environ["WAGGLE_HOME"] = str(home)
os.environ["WAGGLE_ORIGIN"] = "test"
sys.path.insert(0, str(repo))

from waggle.config import Settings  # noqa: E402

work = home / "work"
work.mkdir(parents=True, exist_ok=True)
s = Settings(vault=str(home / "vault"), jobs_dir=str(home / "jobs"), agent_workdir=str(work), quiet_hours=[0, 0],
             chat_model="haiku")
s.save()

from waggle import chat_engine, traces, voice_reply  # noqa: E402
from waggle.chat_session import ChatSession  # noqa: E402
from waggle.delivery import Delivery  # noqa: E402

out_dir = home / "collected"
out_dir.mkdir(exist_ok=True)


class VoiceCollect(Delivery):
    can_edit, can_voice, max_len, name = False, True, 4096, "collect-voice"

    def __init__(self):
        self.t0, self.log = time.time(), []

    def post(self, text):
        self.log.append(("text", round(time.time() - self.t0, 2), text))
        return len(self.log)

    def send_voice(self, path, caption="", duration=None):
        dest = out_dir / f"voice_{len(self.log) + 1}.ogg"
        shutil.copy(path, dest)
        self.log.append(("voice", round(time.time() - self.t0, 2), str(dest), duration))
        return len(self.log)


def sample_note(text, n):
    """A sample voice note: the speech server's WAV converted to Ogg/Opus, like Telegram's."""
    wav, ogg = out_dir / f"sample_{n}.wav", out_dir / f"sample_{n}.ogg"
    voice_reply.synth_wav(text, wav, "bm_george", 1.0, 30)
    voice_reply.to_ogg(wav, ogg, 30)
    return ogg


sess = ChatSession("test:real-voice", settings=s, transcript=traces.chat_transcript)
sess.origin = "test"
for n, text in enumerate(notes, 1):
    audio = sample_note(text, n)
    d = VoiceCollect()
    t0 = time.time()
    rec = chat_engine.handle_inbound("test:real-voice", "", kind="voice", audio=str(audio), platform="test",
                                     origin="test", delivery=d, settings=s, session=sess, t0=t0)
    v = rec.voice
    voice_files = [e for e in d.log if e[0] == "voice"]
    texts = [e for e in d.log if e[0] == "text"]
    ogg = Path(voice_files[0][2]) if voice_files else None
    print(json.dumps({
        "note": text, "transcript": rec.text, "outcome": rec.outcome, "why": rec.outcome_why,
        "stt_s": v.get("stt_s"), "stt_load_s": v.get("stt_load_s"), "model_s": v.get("model_s"),
        "synth_s": v.get("synth_s"), "convert_s": v.get("convert_s"), "audio_s_reported": v.get("audio_s"),
        "audio_s_ffmpeg": voice_reply.ogg_seconds(ogg) if ogg else None, "ogg_bytes": ogg.stat().st_size if ogg else None,
        "voice_sent_at_s": voice_files[0][1] if voice_files else None, "text_sent_at_s": texts[0][1] if texts else None,
        "total_s": rec.total_s, "voice_error": v.get("voice_error"), "spoken": v.get("spoken"),
        "spoken_from_model": v.get("spoken_from_model"), "steps": [x.get("tool") for x in rec.steps],
        "text": rec.reply, "model": rec.model}, ensure_ascii=False), flush=True)
sess.stop() if hasattr(sess, "stop") else None
