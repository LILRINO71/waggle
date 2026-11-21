"""How the real-world tools are wired in: MCP registration, policy rows, skills and README, removal of a pack."""
import json
import re
from pathlib import Path

from waggle import policy, skills, tools_ext
from waggle import waggle_mcp as mcp

ROOT = Path(__file__).resolve().parent.parent
TOOLS = {"cad_convert", "cad_measure", "print_estimate", "doc_to_pdf", "media_convert", "image_convert",
         "portfolio_audit", "explain_file"}
SKILL_TOOLS = {"cad-files": {"cad_convert", "cad_measure", "print_estimate"}, "documents-to-pdf": {"doc_to_pdf"},
               "media-convert": {"media_convert", "image_convert"}, "github-portfolio-audit": {"portfolio_audit"},
               "explain-this-file": {"explain_file"}}


def test_every_tool_is_served_by_the_waggle_mcp_server():
    assert TOOLS <= set(mcp.TOOLS)
    for name in TOOLS:
        spec, fn = mcp.TOOLS[name]
        assert spec["parameters"]["type"] == "object" and 60 < len(spec["description"]) < 700, name
        assert set(spec["parameters"]["required"]) <= set(spec["parameters"]["properties"]), name
        assert callable(fn)


def test_every_tool_has_an_allow_row_in_the_policy_and_is_decided_by_it():
    for name in TOOLS:
        action, why = policy.WAGGLE_TOOLS[name]
        assert action == policy.ALLOW and why, name
        assert policy.decide("mcp__waggle__" + name, {}).allowed, name


def test_tools_answer_json_through_the_mcp_dispatcher(tmp_path):
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "cad_measure", "arguments": {"path": str(tmp_path / "missing.step")}}})
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is False and json.loads(text)["ok"] is False and "not found" in text


def test_each_skill_names_real_tools_and_the_readme_lists_each_skill():
    readme = (ROOT / "skills" / "README.md").read_text(encoding="utf-8")
    for skill, tools in SKILL_TOOLS.items():
        meta, body = skills.parse_frontmatter((ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"))
        for t in tools:
            assert f"`{t}`" in body and t in mcp.TOOLS, (skill, t)
        assert f"`{skill}`" in readme
        assert "rm" not in re.findall(r"\bsudo\b|\bcurl\b", body)  # no shell recipes in a skill
    for t in TOOLS:
        assert f"`{t}`" in readme


def test_a_removed_pack_drops_out_and_a_broken_one_does_not_hide_the_rest(capsys):
    full = {s["name"] for s, _ in tools_ext.collect()}
    assert TOOLS <= full
    without = {s["name"] for s, _ in tools_ext.collect(("tools_cad", "tools_not_here", "tools_media"))}
    assert without == {"cad_convert", "cad_measure", "media_convert", "image_convert"}

    def importer(name):
        if name.endswith("tools_docs"):
            raise ModuleNotFoundError("No module named 'nopackage'", name="nopackage")
        if name.endswith("tools_print"):
            raise RuntimeError("boom")
        return __import__("importlib").import_module(name)

    kept = {s["name"] for s, _ in tools_ext.collect(("tools_cad", "tools_docs", "tools_print"), importer)}
    assert kept == {"cad_convert", "cad_measure"}
    err = capsys.readouterr().err
    assert "tools_docs not loaded" in err and "tools_print not loaded: RuntimeError" in err


def test_the_modules_stay_out_of_the_live_data_home_and_other_peoples_files():
    """The tool packs never name the live data home or the files other work streams own."""
    for name in ("tools_common", "tools_cad", "tools_print", "tools_docs", "tools_media", "tools_github", "tools_explain",
                 "tools_ext"):
        src = (ROOT / "waggle" / f"{name}.py").read_text(encoding="utf-8")
        assert ".waggle" not in src.replace("waggle's", ""), name
        assert not re.search(r"^\s*(?:from|import)\s+.*\b(learn|digest|initiative|routines|engines|quota|vault|purge|pcvoice)\b",
                             src, re.M), name
