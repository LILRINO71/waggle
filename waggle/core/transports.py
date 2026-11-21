"""The core's own transports: Telegram text, voice notes and files.

Every attempt is evidence for the capability ledger (`telegram_text`, `telegram_voice`, `telegram_file`): a
message id returned by the Bot API records a success, any failure records a failure, so readiness always
reflects the last real attempt. This proves sending only; the round trip is the test client's job.

A row's `chat` may be a Telegram chat id ("123", "-100...", "@channel") or a chat key from the `chats` table
("telegram:alice", "home"); a key is sent to that chat's `address`. A key without an address cannot be
delivered and fails permanently. An empty `chat` goes to the configured home chat."""
import re

from waggle.core import ledger, sender, store, telegram

EVIDENCE_TTL_S = 7 * 86400
CAPABILITY = {"text": "telegram_text", "voice": "telegram_voice", "file": "telegram_file"}
_PLAIN_CHAT = re.compile(r"-?\d+|@\w+")


def resolve_chat(row):
    """The row with `chat` turned into a Telegram chat id; raises a permanent TelegramError when a chat key has
    no address."""
    chat = str(row.get("chat") or "").strip()
    if not chat or _PLAIN_CHAT.fullmatch(chat):
        return row
    found = store.connect().execute("SELECT address FROM chats WHERE chat=?", (chat,)).fetchone()
    address = str(found["address"] or "").strip() if found else ""
    if not _PLAIN_CHAT.fullmatch(address):
        reason = "is not in the chats table" if found is None else "has no Telegram address in the chats table"
        raise telegram.TelegramError("permanent", f"chat {chat!r} {reason}; cannot deliver")
    return {**row, "chat": address}


def _telegram(row):
    name = CAPABILITY[row["kind"]]
    from waggle import pcspeak
    row = pcspeak.consider(row)  # Waggle's own message, owner at the PC: said aloud there, sent silently
    row = resolve_chat(row)
    try:
        msg_id = telegram.send_row(row)
    except Exception as e:
        ledger.record(name, False, method="bot api send", detail=f"{type(e).__name__}: {e}"[:300])
        raise
    ledger.record(name, True, method="bot api message id", evidence_ref=f"outbox:{row['id']} msg:{msg_id}",
                  ttl_s=EVIDENCE_TTL_S)
    return msg_id


def register_core():
    for kind in CAPABILITY:
        sender.register(kind, _telegram)
