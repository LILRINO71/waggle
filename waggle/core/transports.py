"""The core's own transports: Telegram text, voice notes and files.

Every attempt is evidence for the capability ledger (`telegram_text`, `telegram_voice`, `telegram_file`): a
message id returned by the Bot API records a success, any failure records a failure, so readiness always
reflects the last real attempt. This proves sending only; the round trip is the test client's job."""
from waggle.core import ledger, sender, telegram

EVIDENCE_TTL_S = 7 * 86400
CAPABILITY = {"text": "telegram_text", "voice": "telegram_voice", "file": "telegram_file"}


def _telegram(row):
    name = CAPABILITY[row["kind"]]
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
