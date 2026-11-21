"""The reviewed skills shipped in skills/: they parse, carry no scripts, install safely and are found."""
import json
from pathlib import Path

import install
from waggle import skills

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"
PLANNING = {"verified-computer-use", "verified-browser-task", "research-brief", "objective-planning"}
TOOL_SKILLS = {"cad-files", "documents-to-pdf", "media-convert", "github-portfolio-audit", "explain-this-file"}  # rebuild/tools
NAMES = PLANNING | TOOL_SKILLS | {"watch-feeds", "waggle-vault-cli"}


def test_skills_parse_and_have_no_scripts():
    found = {}
    for md in REPO_SKILLS.glob("*/SKILL.md"):
        meta, body = skills.parse_frontmatter(md.read_text(encoding="utf-8"))
        desc = meta["description"]
        # Hermes's prompt index clips descriptions past 60 chars (agent/skill_utils.py SKILL_PROMPT_DESC_LIMIT)
        assert meta["name"] == md.parent.name and 30 <= len(desc) <= 60 and desc.endswith("."), desc
        assert meta["metadata"]["hermes"]["tags"]
        assert skills.audit_dir(md.parent)["level"] == "none"
        found[meta["name"]] = body
    assert set(found) == NAMES
    tools = ("computer_use", "browser_exec", "browser_vault", "vault_search", "objective_create", "routine_add",
             "delegate")
    assert all(t in "".join(found.values()) for t in tools)  # they reference the real tool names


def test_install_skills_copies_and_keeps_old_versions(tmp_path):
    dest = tmp_path / "waggle"
    assert set(install.install_skills(dest)) == NAMES
    assert install.install_skills(dest) == []  # unchanged -> nothing rewritten
    target = dest / "research-brief" / "SKILL.md"
    target.write_text("locally edited", encoding="utf-8")
    assert install.install_skills(dest) == ["research-brief"]
    kept = list((target.parent / ".versions").glob("*-SKILL.md"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "locally edited"


def test_search_finds_them(tmp_path):
    skills.reindex([("waggle", REPO_SKILLS, "waggle")])
    cases = {"click the save button in the settings window of that app": "verified-computer-use",
             "fill in the signup form on the robotics website": "verified-browser-task",
             "research and compare the best brushless motors, with sources": "research-brief",
             "work on this long multi-step goal in the background": "objective-planning"}
    for q, want in cases.items():
        hits = [h["name"] for h in skills.search(q, k=2)]
        assert want in hits, (q, hits)
    loaded = json.loads(skills.load("verified-computer-use"))
    assert "observe" in json.dumps(loaded).lower()


def test_install_hub_skills_only_fetches_missing_ones(tmp_path):
    from waggle.config import Settings
    calls = []
    got = install.install_hub_skills(run=lambda *a: calls.append(a), home=tmp_path)
    assert len(got) == 4 and calls[0][:3] == ("skills", "install", "official/research/rss-feeds")
    assert "--category" in calls[1] and calls[1][-1] == "--yes"
    for _i, _c, rel in install.HUB_SKILLS[:2]:  # two of them "already installed"
        (tmp_path / "skills" / rel).mkdir(parents=True)
        (tmp_path / "skills" / rel / "SKILL.md").write_text("x")
    calls.clear()
    assert len(install.install_hub_skills(run=lambda *a: calls.append(a), home=tmp_path)) == 2
    s = Settings()
    s.hub_skills = False
    assert install.install_hub_skills(run=lambda *a: calls.append(a), home=tmp_path / "none", settings=s) == []
