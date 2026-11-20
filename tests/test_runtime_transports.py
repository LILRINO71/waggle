import pytest

from waggle.core import ledger, outbox, sender, telegram, transports


@pytest.fixture(autouse=True)
def _own_transports(monkeypatch):
    monkeypatch.setattr(sender, "_transports", {})
    transports.register_core()


def test_confirmed_send_marks_the_row_sent_and_records_evidence(monkeypatch):
    monkeypatch.setattr(telegram, "send_row", lambda row, api=None: "4242")
    rid = outbox.enqueue("text", "hello", origin="test")
    assert sender.send_one() == "sent"
    assert outbox.get(rid)["provider_msg_id"] == "4242"
    assert ledger.ready("telegram_text") and not ledger.ready("telegram_voice")


def test_a_failed_send_after_a_success_makes_the_channel_not_ready(monkeypatch):
    monkeypatch.setattr(telegram, "send_row", lambda row, api=None: "1")
    outbox.enqueue("text", "a", origin="test")
    assert sender.send_one() == "sent"

    def down(row, api=None):
        raise telegram.TelegramError("retryable", "network down")
    monkeypatch.setattr(telegram, "send_row", down)
    rid = outbox.enqueue("text", "b", origin="test")
    assert sender.send_one() == "queued"
    assert outbox.get(rid)["state"] == "queued" and not ledger.ready("telegram_text")


def test_registers_text_voice_and_file_only():
    assert sender.kinds() == ["file", "text", "voice"]


def test_the_real_telegram_is_unreachable_from_a_test_run():
    api = telegram.BotApi(token="123456:fake-token-for-tests")
    with pytest.raises(telegram.TelegramError) as e:
        api.get_me()
    assert e.value.permanent and "off limits" in str(e.value)
    assert not telegram.real_api_allowed()


def test_the_real_telegram_is_unreachable_from_a_temporary_home(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert not telegram.real_api_allowed()
