"""doc_to_pdf (waggle/tools_docs.py): Obsidian cleanup, the renderers, Office/Chrome plumbing with fakes."""
import json
from pathlib import Path

import pytest
from waggle import policy
from waggle import tools_common as tc
from waggle import tools_docs

NOTE = """---
tags: [robotics]
---
# BIOBUZZ strategy

See [[Robot V2|the robot]] and [[Shooter#tuning]] and ![[diagram.png]]. %%private comment%% ==key idea==

- [ ] tune shooter
- [x] build intake

> [!warning] Heads up
> check the **hive** cell

```python
print("[[not a link]]")
```
"""


@pytest.fixture
def writable(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "writable_roots", lambda settings=None: [policy.norm(tmp_path)])
    return tmp_path


class FakeOutput:
    """Replaces the browser and Office exporters: each writes a believable PDF and records what it was asked."""

    def __init__(self, monkeypatch):
        self.html, self.office = [], []
        monkeypatch.setattr(tools_docs, "html_to_pdf", self.to_pdf)
        monkeypatch.setattr(tools_docs, "office_to_pdf", self.from_office)

    def to_pdf(self, page, pdf, timeout=90):
        self.html.append(Path(page).read_text(encoding="utf-8"))
        Path(pdf).write_bytes(b"%PDF-1.4 " + b"x" * 300)

    def from_office(self, kind, src, pdf, timeout=120):
        self.office.append((kind, Path(src).name))
        Path(pdf).write_bytes(b"%PDF-1.4 " + b"x" * 300)


def test_obsidian_syntax_is_cleaned():
    text = tools_docs.clean_obsidian(NOTE)
    assert "tags:" not in text and "private comment" not in text
    assert "the robot" in text and "[[" not in text.split("```")[0]
    assert "(embedded: diagram.png)" in text and "Shooter" in text and "#tuning" not in text
    assert "☐ tune shooter" in text and "☑ build intake" in text
    assert "> **Heads up**" in text and "<mark>key idea</mark>" in text
    assert 'print("[[not a link]]")' in text  # a code fence is left exactly as written


def test_both_markdown_renderers_make_real_html():
    body = tools_docs.mini_markdown("# T\n\nA *b* and **c** with `d` and [e](http://x.y)\n\n- one\n- two\n\n1. a\n\n> q\n\n```\nx < y\n```")
    for frag in ("<h1>T</h1>", "<em>b</em>", "<strong>c</strong>", "<code>d</code>", '<a href="http://x.y">e</a>',
                 "<ul>", "<li>two</li>", "<ol>", "<blockquote>", "x &lt; y"):
        assert frag in body, frag
    full = tools_docs.render_markdown("# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1>" in full and ("<table>" in full or "| a | b |" in full)  # the table renders when `markdown` is installed


def test_markdown_text_becomes_a_pdf_in_the_tool_output_folder(writable, isolated_home, monkeypatch):
    fake = FakeOutput(monkeypatch)
    out = json.loads(tools_docs.doc_to_pdf({"markdown": "# Match plan\n\nShoot first.", "name": "Match: plan?"}))
    assert out["ok"] and out["title"] == "Match plan"
    pdf = Path(out["output"]["path"])
    assert pdf.parent == Path(isolated_home.jobs_dir) / "tool-output" and pdf.name == "Match- plan-.pdf"
    assert "<title>Match plan</title>" in fake.html[0] and "Shoot first." in fake.html[0]
    again = json.loads(tools_docs.doc_to_pdf({"markdown": "# Match plan\n\nShoot first.", "name": "Match: plan?"}))
    assert Path(again["output"]["path"]).name == "Match- plan- (2).pdf"


def test_a_note_is_rendered_next_to_itself(writable, monkeypatch):
    note = writable / "Strategy.md"
    note.write_text(NOTE, encoding="utf-8")
    fake = FakeOutput(monkeypatch)
    out = json.loads(tools_docs.doc_to_pdf({"path": str(note)}))
    assert out["ok"] and Path(out["output"]["path"]) == writable / "Strategy.pdf" and out["via"] == "Chrome"
    assert "<title>BIOBUZZ strategy</title>" in fake.html[0] and "the robot" in fake.html[0]
    assert Path(writable).as_uri() in fake.html[0]  # <base> so relative images still resolve


def test_office_files_go_to_the_matching_office_app(writable, monkeypatch):
    fake = FakeOutput(monkeypatch)
    for name, kind in [("r.docx", "word"), ("r.rtf", "word"), ("s.xlsx", "excel"), ("s.csv", "excel"), ("d.pptx", "powerpoint")]:
        f = writable / name
        f.write_text("x")
        out = json.loads(tools_docs.doc_to_pdf({"path": str(f)}))
        assert out["ok"] and out["source"] == kind, out
    assert [k for k, _ in fake.office] == ["word", "word", "excel", "excel", "powerpoint"]


def test_unsupported_and_empty_requests(writable, monkeypatch):
    FakeOutput(monkeypatch)
    f = writable / "a.xyz"
    f.write_text("x")
    assert "not supported" in json.loads(tools_docs.doc_to_pdf({"path": str(f)}))["error"]
    assert "no input" in json.loads(tools_docs.doc_to_pdf({}))["error"]
    assert "empty" in json.loads(tools_docs.doc_to_pdf({"markdown": "  "}))["error"]


def test_office_script_never_quits_an_office_it_did_not_start():
    ps = tools_docs.PS_SCRIPT
    assert "$ours" in ps and "if ($ours) { try { $app.Quit() }" in ps  # quits only an Office process that appeared with us
    assert ps.count("$app.Quit()") == 1
    assert "$doc.Close($false)" in ps and "$wb.Close($false)" in ps  # only the document we opened is closed


def test_office_pid_listing_reads_the_first_column_pair_only(monkeypatch):
    out = '"WINWORD.EXE","31320","Console","1","120,000 K"\n"WINWORD.EXE","18052","Console","1","90,000 K"\n'
    monkeypatch.setattr(tc, "run", lambda cmd, **kw: (0, out, ""))
    assert tools_docs._office_pids("word") == {31320, 18052}
    monkeypatch.setattr(tc, "run", lambda cmd, **kw: (0, "INFO: No tasks are running which match the criteria.\n", ""))
    assert tools_docs._office_pids("word") == set()


def test_office_timeout_kills_only_processes_it_started(writable, monkeypatch):
    killed = []
    pids = iter([{100}, {100, 200}])
    monkeypatch.setattr(tools_docs, "_office_pids", lambda kind: next(pids))
    monkeypatch.setattr(tc, "kill_tree", lambda pid: killed.append(pid))

    def hang(cmd, timeout=120, **kw):
        import subprocess
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(tc, "run", hang)
    with pytest.raises(tc.ToolError, match="did not finish"):
        tools_docs.office_to_pdf("word", writable / "a.docx", writable / "a.pdf", timeout=1)
    assert killed == [200]


@pytest.mark.skipif(not tc.which("chrome", "msedge", extra=tools_docs.BROWSER_GLOBS), reason="no Chrome or Edge")
def test_real_browser_prints_a_note(writable):
    note = writable / "n.md"
    note.write_text(NOTE, encoding="utf-8")
    out = json.loads(tools_docs.doc_to_pdf({"path": str(note)}))
    assert out["ok"]
    assert Path(out["output"]["path"]).read_bytes().startswith(b"%PDF")
