"""Waggle speaks first when the owner is at the PC: the transport hands the spoken form of Waggle's own messages
to the PC voice and sends them silently; replies, quiet hours, an absent owner or no PC voice change nothing."""
import json

import pytest

from waggle import pcspeak, pcvoice
from waggle.core import events, outbox, sender, telegram, transports


def row(origin="reminder", body="⏰ Stand up and stretch, sir.", kind="text", rid=7, meta=None):
    return {"id": rid, "kind": kind, "chat": "123", "body": body, "origin": origin, "file": None,
            "meta": dict(meta or {})}


def spooled():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pcvoice.folder(pcvoice.SPOOL).glob("*.json"))]


HERE = dict(present=True, running=True, quiet=False)


@pytest.mark.parametrize("origin", ["reminder", "brief", "evening", "background", "initiative", "scout", "autowork"])
def test_own_messages_are_spoken_and_sent_silently_when_the_owner_is_here(origin):
    out = pcspeak.consider(row(origin=origin), **HERE)
    assert out["meta"]["silent"] is True
    assert [s["text"] for s in spooled()] == ["Stand up and stretch, sir."]  # no emoji in the spoken form
    assert events.query(pcspeak.EVENT, ref=7, limit=5)


@pytest.mark.parametrize("r", [row(origin="turn", meta={"kind": "reply"}), row(origin="voice"),
                               row(origin="inbound"), row(origin="brief", meta={"kind": "reply"}),
                               row(kind="push"), row(kind="call"), row(meta={"silent": True})])
def test_replies_other_kinds_and_already_silent_rows_are_left_alone(r):
    assert pcspeak.consider(r, **HERE) == r and spooled() == []


def test_an_answer_after_the_pc_holding_line_is_spoken_even_though_it_is_a_reply():
    r = row(origin="voice", meta={"kind": "reply", "speak_pc": True}, body='Heard: "x"\n\nThe build passed.')
    assert pcspeak.consider(r, **HERE)["meta"]["silent"] is True and len(spooled()) == 1


@pytest.mark.parametrize("why", [dict(present=False, running=True, quiet=False),
                                 dict(present=True, running=False, quiet=False),
                                 dict(present=True, running=True, quiet=True)])
def test_away_no_pc_voice_or_quiet_hours_means_telegram_as_today(why):
    r = row()
    assert pcspeak.consider(r, **why) == r and spooled() == []


def test_the_setting_switches_it_off(settings):
    from waggle import config
    data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    data["pc_voice_speak_messages"] = False
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    r = row()
    assert pcspeak.consider(r, **HERE) == r and spooled() == []


def test_a_retry_of_the_same_row_is_not_spoken_twice():
    pcspeak.consider(row(), **HERE)
    out = pcspeak.consider(row(), **HERE)
    assert out["meta"]["silent"] is True and len(spooled()) == 1


def test_a_voice_note_without_words_is_sent_silently_and_not_spoken():
    out = pcspeak.consider(row(kind="voice", body=""), **HERE)
    assert out["meta"]["silent"] is True and spooled() == []


def test_the_spoken_form_is_short():
    long = "First point here. Second point here. Third point that is not said. " + "word " * 50
    said = pcspeak.spoken_form("🐝 **" + long)
    assert said == "First point here. Second point here."


def test_an_error_inside_never_stops_the_message(monkeypatch):
    monkeypatch.setattr(pcspeak, "hand_over", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = row()
    assert pcspeak.consider(r, **HERE) == r


def test_the_real_checks_default_to_not_speaking_without_a_pc_voice_process(monkeypatch):
    monkeypatch.setattr(pcspeak, "_quiet", lambda: False)
    r = row()
    assert pcspeak.consider(r) == r  # no PC voice process holds the lock in a test home


def test_the_core_transport_sends_waggles_own_message_silently_when_spoken(monkeypatch, settings):
    from waggle import presence
    monkeypatch.setattr(pcvoice, "running", lambda: True)
    monkeypatch.setattr(presence, "at_pc", lambda *a, **k: True)
    monkeypatch.setattr(pcspeak, "_quiet", lambda: False)
    sent = []
    monkeypatch.setattr(telegram, "send_row", lambda r, api=None: sent.append(r) or "555")
    transports.register_core()
    reminder = outbox.enqueue("text", "⏰ Call the dentist", chat="123", origin="reminder", meta={"asked": True})
    reply = outbox.enqueue("text", "Here is the answer.", chat="123", origin="turn", meta={"kind": "reply"})
    assert sender.send_one() == "sent" and sender.send_one() == "sent"
    by_body = {r["body"]: r for r in sent}
    assert by_body["⏰ Call the dentist"]["meta"]["silent"] is True
    assert "silent" not in by_body["Here is the answer."]["meta"]
    assert [s["text"] for s in spooled()] == ["Call the dentist"]
    assert outbox.get(reminder)["state"] == "sent" and outbox.get(reply)["state"] == "sent"
