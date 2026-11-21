"""Waggle speaks first when the owner is at the PC.

When one of Waggle's own messages (a reminder, a brief, outreach, a finished background job, an answer that came
after the PC voice's holding line) is about to go out to Telegram and the owner is at the PC, its short spoken
form is said through the PC speakers and the Telegram message goes out without a notification sound. When the
owner is away, Telegram works exactly as before.

Where this lives: in the core's Telegram transport (waggle/core/transports.py calls consider() just before a row
is sent), not where messages are queued, because
  * every message, whichever part of Waggle made it, passes through that one place: notify.deliver (reminders,
    brief, evening note, outreach), background job workers that write the outbox directly, and the voice line;
  * it runs when the message really goes out: quiet hours have already held a non-urgent row back
    (not_before), the daily caps were applied when it was queued, and presence is read at that moment, not
    hours earlier when it was queued;
  * it is three lines in the transport and leaves the policy, the caps and the outbox itself untouched.

consider(row) -> the row to send: unchanged, or with meta.silent = True after the spoken form was handed to the
PC voice process through a small JSON file in <home>/pcvoice/say/ (that process synthesises and plays it between
conversations). A retry of the same row is never spoken twice (event pcvoice.handed, ref = the row id).

Nothing is spoken when: the row is a reply to the owner's own message (they are in that conversation already),
it is already silent, it is not a Telegram text or voice row, it is quiet hours, pc_voice_speak_messages is off,
the PC voice process is not running, or the owner is not at the PC (waggle/presence.py)."""
import json
import logging
import os
import time
import unicodedata
import uuid

log = logging.getLogger("waggle.pcspeak")

REPLY_ORIGINS = {"turn", "voice", "inbound"}
EVENT = "pcvoice.handed"


def own_message(row):
    """Is this row a message Waggle sends on its own (not the answer to something the owner just said)?"""
    meta = row.get("meta") or {}
    if row.get("kind") not in ("text", "voice"):
        return False
    if meta.get("speak_pc"):
        return True
    return not (meta.get("kind") == "reply" or row.get("origin") in REPLY_ORIGINS)


def spoken_form(text):
    """The words to say: no emoji or other symbols, markdown removed, at most two sentences and 35 words."""
    from waggle import voice_reply
    clean = "".join(" " if unicodedata.category(c) in ("So", "Sk", "Cs") else c for c in text or "")
    return voice_reply.cap_spoken(clean, max_sentences=2, max_words=35)


def _quiet():
    from waggle import notify
    try:
        return notify.in_quiet_hours()
    except Exception:
        return True  # cannot tell: do not speak out loud


def hand_over(row_id, text, now=None):
    """Write the spool file the PC voice process picks up. Returns its path."""
    from waggle import pcvoice
    folder = pcvoice.folder(pcvoice.SPOOL)
    p = folder / f"{int((now or time.time()) * 1000)}-{row_id}-{uuid.uuid4().hex[:6]}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": row_id, "text": text, "created": now or time.time()}), encoding="utf-8")
    os.replace(tmp, p)
    return p


def consider(row, *, present=None, running=None, quiet=None, now=None):
    """The row as it should be sent (see the module docstring). Never raises."""
    try:
        return _consider(row, present, running, quiet, now)
    except Exception as e:  # the message must go out whatever happens here
        log.warning("pc speech skipped: %s: %s", type(e).__name__, e)
        return row


def _consider(row, present, running, quiet, now):
    meta = row.get("meta") or {}
    if meta.get("silent") or not own_message(row):
        return row
    from waggle import pcvoice, presence
    if not pcvoice.setting("pc_voice_speak_messages"):
        return row
    if (quiet if quiet is not None else _quiet()):
        return row
    if not (running if running is not None else pcvoice.running()):
        return row
    if not (present if present is not None else presence.at_pc()):
        return row
    from waggle.core import events
    text = spoken_form(meta.get("spoken") or row.get("body") or "")  # a reminder carries its own spoken sentence
    if text and not events.query(EVENT, ref=row.get("id"), limit=1):
        hand_over(row.get("id"), text, now)
        events.emit(EVENT, source="pcspeak", ref=row.get("id"), now=now,
                    data={"kind": row.get("kind"), "origin": row.get("origin"), "words": len(text.split())})
    # A voice row without words (a reminder's voice note) is followed by its text row, which is the one spoken.
    return {**row, "meta": {**meta, "silent": True}}
