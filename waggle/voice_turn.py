"""Voice notes from Telegram (or any Hermes platform) arrive as one quoted transcript plus a media marker:

    "what's the next thing on the bio bus robot"
    [User sent audio: C:\\...\\audio_1234.ogg]

parse() turns that back into what the user said, fixes known mishearings, and says whether it was a voice turn,
so the answering model knows the words came through speech recognition and should be read for intent. A failed
transcription becomes a clear "say it again" instead of the model guessing from a file path.
"""
import re

NOTE = re.compile(r'"(?P<t>.*?)"\s*\n?\s*\[User sent audio: [^\]\n]*\]', re.S)
FAILED = re.compile(r"\[voice message could not be transcribed[^\]]*\]", re.I)
# Mishearings that are safe to fix everywhere (FTC vocabulary). Personal ones (a robot's name) live in
# config.json as voice_fixes, so the public repo holds no personal data.
DEFAULT_FIXES = {
    r"\bethics robotics\b": "FTC robotics",
    r"\bf\.? t\.? c\.?(?=\s|$)": "FTC",
    r"\bon ?shape\b": "Onshape",
    r"\bgo ?bilda\b|\bgo ?bild[ae]r\b": "goBILDA",
    r"\btele ?op\b": "TeleOp",
}

VOICE_HINT = ("<voice_message>The user said this out loud; it reached you through speech recognition, so names and "
              "terms can be misheard (a project or robot name can come out as similar-sounding words, and "
              "\"ethics\" next to robots or a team usually means \"FTC\"). Work out what "
              "they meant from the conversation and their notes; ask one short question only if it is genuinely "
              "unclear. Your reply may be read aloud, so write natural spoken sentences: lead with the answer, keep "
              "it short unless they asked for detail, and leave out tables, code blocks and file paths unless they "
              "asked for them.</voice_message>")
FAILED_REPLY = ("I couldn't make out that voice message: speech recognition failed on it. Could you send it again "
                "(a bit closer to the mic helps) or type it?")


def fix(text, fixes=None):
    for pat, repl in {**DEFAULT_FIXES, **(fixes or {})}.items():
        try:
            text = re.sub(pat, repl, text, flags=re.I)
        except re.error:
            continue
    return text


def parse(message, fixes=None):
    """(text, kind): kind is "text", "voice" or "voice_failed"."""
    msg = message or ""
    if FAILED.search(msg) and not NOTE.search(msg):
        return FAILED.sub("", msg).strip(), "voice_failed"
    notes = NOTE.findall(msg)
    if not notes:
        return msg, "text"
    caption = NOTE.sub("", msg).strip()
    spoken = " ".join(t.strip() for t in notes if t.strip())
    text = fix(spoken, fixes)
    return (f"{caption}\n\n{text}".strip() if caption else text), "voice"
