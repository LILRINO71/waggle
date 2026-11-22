"""Waggle inside the owner's vault: it reads the whole vault but keeps its own notes in ONE folder ("12 - Waggle"),
writes nowhere else, never touches the owner's .obsidian, and keeps its history inside that folder. Temp vaults only."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import bag_embed

from waggle import brief, dropbox_ingest, graph, hq, ingest, learn, memory, policy, projects, purge, vault, vault_git

OWN = "12 - Waggle"
PROFILE = ["00 - Home/Personal Intelligence Profile.md", "01 - Identity/*", "04 - Preferences/*",
           "06 - Lifestyle/Daily Rhythm.md", "03 - Goals and Ambitions/*"]

OWNER_NOTES = {
    "00 - Home/Personal Intelligence Profile.md": (
        "---\ntype: profile\ntags: [profile]\n---\n[[Dashboard]]\n\n# Personal Intelligence Profile\n\n"
        "> [!info] Epistemic labels\n> `#epi/explicit` stated\n\n## Summary\n"
        "- Ambitious fast-moving builder who works intensely. #epi/explicit\n"
        "- Wants an AI that researches, challenges and executes. #epi/explicit\n\n## Map\n| Area | Index |\n|---|---|\n"
        "| Identity | [[Identity Index]] |\n\n## Update rule\nEdit the underlying note.\n"),
    "01 - Identity/Values.md": (
        "---\ntype: profile\n---\n[[Dashboard]] > [[Identity Index]]\n\n# Values\n\n## Stated\n"
        "- Independence and freedom. #epi/explicit\n- Execution: finishing valuable projects. #epi/explicit\n\n"
        "## Not recorded\n- Political values: not provided.\n\nRelated: [[Motivations]]\n"),
    "04 - Preferences/Communication Preferences.md": (
        "---\ntype: preference\n---\n# Communication Preferences\n\n- Concise by default. #epi/explicit\n"
        "- Direct, factual, honest. #epi/explicit\n"),
    "06 - Lifestyle/Daily Rhythm.md": "# Daily Rhythm\n\n- Prefers waking around 5-6 AM. #epi/explicit\n",
    "03 - Goals and Ambitions/Long-Term Vision.md": "# Long-Term Vision\n\n- Build a recognizable technology company.\n",
    "07 - Projects/Waggle.md": "# Waggle\n\nThe owner's own note about the Waggle project, zebra stripes.\n",
    "07 - Projects/FTC BioBuzz.md": "# FTC BioBuzz\n\nBioBuzz robot notes with the word quokka.\n",
    "10 - Knowledge/Opt Out.md": "---\nwaggle_index: false\n---\n# Opt Out\n\nPrivate platypus text here.\n",
    "Objectives/owner-folder.md": "# Owner folder named Objectives\n\nThe owner may have a folder with this name: marmot.\n",
}
OBSIDIAN = {
    "app.json": '{"foldPage": true}',
    "appearance.json": '{"theme": "moonstone", "enabledCssSnippets": ["personal-intelligence"]}',
    "graph.json": '{"showArrow": false}',
    "workspace.json": '{"main": {}}',
    "snippets/personal-intelligence.css": ".theme-dark { --accent: red; }",
}


@pytest.fixture
def s(settings, tmp_path):
    settings.vault = str(tmp_path / "piv")
    settings.vault_own = OWN
    settings.vault_profile_notes = list(PROFILE)
    settings.vault_git = True
    settings.save()
    root = Path(settings.vault)
    for rel, text in OWNER_NOTES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for rel, text in OBSIDIAN.items():
        p = root / ".obsidian" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    vault.init(settings)
    return settings


def tree(s, under=None, skip_own=True):
    """{relative path: sha1} of every file (and '/' marks for folders) under the vault, minus Waggle's own folder."""
    base = Path(under) if under else vault.root(s)
    out = {}
    for dp, dirs, files in os.walk(base):
        rel_dir = Path(dp).relative_to(base).as_posix()
        if skip_own and rel_dir == OWN:
            dirs[:] = []
            continue
        out[rel_dir + "/"] = ""
        for f in files:
            out[(Path(rel_dir) / f).as_posix()] = hashlib.sha1((Path(dp) / f).read_bytes()).hexdigest()
    out.pop("./", None)
    return out


def git(path, *args):
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip()


def commit_all(s, writer, summary):
    """Commit every note a plain vault.write left pending, under one writer's name."""
    return vault_git.commit(sorted(vault_git._load_pending(vault.own(s))), writer, summary, s)


# ---- the one helper: every Waggle path goes through it ------------------------------------------------------

def test_own_path_is_inside_the_own_folder(s):
    assert vault.own(s) == vault.root(s) / OWN
    assert vault.safe_path("Me/Profile", s) == (vault.root(s) / OWN / "Me" / "Profile.md").resolve()
    assert vault.own_path(f"{OWN}/Me/Profile", s) == vault.own_path("Me/Profile", s)  # a full path is not doubled
    for bad in ("../outside", "Me/../../outside", "../../x/y.md", "..\\x"):
        with pytest.raises(ValueError):
            vault.own_path(bad, s)
    with pytest.raises(ValueError):
        vault.vault_path("../outside", s)


def test_empty_vault_own_is_the_old_layout(settings, tmp_path):
    settings.vault = str(tmp_path / "old")
    settings.save()
    assert vault.own(settings) == vault.root(settings)
    vault.init(settings)
    assert (tmp_path / "old" / "Me" / "Profile.md").exists() and (tmp_path / "old" / ".obsidian").is_dir()
    assert vault.safe_path("Me/Profile", settings) == (tmp_path / "old" / "Me" / "Profile.md").resolve()


@pytest.mark.parametrize("bad", ["../x", "C:/x", "a/../../b", "/abs"])
def test_a_bad_vault_own_is_refused(s, bad):
    s.vault_own = bad
    if bad != "/abs":  # a leading slash is just stripped
        with pytest.raises(ValueError):
            vault.own(s)


def test_init_makes_only_the_own_folder_and_no_obsidian_at_the_root(settings, tmp_path):
    settings.vault = str(tmp_path / "bare")
    settings.vault_own = OWN
    settings.save()
    (tmp_path / "bare").mkdir()
    vault.init(settings)
    assert sorted(p.name for p in (tmp_path / "bare").iterdir()) == [OWN]
    assert (tmp_path / "bare" / OWN / "Me" / "Profile.md").exists()


def test_notes_cover_the_whole_vault_minus_dot_folders_drop_and_originals(s):
    own = vault.own(s)
    (own / "Drop").mkdir(exist_ok=True)
    (own / "Drop" / "waiting.md").write_text("# waiting", encoding="utf-8")
    (own / "Sources" / "_files" / "2026-11").mkdir(parents=True)
    (own / "Sources" / "_files" / "2026-11" / "orig.md").write_text("# orig", encoding="utf-8")
    (vault.root(s) / ".trash").mkdir()
    (vault.root(s) / ".trash" / "gone.md").write_text("# gone", encoding="utf-8")
    (vault.root(s) / ".obsidian" / "note.md").write_text("# x", encoding="utf-8")
    rels = {p.relative_to(vault.root(s)).as_posix() for p in vault.notes(s)}
    assert "01 - Identity/Values.md" in rels and f"{OWN}/Me/Profile.md" in rels
    assert not any(r.startswith((".trash", ".obsidian")) or "/Drop/" in r or "_files" in r for r in rels)
    mine = {p.relative_to(vault.root(s)).as_posix() for p in vault.own_notes(s)}
    assert mine and all(r.startswith(OWN + "/") for r in mine)


# ---- nothing outside the own folder changes ------------------------------------------------------------------

def test_waggle_writes_only_inside_its_own_folder(s):
    before = tree(s)
    vault.write("Me/Profile", "# Profile\n\n- **Name:** Test\n", s)
    vault.write("01 - Identity/Values", "# Waggle's copy, not the owner's\n", s)  # lands inside 12 - Waggle
    vault.append("Ideas/Ideas", "- an idea", s)
    vault.journal("did a thing", s)
    projects.create("Alpha", s)
    projects.add_log("Alpha", "started", s)
    hq.write(s)
    learn.file_fact({"kind": "like", "text": "Likes quiet cafes"}, s, source="t")
    (vault.own(s) / "Objectives").mkdir(exist_ok=True)
    (vault.own(s) / "Objectives" / "test-x.md").write_text("# test objective\n", encoding="utf-8")
    ingest.cleanup(s, apply=True)
    graph.build_from_vault(s)
    memory.reindex(s, embedder=bag_embed)
    drop = dropbox_ingest.init(s)
    (drop / "scan.txt").write_text("The club budget is forty dollars per student. " * 8, encoding="utf-8")
    dropbox_ingest.scan_pass(s, now=1.0e9 + 1.0, ask=lambda n, k, t: {"title": "Budget", "summary": ["a", "b", "c"],
                                                                      "facts": []}, embedder=bag_embed, stable_s=0)
    commit_all(s, "tests", "everything")
    assert vault_git.log(5, s)
    assert tree(s) == before  # every file and folder outside 12 - Waggle is byte for byte what it was
    assert (vault.own(s) / "Projects" / "Alpha.md").exists()
    assert (vault.own(s) / "01 - Identity" / "Values.md").exists()
    assert list((vault.own(s) / "Sources").glob("*/*.md"))  # the dropped file became a note in Waggle's folder


def test_purge_only_rewrites_waggle_notes(s):
    owner = vault.root(s) / "07 - Projects" / "Waggle.md"
    owner.write_text("# Waggle\n\nzebra stripes\n- 09:00 route=chat the model said abracadabra things\n", encoding="utf-8")
    mine = vault.own(s) / "Journal" / "2026-11-20.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("# day\n\n- 09:00 route=chat the model said abracadabra things\n", encoding="utf-8")
    before = owner.read_bytes()
    r = purge.purge(s, apply=True, phrases=("the model said abracadabra",), embedder=bag_embed)
    files = [f["path"] for f in r["vault"]["files"]]
    assert files and all(f.startswith(OWN + "/") for f in files)
    assert owner.read_bytes() == before


def test_the_owners_obsidian_folder_is_never_touched(s, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("waggle_install", Path(__file__).resolve().parent.parent / "install.py")
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    obs = vault.root(s) / ".obsidian"
    before = tree(s, obs, skip_own=False)
    install.setup_obsidian(s)
    after = tree(s, obs, skip_own=False)
    added = set(after) - set(before)
    assert added == {"snippets/waggle-hq.css"}  # a stylesheet of ours that nothing enables
    assert all(after[k] == before[k] for k in before)  # theme, enabled snippets, graph, workspace: unchanged
    assert json.loads((obs / "appearance.json").read_text(encoding="utf-8"))["theme"] == "moonstone"
    (obs / "snippets" / "waggle-hq.css").write_text("/* the owner edited it */", encoding="utf-8")
    install.setup_obsidian(s)  # the file exists now: left as the owner has it
    assert (obs / "snippets" / "waggle-hq.css").read_text(encoding="utf-8") == "/* the owner edited it */"


def test_no_obsidian_folder_is_created_when_there_is_none(settings, tmp_path):
    settings.vault = str(tmp_path / "noobs")
    settings.vault_own = OWN
    settings.save()
    (tmp_path / "noobs").mkdir()
    spec = importlib.util.spec_from_file_location("waggle_install2", Path(__file__).resolve().parent.parent / "install.py")
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    vault.init(settings)
    install.setup_obsidian(settings)
    assert not (tmp_path / "noobs" / ".obsidian").exists()
    assert (tmp_path / "noobs" / OWN / "Waggle HQ.md").exists()


def test_agents_may_write_only_in_the_own_folder(s, monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path / "elsewhere"))  # the test vault itself sits under the real TEMP
    roots = policy.write_roots(s)
    assert policy.norm(vault.own(s)) in roots
    assert policy.norm(vault.root(s)) not in roots
    assert not policy.under(vault.root(s) / "01 - Identity" / "Values.md", roots)
    assert policy.under(vault.own(s) / "Me" / "Profile.md", roots)


def test_vault_own_is_a_guarded_setting():
    from waggle import guard
    assert "vault_own" in guard.CONFIG_KEYS and "vault" in guard.CONFIG_KEYS


# ---- history: inside the own folder only ---------------------------------------------------------------------

def test_history_lives_inside_the_own_folder_and_ignores_the_owners_notes(s):
    root, own = vault.root(s), vault.own(s)
    owner_note = root / "01 - Identity" / "Values.md"
    vault.write("Me/Learned", "# Learned\n\n- one\n", s)
    commit_all(s, "learn", "first")
    assert (own / ".git").is_dir() and not (root / ".git").exists()
    owner_note.write_text(owner_note.read_text(encoding="utf-8") + "\n- an edit made in Obsidian\n", encoding="utf-8")
    (root / "01 - Identity" / "New note.md").write_text("# new\n", encoding="utf-8")
    vault.write("Me/Learned", "# Learned\n\n- one\n- two\n", s)
    sha = commit_all(s, "learn", "second")
    assert sha
    files = {f for r in vault_git.log(50, s) for f in r["files"]}
    assert "Me/Learned.md" in files and not any("Identity" in f for f in files)  # paths are inside the own folder
    assert git(own, "status", "--porcelain").count("Identity") == 0
    assert "an edit made in Obsidian" in owner_note.read_text(encoding="utf-8")


def test_restore_puts_waggles_note_back_and_leaves_the_owners_notes_alone(s):
    root, own = vault.root(s), vault.own(s)
    vault.write("Me/Learned", "# Learned\n\n- good\n", s)
    commit_all(s, "learn", "good")
    vault.write("Me/Learned", "# Learned\n\n- bad\n", s)
    bad = commit_all(s, "learn", "bad")
    owner_note = root / "03 - Goals and Ambitions" / "Long-Term Vision.md"
    owner_note.write_text("# edited by the owner\n", encoding="utf-8")
    r = vault_git.restore(bad, s)
    assert r["ok"], r
    assert "good" in (own / "Me" / "Learned.md").read_text(encoding="utf-8")
    assert owner_note.read_text(encoding="utf-8") == "# edited by the owner\n"
    assert vault_git.status(s)["own"] == OWN and not (root / ".git").exists()


def test_a_note_outside_the_own_folder_is_never_recorded(s):
    vault_git.note_write(vault.root(s) / "01 - Identity" / "Values.md", s)
    assert not vault_git._load_pending(vault.own(s))
    assert vault_git.commit([vault.root(s) / "01 - Identity" / "Values.md"], "x", "nope", s) is None or \
        "Identity" not in " ".join(f for r in vault_git.log(5, s) for f in r["files"])


# ---- reading the whole vault ---------------------------------------------------------------------------------

def indexed(s):
    con = memory._db()
    paths = {r[0] for r in con.execute("SELECT DISTINCT path FROM chunks WHERE idx >= 0")}
    con.close()
    return paths


def test_the_index_covers_the_owners_notes_and_waggle_rules_apply_inside_own(s):
    vault.write("Projects/Zeta", "# Zeta\n\nWaggle project note about narwhals and glaciers.\n", s)
    vault.write("Objectives/obj", "# Obj\n\nA Waggle objective about vultures and deserts.\n", s)
    vault.write("Archive/old", "# Old\n\nArchived text about wombats and caves.\n", s)
    (vault.own(s) / "Drop").mkdir(exist_ok=True)
    (vault.own(s) / "Drop" / "d.md").write_text("# d\n\nA waiting file about otters and rivers.\n", encoding="utf-8")
    memory.reindex(s, embedder=bag_embed)
    got = indexed(s)
    assert "01 - Identity/Values.md" in got and "07 - Projects/FTC BioBuzz.md" in got
    assert f"{OWN}/Projects/Zeta.md" in got
    assert "Objectives/owner-folder.md" in got  # the owner's folder that happens to be called Objectives
    assert f"{OWN}/Objectives/obj.md" not in got and f"{OWN}/Archive/old.md" not in got
    assert not any("/Drop/" in p for p in got)
    assert "10 - Knowledge/Opt Out.md" not in got  # the owner's waggle_index: false is honoured
    assert not any(p.startswith((".obsidian", ".git")) for p in got)


def test_search_finds_an_owner_note(s):
    memory.reindex(s, embedder=bag_embed)
    hits, _info = memory.relevant("what do I know about quokka robots", s, bag_embed)
    assert any(h["path"] == "07 - Projects/FTC BioBuzz.md" for h in hits)


def test_read_note_by_name_path_and_waggle_path(s):
    vault.write("Projects/Waggle", "# Waggle (Waggle's own note)\n", s)
    r = memory.read_note("FTC BioBuzz", s)
    assert r["path"] == "07 - Projects/FTC BioBuzz.md"
    assert memory.read_note("01 - Identity/Values.md", s)["path"] == "01 - Identity/Values.md"
    assert memory.read_note("Waggle", s)["path"] == "07 - Projects/Waggle.md"  # the owner's note wins a bare name
    assert memory.read_note("Projects/Waggle", s)["path"] == f"{OWN}/Projects/Waggle.md"  # a Waggle path is Waggle's
    assert "error" in memory.read_note("../../outside", s)


def test_the_graph_reads_the_whole_vault(s):
    vault.write("Projects/Zeta", "---\ntype: project\n---\n# Zeta\n\nSee [[FTC BioBuzz]].\n", s)
    graph.build_from_vault(s)
    names = {n["name"] for n in graph.neighbors("FTC BioBuzz")["nodes"]} if isinstance(graph.neighbors("FTC BioBuzz"), dict) \
        and "nodes" in graph.neighbors("FTC BioBuzz") else set()
    con = graph._db()
    keys = {r[0] for r in con.execute("SELECT key FROM nodes")}
    con.close()
    assert any("ftc biobuzz" in k.lower() for k in keys) and any("waggle" in k.lower() for k in keys)


# ---- the owner's profile -------------------------------------------------------------------------------------

def test_profile_digest_is_short_clean_and_within_the_cap(s):
    text = vault.profile_digest(s, 1600)
    assert text and len(text) <= 1700
    assert "Independence and freedom" in text and "Concise by default" in text and "waking around 5-6 AM" in text
    assert "#epi" not in text and "[[" not in text and "---" not in text and "|" not in text
    assert "Update rule" not in text and "Epistemic" not in text
    assert vault.profile_digest(s, 200).count("\n") >= 1 and len(vault.profile_digest(s, 200)) < 700
    s.vault_profile_notes = []
    assert vault.profile_digest(s) == ""
    s.vault_profile_notes = ["../outside.md", "nothing/*", "01 - Identity/Values"]
    assert [p.name for p in vault.profile_notes(s)] == ["Values.md"]


def test_core_block_and_brief_draw_on_the_owners_profile(s):
    vault.write("Me/Profile", "# Profile\n\n## About me\n- **Name:** Test\n", s)
    core = memory.core_block(s)
    assert "From the user's own profile notes" in core and "Execution: finishing valuable projects" in core
    assert "**Name:** Test" in core
    assert len(memory._profile_core(s)) <= 2500
    facts = brief.facts(s)
    assert any("Independence and freedom" in f for f in facts) and facts[0].startswith("The user's name: Test")
    s.vault_profile_notes = []
    assert "From the user's own profile notes" not in memory.core_block(s)
    assert not any("own profile notes" in f for f in brief.facts(s))


# ---- the move script -----------------------------------------------------------------------------------------

def load_move():
    spec = importlib.util.spec_from_file_location("move_vault", Path(__file__).resolve().parent.parent / "tools" / "move_vault.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def move_env(settings, tmp_path):
    old = tmp_path / "WaggleVault"
    new = tmp_path / "piv"
    for rel, text in {"Me/Profile.md": "# Profile\n- **Name:** T\n", "Projects/Alpha.md": "# Alpha\n",
                      "Waggle.md": "# Waggle vault\n", "Journal/2026-11-20.md": "# day\n"}.items():
        (old / rel).parent.mkdir(parents=True, exist_ok=True)
        (old / rel).write_text(text, encoding="utf-8")
    (old / ".obsidian").mkdir()
    (old / ".obsidian" / "appearance.json").write_text('{"theme": "obsidian"}', encoding="utf-8")
    (old / "Drop").mkdir()
    for rel, text in OWNER_NOTES.items():
        (new / rel).parent.mkdir(parents=True, exist_ok=True)
        (new / rel).write_text(text, encoding="utf-8")
    (new / ".obsidian").mkdir()
    (new / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    # the old vault has Waggle's history
    old_s = type(settings)(vault=str(old), vault_git=True)
    vault.write("Me/Likes", "# Likes\n\n- jazz\n", old_s)
    vault_git.commit(sorted(vault_git._load_pending(old)), "t", "first", old_s)
    cfg = tmp_path / "home" / "config.json"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(json.dumps({"vault": str(old), "jobs_dir": "J", "note": "caf\u00e9"}), encoding="utf-8")
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    return old, new, cfg, desktop


def run_move(mv, old, new, cfg, desktop, *extra):
    return mv.main(["--src", str(old), "--dest", str(new), "--config", str(cfg), "--desktop", str(desktop),
                    "--report", str(cfg.parent / "plan.json"), *extra])


def test_move_dry_run_changes_nothing(move_env, capsys):
    old, new, cfg, desktop = move_env
    before = (tree(None, old, skip_own=False), tree(None, new, skip_own=False), cfg.read_bytes())
    assert run_move(load_move(), old, new, cfg, desktop) == 0
    assert (tree(None, old, skip_own=False), tree(None, new, skip_own=False), cfg.read_bytes()) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Nothing was changed" in out and "12 - Waggle" in out
    plan = json.loads((cfg.parent / "plan.json").read_text(encoding="utf-8"))
    assert plan["conflicts"] == [] and plan["copy"] > 4 and plan["history_copied"]
    assert not (new / OWN).exists() and not list(cfg.parent.glob("*.bak*"))


def test_move_apply_copies_keeps_the_history_and_sets_the_config(move_env, capsys):
    old, new, cfg, desktop = move_env
    mv = load_move()
    owner_before = tree(None, new, skip_own=True)
    assert run_move(mv, old, new, cfg, desktop, "--apply") == 0
    own = new / OWN
    assert (own / "Me" / "Profile.md").read_text(encoding="utf-8") == "# Profile\n- **Name:** T\n"
    assert (own / "Projects" / "Alpha.md").exists() and (own / "Waggle.md").exists() and (own / "Drop").is_dir()
    assert not (own / ".obsidian").exists()  # the old theme is not brought along
    assert not (new / ".git").exists() and (own / ".git").is_dir()
    assert tree(None, new, skip_own=True) == owner_before  # the owner's vault is untouched
    assert (old / "Me" / "Profile.md").exists()  # the old vault is left in place
    cfgd = json.loads(cfg.read_text(encoding="utf-8"))
    assert cfgd["vault"] == str(new) and cfgd["vault_own"] == OWN and cfgd["vault_profile_notes"] == mv.PROFILE_NOTES
    assert cfgd["note"] == "caf\u00e9" and cfgd["jobs_dir"] == "J"  # other settings and UTF-8 survive
    assert len(list(cfg.parent.glob("config.json.bak-vaultmove-*"))) == 1
    # the copied history works from the new place
    import waggle.config as wc
    s_new = wc.Settings.load()
    assert s_new.vault == str(new) and vault.own(s_new) == new / OWN
    assert any(r["subject"].startswith("t: first") for r in vault_git.log(10, s_new))


def test_move_never_overwrites_and_is_idempotent(move_env, capsys):
    old, new, cfg, desktop = move_env
    mv = load_move()
    (new / OWN / "Me").mkdir(parents=True)
    (new / OWN / "Me" / "Profile.md").write_text("# the owner's own file, different\n", encoding="utf-8")
    assert run_move(mv, old, new, cfg, desktop, "--apply") == 0
    assert (new / OWN / "Me" / "Profile.md").read_text(encoding="utf-8") == "# the owner's own file, different\n"
    first = json.loads((cfg.parent / "plan.json").read_text(encoding="utf-8"))
    assert first["conflicts"] == ["Me/Profile.md"]
    snap = (tree(None, new, skip_own=False), cfg.read_bytes())
    capsys.readouterr()
    assert run_move(mv, old, new, cfg, desktop, "--apply") == 0  # again: nothing left to do
    again = json.loads((cfg.parent / "plan.json").read_text(encoding="utf-8"))
    assert again["copy"] == 0 and again["config_changes"] == {}
    assert (tree(None, new, skip_own=False), cfg.read_bytes()) == snap
    assert len(list(cfg.parent.glob("config.json.bak-vaultmove-*"))) == 1  # no second backup for no change


def test_move_refuses_while_the_old_history_is_in_use(move_env, capsys):
    old, new, cfg, desktop = move_env
    (old / ".git" / "waggle.lock").write_text("x", encoding="utf-8")
    assert run_move(load_move(), old, new, cfg, desktop, "--apply") == 3
    assert not (new / OWN).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="a .lnk shortcut")
def test_move_repoints_the_feed_waggle_shortcut(move_env):
    old, new, cfg, desktop = move_env
    mv = load_move()
    ok, err = mv.set_shortcut(desktop / mv.SHORTCUT, old / "Drop")
    assert ok, err
    assert mv.same_path(mv.shortcut_target(desktop / mv.SHORTCUT), old / "Drop")
    assert run_move(mv, old, new, cfg, desktop, "--apply") == 0
    assert mv.same_path(mv.shortcut_target(desktop / mv.SHORTCUT), new / OWN / "Drop")
