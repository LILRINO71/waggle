"""Local-model calls that expect JSON pass a JSON schema as Ollama's `format` (waggle/ollama.py), and the parsers stay
tolerant when the model answers with something else. Fakes only: ollama._post records the payload."""
import json

import pytest

from waggle import dropbox_ingest, dropbox_tidy, gates, graph, learnloop, mailwatch, notifycheck, ollama, router, traces


class Post:
    """A fake ollama._post: records every payload, answers with canned content (or fails with an OllamaError)."""

    def __init__(self, content='{"ok": true}', fail_with=None):
        self.payloads, self.content, self.fail_with = [], content, fail_with

    def __call__(self, path, payload, timeout=300, settings=None):
        self.payloads.append(json.loads(json.dumps(payload)))   # a copy: chat() edits its payload to retry
        if self.fail_with and isinstance(payload.get("format"), dict):
            raise self.fail_with
        return {"message": {"content": self.content}}


@pytest.fixture
def post(monkeypatch):
    p = Post()
    monkeypatch.setattr(ollama, "_post", p)
    return p


SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


# ---- the one helper ----------------------------------------------------------------------------------------------

def test_a_schema_is_sent_as_the_format_with_temperature_zero(post, settings):
    assert ollama.chat_json([{"role": "user", "content": "q"}], schema=SCHEMA, settings=settings) == {"ok": True}
    sent = post.payloads[0]
    assert sent["format"] == SCHEMA and sent["options"] == {"temperature": 0} and sent["stream"] is False


def test_without_a_schema_it_is_plain_json_mode_as_before(post, settings):
    ollama.chat_json([{"role": "user", "content": "q"}], settings=settings)
    assert post.payloads[0]["format"] == "json" and "options" not in post.payloads[0]
    ollama.chat([{"role": "user", "content": "q"}], settings=settings)
    assert "format" not in post.payloads[1]


def test_a_server_that_refuses_the_schema_gets_plain_json_once(monkeypatch, settings):
    err = ollama.OllamaError("HTTP Error 400: Bad Request")
    err.status = 400
    p = Post(fail_with=err)
    monkeypatch.setattr(ollama, "_post", p)
    assert ollama.chat_json([{"role": "user", "content": "q"}], schema=SCHEMA, settings=settings) == {"ok": True}
    assert [x["format"] for x in p.payloads] == [SCHEMA, "json"]


def test_other_errors_are_not_retried(monkeypatch, settings):
    p = Post(fail_with=ollama.OllamaError("Ollama not reachable"))
    monkeypatch.setattr(ollama, "_post", p)
    with pytest.raises(ollama.OllamaError):
        ollama.chat_json([{"role": "user", "content": "q"}], schema=SCHEMA, settings=settings)
    assert len(p.payloads) == 1


@pytest.mark.parametrize("text,want", [
    ('{"ok": true}', {"ok": True}),
    ('```json\n{"ok": false}\n```', {"ok": False}),
    ('Sure, here you go: {"ok": true} hope that helps', {"ok": True}),
    ('[1, 2, 3]', [1, 2, 3]),
    ('Result:\n```\n[{"a": 1}]\n```', [{"a": 1}]),
])
def test_the_parser_stays_tolerant(text, want):
    assert ollama.parse_json(text) == want


@pytest.mark.parametrize("text", ["", "no json here", "{broken", None])
def test_nothing_parseable_is_an_ollama_error(text):
    with pytest.raises(ollama.OllamaError):
        ollama.parse_json(text)


def test_every_schema_in_the_code_is_a_valid_object_schema():
    for name, schema in [("gates", gates.LOCAL_SCHEMA), ("notifycheck", notifycheck.SCHEMA),
                         ("mailwatch", mailwatch.LOCAL_SCHEMA), ("graph", graph.EXTRACT_SCHEMA),
                         ("dropbox_ingest", dropbox_ingest.SUMMARY_SCHEMA), ("router", router.SORT_SCHEMA),
                         ("learnloop", learnloop.WORDING_SCHEMA), *[("tidy", x) for x in
                                                                    dropbox_tidy.SCHEMA_FOR.values()]]:
        assert schema["type"] == "object" and schema["required"] and set(schema["required"]) <= set(
            schema["properties"]), name
        json.dumps(schema)


# ---- the callers pass theirs --------------------------------------------------------------------------------------

def test_the_gate_question_passes_its_schema(post, settings):
    post.content = json.dumps({"wake": False, "p_needed": 0.01, "why": "newsletter"})
    out = gates._local_ask([{"role": "user", "content": "q"}], settings, 5)
    assert out["wake"] is False and post.payloads[0]["format"] == gates.LOCAL_SCHEMA


def test_the_notify_check_passes_its_schema_and_reads_a_wrapped_answer(post, settings):
    post.content = 'Here: {"decision": "no", "reason": "nothing new"}'      # the prose around it is tolerated
    out = notifycheck.decide("check CI", "all green", settings=settings)
    assert out["notify"] is False and out["by"] == "model"
    assert post.payloads[0]["format"] == notifycheck.SCHEMA


def test_an_unparseable_notify_check_answer_keeps_the_result_for_the_evening_note(post, settings):
    post.content = "I think so!"
    out = notifycheck.decide("check CI", "all green", settings=settings)
    assert out["notify"] is False and out["by"] == "failure"


def test_mail_triage_passes_its_schema_through_the_helper(post, settings):
    post.content = '```json\n{"importance": "high", "want": "you need to reply", "deadline": ""}\n```'
    out = mailwatch.ollama_triage([{"role": "user", "content": "mail"}], "gemma4", 30, settings)
    assert out["importance"] == "high"
    sent = post.payloads[0]
    assert sent["format"] == mailwatch.LOCAL_SCHEMA and sent["model"] == "gemma4" and sent["think"] is False


def test_the_drop_folder_summary_passes_its_schema(post, settings):
    post.content = json.dumps({"title": "t", "summary": ["a."], "facts": []})
    assert dropbox_ingest.ask_summary(settings, "n.txt", "text", "hello")["title"] == "t"
    assert post.payloads[0]["format"] == dropbox_ingest.SUMMARY_SCHEMA


def test_the_weekly_tidy_passes_a_schema_per_question(post, settings):
    model = dropbox_tidy.Model(settings)
    post.content = '{"groups": []}'
    assert model(dropbox_tidy.GROUP_SYSTEM, "x") == {"groups": []}
    post.content = '{"links": []}'
    model(dropbox_tidy.LINK_SYSTEM, "x")
    post.content = '{"stale": []}'
    model(dropbox_tidy.STALE_SYSTEM, "x")
    assert [p["format"] for p in post.payloads] == [dropbox_tidy.GROUP_SCHEMA, dropbox_tidy.LINK_SCHEMA,
                                                    dropbox_tidy.STALE_SCHEMA]


def test_graph_extraction_passes_its_schema(post, settings):
    traces.record("we decided on the roller shooter for the robot build this week", session="tg-7",
                  platform="telegram")
    post.content = json.dumps({"entities": [], "relations": [], "decisions": []})
    graph.extract_from_traces(since=0, settings=settings)
    assert post.payloads and post.payloads[0]["format"] == graph.EXTRACT_SCHEMA


def test_the_correction_wording_passes_its_schema(post, settings):
    post.content = json.dumps({"verdict": "unclear"})
    turn, prev = {"request": "no, my robot has four wheels", "reply": "", "time": 0}, {"request": "q", "reply": "a"}
    learnloop.word_claim(turn, prev, {"kind": "correction", "claim": "it has four wheels, the red one"}, settings,
                         chat=None)
    assert post.payloads and post.payloads[0]["format"] == learnloop.WORDING_SCHEMA


def test_an_injected_chat_still_works_without_a_schema_argument(settings):
    seen = []
    got = learnloop.word_claim({"request": "no, it is red"}, {"request": "q", "reply": "a"},
                               {"kind": "correction", "claim": "it is red, that one"}, settings,
                               chat=lambda msgs, settings=None: seen.append(msgs) or {"verdict": "unclear"})
    assert seen and got[0] in ("unclear", "fact", "style", "deferred")
