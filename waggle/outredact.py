"""Nothing that looks like a credential leaves Waggle for the owner, whoever wrote the text.

scrub(text, channel) is the one call every outgoing channel makes at its lowest point (Telegram client, outbox,
ntfy, speech, phone call, the stream writer, the engine's reply). It replaces credential-looking strings with
[REDACTED:<kind>] (waggle.policy.redact) and counts each redaction as an event of kind "redacted_outbound" with the
channel and the kinds, never the secret. It fails closed: if the patterns cannot load, the send fails.
Counting is best effort and never stops a message.
"""
import logging
import threading
import time

log = logging.getLogger("waggle.outredact")

EVENT = "redacted_outbound"
THROTTLE_S = 5.0   # the same channel, chat and kinds are counted once per window (a stream is scrubbed on every chunk)
_recent = {}
_lock = threading.Lock()


def scrub(text, channel, *, chat="", ref="", once=None):
    """`text` with credentials replaced. A redaction is recorded under `channel` ("telegram", "outbox", "ntfy",
    "tts", "call", "stream", "reply", ...). once: a set the caller keeps; a channel already in it is not counted
    again (a streamed reply is redacted on every edit but is one event)."""
    if not text or not isinstance(text, str):
        return text
    from waggle import policy
    out, kinds = policy.redact_report(text)
    if kinds and not (once is not None and channel in once):
        if once is not None:
            once.add(channel)
        _count(channel, kinds, chat, ref)
    return out


def _fresh(channel, kinds, chat):
    key = (channel, str(chat or ""), tuple(sorted(set(kinds))))
    now = time.monotonic()
    with _lock:
        if now - _recent.get(key, -1e9) < THROTTLE_S:
            return False
        _recent[key] = now
        if len(_recent) > 256:
            for k in [k for k, t in _recent.items() if now - t >= THROTTLE_S]:
                del _recent[k]
    return True


def _count(channel, kinds, chat, ref):
    if not _fresh(channel, kinds, chat):
        return
    data = {"channel": channel, "kinds": sorted(set(kinds)), "count": len(kinds)}
    try:
        from waggle.core import events
        events.emit(EVENT, source="outredact", chat=str(chat or ""), ref=str(ref or ""), data=data)
    except Exception as e:  # noqa: BLE001 - counting must never stop a message
        log.warning("redaction event not recorded: %s: %s", type(e).__name__, e)
    try:
        from waggle import events as jsonl
        jsonl.log(EVENT, **data)
    except Exception as e:  # noqa: BLE001
        log.warning("redaction log not written: %s: %s", type(e).__name__, e)
