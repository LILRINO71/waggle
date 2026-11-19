from waggle import screen, security


def test_wants_look():
    for t in ["what's this error on my screen?", "read my screen", "What am I looking at",
              "look at the window", "can you see this?", "take a screenshot"]:
        assert screen.wants_look(t), t
    for t in ["weather tomorrow", "screen time tips for teens?", "call me in 5"]:
        assert not screen.wants_look(t), t


def test_look_and_context_use_injected_parts(tmp_path):
    img = tmp_path / "s.png"
    img.write_bytes(b"png")
    seen = []
    out = screen.context("what error?", capturer=lambda: img,
                         describer=lambda i, q, s: seen.append((i, q)) or "VS Code: NameError on line 4")
    assert "NameError on line 4" in out and 'source="screen"' in out
    assert seen == [(img, "what error?")]


def test_context_reports_failure():
    def boom():
        raise RuntimeError("no display")
    assert "Could not look" in screen.context("x", capturer=boom)


def test_describe_sends_image_to_vision_model(tmp_path):
    img = tmp_path / "s.png"
    img.write_bytes(b"\x89PNG")
    bodies = []
    screen.describe(img, "what?", post=lambda b: bodies.append(b) or "ok")
    msg = bodies[0]["messages"][1]
    assert bodies[0]["model"] == "gemma4" and msg["images"] == ["iVBORw=="] and msg["content"] == "what?"


def test_screen_control_guardrails():
    ok = lambda a: security.check_tool_call("computer_use", a) is None  # noqa: E731
    assert ok({"action": "capture", "app": "Chase Bank"})
    assert ok({"action": "click", "app": "Visual Studio Code", "element": 4})
    assert ok({"action": "type", "text": "hello world"})
    assert not ok({"action": "click", "app": "PayPal"})
    assert not ok({"action": "type", "text": "4111 1111 1111 1111"})
    assert not ok({"action": "type", "text": "my password is hunter2"})
    assert not ok({"action": "focus_app", "app": "Outlook"})
    assert ok({"action": "focus_app", "app": "Obsidian"})
