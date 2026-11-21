from waggle import chat_engine


def test_mark_demotes_owner_message_to_canary():
    assert chat_engine.test_marked("[waggle-test] what time is it") == (True, "what time is it")
    assert chat_engine.test_marked("  [WAGGLE-TEST]hi") == (True, "hi")
    assert chat_engine.origin_for_message("telegram", "[waggle-test] ping", env={}) == "canary"
    assert chat_engine.origin_for_message("cli", "[waggle-test] ping", env={}) == "test"  # never raised


def test_unmarked_and_mid_text_marks_stay_user():
    assert chat_engine.test_marked("hello") == (False, "hello")
    assert chat_engine.origin_for_message("telegram", "hello [waggle-test]", env={}) == "user"


def test_on_inbound_strips_mark_and_marks_test(monkeypatch):
    seen = {}

    def fake_handle(chat, text, **kw):
        seen.update(text=text, origin=kw.get("origin"))
        return chat_engine.TurnRecord(chat=chat, text=text, origin=kw.get("origin"), platform="telegram")

    monkeypatch.setattr(chat_engine, "handle_inbound", fake_handle)
    try:
        chat_engine.on_inbound({"chat": "telegram:1", "address": "1", "text": "[waggle-test] ping",
                                "platform": "telegram", "origin": "user"})
    except Exception:
        pass  # only what reached handle_inbound matters here
    assert seen == {"text": "ping", "origin": "canary"}
