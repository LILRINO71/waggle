"""claude_cli.StreamParser: Claude Code stream-json, including the lines real processes get wrong."""
import json

from engine_fakes import ev_init, ev_result, ev_text, ev_tool

from waggle import claude_cli
from waggle.claude_cli import StreamParser, error_kind


def feed_all(parser, events):
    done = False
    for ev in events:
        done = parser.feed(json.dumps(ev)) or done
    return done


def test_streamed_text_is_kept_once_and_reported_as_it_grows():
    seen = []
    p = StreamParser(on_text=seen.append, clock=iter(range(100, 200)).__next__)
    done = feed_all(p, [ev_init("s1"), *ev_text("s1", "Hello there, friend.", pieces=4)])
    assert not done and p.text == "Hello there, friend."  # the whole assistant message did not add it twice
    assert len(seen) == 4 and seen[-1] == "Hello there, friend." and seen[0] != seen[-1]
    assert p.first_text_at is not None and p.session_id == "s1" and p.model == "claude-haiku-5-5"
    assert feed_all(p, [ev_result("s1", "Hello there, friend.")])
    assert p.error() is None and p.main_model() == "claude-haiku-5-5"


def test_message_without_partial_events_is_taken_whole_and_messages_are_joined():
    p = StreamParser()
    feed_all(p, ev_text("s", "First part.", msg_id="m1"))
    feed_all(p, ev_tool("s", "Glob", {"pattern": "*.py"}, "a.py\nb.py"))
    p.feed(json.dumps({"type": "assistant", "session_id": "s",
                       "message": {"id": "m2", "content": [{"type": "text", "text": "Second part."}]}}))
    assert p.text == "First part.\n\nSecond part."


def test_tool_calls_record_outcome_redacted_and_truncated():
    secret = "sk-ant-" + "A" * 30
    p = StreamParser(on_tool=lambda step: started.append(step["tool"]))
    started = []
    feed_all(p, ev_tool("s", "Bash", {"command": f"echo {secret}"}, "x" * 900 + secret, tool_id="t1"))
    feed_all(p, ev_tool("s", "Read", {"file_path": "notes.md"}, "File does not exist.", is_error=True, tool_id="t2",
                        msg_id="m2"))
    feed_all(p, ev_tool("s", "Write", {"file_path": "x"}, "NEEDS_APPROVAL: writing outside the allowed folders",
                        is_error=True, tool_id="t3", msg_id="m3"))
    bash, read, write = p.steps
    assert started == ["Bash", "Read", "Write"]
    assert bash["ok"] is True and len(bash["result"]) == claude_cli.TOOL_RESULT_CAP and secret not in bash["input"]
    assert read["ok"] is False and not read["denied"] and read["result"] == "File does not exist."
    assert write["denied"] is True


def test_tool_result_with_block_list_content():
    p = StreamParser()
    feed_all(p, ev_tool("s", "WebFetch", {"url": "https://example.com"},
                        [{"type": "text", "text": "page"}, {"type": "text", "text": "body"}]))
    assert p.steps[0]["result"] == "page body" and p.steps[0]["ok"] is True


def test_subagent_events_are_ignored():
    p = StreamParser()
    ev = ev_text("s", "inner thoughts")[-1]
    ev["parent_tool_use_id"] = "toolu_9"
    p.feed(json.dumps(ev))
    assert p.text == "" and p.steps == []


def test_error_results_by_kind():
    cases = [
        (ev_result("s", "Claude AI usage limit reached|1763600000", is_error=True), "limit"),
        (ev_result("s", "You've hit your limit · resets 3pm", is_error=True), "limit"),
        (ev_result("s", "Invalid API key · Please run /login", is_error=True), "auth"),
        (ev_result("s", "OAuth token has expired", is_error=True), "auth"),
        (ev_result("s", "", is_error=True, subtype="error_during_execution"), "error"),
        (ev_result("s", "Overloaded", is_error=True, api_error_status=429), "limit"),
        (ev_result("s", "", subtype="error_max_turns"), "error"),
    ]
    for ev, kind in cases:
        p = StreamParser()
        assert p.feed(json.dumps(ev)) is True
        assert p.error()[0] == kind, ev
        assert p.text == ""  # an error result's text is never shown as the answer


def test_no_result_is_an_error():
    p = StreamParser()
    feed_all(p, ev_text("s", "half an answer"))
    assert p.error() == ("error", "no result")


def test_result_only_text_is_used_when_nothing_streamed():
    p = StreamParser()
    p.feed(json.dumps(ev_result("s", "Just the result.")))
    assert p.text == "Just the result." and p.error() is None


def test_garbage_lines_never_break_the_parser():
    p = StreamParser()
    for line in ["", "   ", "Warning: something on stdout", "[1, 2]", "123", '{"type": "stream_event", "event": "x"}',
                 '{"type": "assistant", "message": {"content": ["text"]}}', '{"type": "result", "subtype"',
                 '{"type": "user", "message": {"content": "plain string"}}', "\x00\x01binary"]:
        assert p.feed(line) is False
    assert p.garbage >= 5
    feed_all(p, ev_text("s", "Still works."))
    assert p.text == "Still works."


def test_partial_lines_are_buffered_across_chunks():
    p = StreamParser()
    raw = "".join(json.dumps(ev) + "\n" for ev in [*ev_text("s", "Split across chunks."), ev_result("s", "x")])
    done = [p.feed_chunk(raw[i:i + 7]) for i in range(0, len(raw), 7)]
    assert done[-1] is True and p.text == "Split across chunks." and p.garbage == 0


def test_error_kind_words():
    assert error_kind("Please run /login") == "auth"
    assert error_kind("5-hour limit reached") == "limit"
    assert error_kind("rate_limit_error") == "limit"
    assert error_kind("ECONNRESET") == "error"
    assert error_kind("") == "error"


def test_stream_one_shot_with_a_fake_process():
    from engine_fakes import FakeClaude
    fake = FakeClaude()

    class OneShot:
        def __init__(self, cmd, **kw):
            self.proc = fake(cmd + ["--session-id", "one"], **kw)
            self.stdin = self
            self.stdout = self.proc.stdout
            self.buf = ""

        def write(self, text):
            self.buf += text

        def close(self):
            self.proc.on_input(json.dumps({"message": {"content": [{"text": self.buf}]}}))
            self.proc.stdout.eof()

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    shown = []
    out = claude_cli.stream("hi", model="haiku", on_text=shown.append, popen=OneShot)
    assert out["text"] == "Hello from Claude." and out["model_id"] == "claude-haiku-5-5" and shown
    assert out["ttfw_s"] is not None and "--include-partial-messages" in fake.procs[0].cmd


def test_real_tool_turn_recorded_from_claude_code_2_1():
    """A tool turn captured from the real CLI (session id replaced, usage trimmed): the assistant event arrives
    before content_block_stop and carries text that was already streamed."""
    from pathlib import Path
    seen = []
    p = StreamParser(on_text=seen.append)
    lines = (Path(__file__).parent / "fixtures" / "claude_stream_tool_turn.jsonl").read_text(encoding="utf-8")
    done = [p.feed(line) for line in lines.splitlines()]
    assert done[-1] is True and sum(done) == 1 and p.garbage == 0
    assert p.text == "There are 3 .txt files in the current folder: alpha.txt, beta.txt, and gamma.txt."
    assert p.steps == [{"tool": "Glob", "id": p.steps[0]["id"], "input": '{"pattern": "*.txt"}', "ok": True,
                        "denied": False, "result": "alpha.txt\nbeta.txt\ngamma.txt"}]
    assert p.error() is None and p.main_model() == "claude-haiku-5-5" and len(seen) == 5
