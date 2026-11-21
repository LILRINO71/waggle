"""Plug-in that tests/test_voiceline_core.py loads into a real core process (WAGGLE_CORE_PLUGINS): the real turn
engine with a fake `claude` process and a fake speech server, and a fake Telegram transport that logs what the
core's sender delivers. Every effect is appended to a file in the (temporary) Waggle home. No tests live here."""
import math
import struct
import time
import wave
from pathlib import Path

import waggle.config as config


def _log(name, line):
    with open(Path(config.HOME) / name, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _tone(text, out, voice, speed, timeout):
    _log("speech.log", text)
    rate = 24000
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(rate * 0.8))))
    return 0.8


def _transport(row):
    _log("sent.log", f"{row['id']}\t{row['chat']}\t{row['body'].replace(chr(10), '|')}")
    return f"fake-{row['id']}"


def register_core():
    from engine_fakes import FakeClaude

    from waggle import chat_engine, claude_cli, voice_reply
    from waggle.chat_session import ChatSession, SessionPool
    from waggle.config import Settings
    from waggle.core import inbound, sender

    claude_cli.find_claude = lambda: None  # what tests/conftest.py does in-process: never the real Claude CLI
    claude_cli.mcp_servers = lambda claude=None, refresh=False: []
    s = Settings.load()

    def script(proc, text):
        _log("prompts.log", text.replace("\n", "|")[-200:])
        if "SLOWTURN" in text:
            time.sleep(2.5)
        proc.answer("SPOKEN: The reminder is set, sir.\nANSWER:\nReminder set for ten minutes from now.")
    fake = FakeClaude(script)
    chat_engine.set_pool(SessionPool(factory=lambda chat: ChatSession(chat, settings=s, popen=fake, claude="claude"),
                                     settings=s))
    chat_engine.build_context = lambda *a, **k: ""
    chat_engine.claude_available = lambda settings: (True, "")
    voice_reply.synth_wav = _tone
    inbound.register(lambda msg: {})
    sender.register("text", _transport)
