"""The vault under local git: every Waggle change is a commit with the writer's name, the owner's edits are kept
apart, constraints refuse bad changes, restore is a new commit, facts are diffable, and the commands work.
Temp vault only (conftest points Settings at tmp_path); git runs for real but only inside that folder."""
import json
import subprocess
from pathlib import Path

import pytest

from waggle import engine_commands, events, kstore, learn, vault, vault_git
from waggle.vault_git import FACTS_REL


@pytest.fixture
def s(settings):
    settings.vault_git = True
    settings.save()
    vault.init(settings)
    return settings


def root(s):
    return vault.root(s)


def git(s, *args):
    r = subprocess.run(["git", "-C", str(root(s)), *args], capture_output=True, text=True, encoding="utf-8",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip()


def subjects(s, n=50):
    return [r["subject"] for r in vault_git.log(n, s)]


def commit_all(s, writer, summary):
    """Commit every note a plain vault.write left pending, under one writer's name."""
    return vault_git.commit(sorted(vault_git._load_pending(root(s))), writer, summary, s)


def owner_edit(s, rel, text):
    p = root(s) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---- the repository ---------------------------------------------------------------------------------------

def test_the_repository_is_created_on_first_use_and_init_is_idempotent(s):
    assert not vault_git.has_repo(s)
    vault.write("Me/Likes", "# Likes\n\n- jazz\n", s)
    assert vault_git.has_repo(s)
    first = git(s, "rev-list", "--count", "HEAD")
    assert subjects(s)[-1].startswith("owner edits: first snapshot of the vault")
    for _ in range(3):
        vault.write("Me/Likes", "# Likes\n\n- jazz\n", s)  # the same content again: nothing new to commit
        assert vault_git.checkpoint(s) is None
    assert git(s, "rev-list", "--count", "HEAD") == first
    assert (root(s) / ".gitignore").exists()
    assert git(s, "remote") == ""  # never pushed anywhere


def test_a_second_init_leaves_an_owner_edited_gitignore_alone(s):
    vault.write("Me/Likes", "# Likes\n", s)
    ignore = root(s) / ".gitignore"
    ignore.write_text("Drop/\nmy-own-rule\n", encoding="utf-8")
    vault_git._READY.clear()
    vault.write("Me/Dislikes", "# D\n", s)
    assert "my-own-rule" in ignore.read_text(encoding="utf-8")


def test_the_drop_folder_raw_files_and_big_binaries_stay_out(s):
    vault.write("Me/Likes", "# Likes\n", s)
    (root(s) / "Drop").mkdir(exist_ok=True)
    (root(s) / "Drop" / "raw.txt").write_text("raw dropped file", encoding="utf-8")
    (root(s) / "Sources" / "_files").mkdir(parents=True, exist_ok=True)
    (root(s) / "Sources" / "_files" / "orig.md").write_text("# an original", encoding="utf-8")
    (root(s) / "Inbox" / "scan.pdf").write_bytes(b"%PDF" + b"0" * 5000)
    (root(s) / "Inbox" / "photo.png").write_bytes(b"\x89PNG" + b"0" * 5000)
    vault_git.checkpoint(s)
    tracked = git(s, "ls-files").splitlines()
    assert not any(t.startswith(("Drop/", "Sources/_files/")) or t.endswith((".pdf", ".png")) for t in tracked)
    assert "Me/Likes.md" in tracked


def test_a_disabled_feature_does_nothing(settings):
    settings.vault_git = False
    settings.save()
    vault.init(settings)
    vault.write("Me/Likes", "# Likes\n", settings)
    assert vault_git.commit(None, "digest", "x", settings) is None
    assert not (root(settings) / ".git").exists()


# ---- one commit per writer, named ------------------------------------------------------------------------

def test_every_writer_gets_its_own_named_commit(s):
    @vault_git.recorded("digest", "journal digest: 1 note")
    def digest_like():
        vault.write("Journal/2026-11-20", "# 2026-11-20\n\n- 12:00 something\n", s)

    @vault_git.recorded("learn", lambda r, a, k: "filed like fact: jazz")
    def learn_like():
        vault.append("Me/Likes", "- jazz", s)

    @vault_git.recorded("dropbox", "Drop folder: 1 file(s) filed")
    def drop_like():
        vault.write("Sources/2026-11/report", "# report\n", s)

    digest_like()
    learn_like()
    drop_like()
    subs = subjects(s)
    assert subs[:3] == ["dropbox: Drop folder: 1 file(s) filed", "learn: filed like fact: jazz",
                        "digest: journal digest: 1 note"]
    rows = vault_git.log(3, s)
    assert rows[2]["writer"] == "digest" and rows[2]["files"] == ["Journal/2026-11-20.md"]
    body = git(s, "log", "-1", "--format=%B", "HEAD~2")
    assert "A Journal/2026-11-20.md" in body and "Waggle-Writer: digest" in body


def test_a_write_with_no_recorded_run_is_named_for_the_calling_module_and_flushed_later(s, monkeypatch):
    vault.write("Projects/Hive", "# Hive\n", s)
    assert vault_git._load_pending(root(s))["Projects/Hive.md"][0] == "test_vault_git"
    monkeypatch.setattr(vault_git, "STALE_PENDING_S", -1)
    vault_git.commit(None, "digest", "nothing of its own", s)
    assert any(x.startswith("test_vault_git: 1 note(s) written outside a recorded run") for x in subjects(s))


def test_a_recorded_function_that_changes_nothing_commits_nothing(s):
    @vault_git.recorded("digest", "x")
    def quiet():
        return None

    vault.write("Me/Likes", "# Likes\n", s)
    before = git(s, "rev-list", "--count", "HEAD")
    quiet()
    assert git(s, "rev-list", "--count", "HEAD") == before


def test_a_failing_git_never_breaks_the_writer(s, monkeypatch):
    vault.write("Me/Likes", "# Likes\n", s)
    commit_all(s, "learn", "baseline")
    monkeypatch.setattr(vault_git, "_git", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git exploded")))
    vault.write("Me/Likes", "# Likes\n\n- survives\n", s)  # the hook swallows it
    assert "survives" in vault.read("Me/Likes", s)
    assert vault_git.commit(None, "digest", "x", s) is None
    assert "git exploded" in vault_git.LAST_ERROR[0]
    assert any(e.get("action") == "error" for e in events.read("vault_git"))


def test_a_missing_git_binary_never_breaks_the_writer(s, monkeypatch):
    vault.write("Me/Likes", "# Likes\n", s)

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(vault_git.subprocess, "run", no_git)
    vault.write("Me/Likes", "# Likes\n\n- still written\n", s)
    assert "still written" in vault.read("Me/Likes", s)
    assert vault_git.commit(None, "learn", "x", s) is None


# ---- the owner's edits ------------------------------------------------------------------------------------

def test_the_owners_edits_are_committed_first_so_a_restore_never_loses_them(s):
    vault.write("Me/Likes", "# Likes\n\n- jazz\n", s)
    commit_all(s, "learn", "first")
    owner_edit(s, "Me/Likes.md", "# Likes\n\n- jazz\n- my own line typed in Obsidian\n")  # not through Waggle

    @vault_git.recorded("learn", "filed like fact: piano")
    def waggle_adds():
        vault.append("Me/Likes", "- piano", s)

    waggle_adds()
    subs = subjects(s)
    assert subs[0] == "learn: filed like fact: piano"
    assert subs[1] == "owner edits: Me/Likes.md"
    sha = vault_git.log(1, s)[0]["sha"]
    res = vault_git.restore(sha, s)
    assert res["ok"]
    text = vault.read("Me/Likes", s)
    assert "my own line typed in Obsidian" in text and "piano" not in text


def test_new_owner_files_never_touched_by_waggle_are_swept_into_the_next_commit_as_owner_edits(s):
    vault.write("Me/Likes", "# Likes\n", s)
    commit_all(s, "learn", "first")
    owner_edit(s, "Ideas/mine.md", "# an idea of mine\n")
    vault.write("Me/Dislikes", "# Dislikes\n\n- noise\n", s)
    commit_all(s, "learn", "second")
    subs = subjects(s)
    assert subs[0] == "learn: second" and subs[1] == "owner edits: Ideas/mine.md"


def test_an_owner_edit_cannot_be_restored(s):
    vault.write("Me/Likes", "# Likes\n", s)
    commit_all(s, "learn", "x")
    owner_edit(s, "Me/Likes.md", "# Likes\n\n- mine\n")
    vault_git.checkpoint(s)
    sha = vault_git.log(1, s)[0]["sha"]
    res = vault_git.restore(sha, s)
    assert not res["ok"] and "your own edit" in res["message"]
    assert "mine" in vault.read("Me/Likes", s)


# ---- constraints ------------------------------------------------------------------------------------------

def test_a_change_over_the_size_cap_is_refused_logged_and_put_back(s):
    s.vault_git_max_kb = 1
    s.save()
    vault.write("Me/Learned", "# Learned\n\n- small\n", s)
    commit_all(s, "learn", "baseline")
    vault_git.REFUSED.clear()

    @vault_git.recorded("digest", "huge")
    def writes_too_much():
        vault.write("Me/Learned", "# Learned\n\n" + "- a long line of text\n" * 200, s)
        vault.write("Journal/huge", "x" * 5000, s)

    writes_too_much()
    assert vault.read("Me/Learned", s).strip().endswith("- small")  # put back to the committed content
    assert not (root(s) / "Journal" / "huge.md").exists()  # a new note that is too big is removed
    assert {r for r, _ in vault_git.REFUSED} == {"Me/Learned.md", "Journal/huge.md"}
    assert all("size cap" in why for _, why in vault_git.REFUSED)
    ev = events.read("vault_git_refused")
    assert {e["path"] for e in ev} == {"Me/Learned.md", "Journal/huge.md"} and ev[0]["writer"] == "digest"
    assert not any(x.startswith("digest:") for x in subjects(s, 3))  # nothing was committed for it


def test_a_read_only_note_cannot_be_changed_by_waggle_but_the_owner_can(s):
    s.vault_git_readonly = ["Me/Profile.md", "Me/Specialists/*"]
    s.save()
    vault_git.REFUSED.clear()
    vault.write("Me/Likes", "# Likes\n", s)
    before = vault.read("Me/Profile.md", s)
    learn.set_about("name", "Not Real", s)  # learn.py writes Me/Profile.md through vault.write
    vault.write("Me/Specialists/coder", "# coder\n", s)

    @vault_git.recorded("learn", "about")
    def go():
        learn.set_about("grade", "Junior", s)
        vault.write("Me/Specialists/coder", "# coder\n\nchanged\n", s)
        vault.write("Me/Likes", "# Likes\n\n- fine\n", s)

    go()
    assert vault.read("Me/Profile.md", s) == before
    assert not (root(s) / "Me" / "Specialists" / "coder.md").exists()
    assert "fine" in vault.read("Me/Likes", s)  # the allowed change in the same run still lands
    assert {r for r, _ in vault_git.REFUSED} >= {"Me/Profile.md", "Me/Specialists/coder.md"}
    assert all("read-only" in why for _, why in vault_git.REFUSED)
    # the owner edits the read-only note in Obsidian: committed as theirs, never refused
    owner_edit(s, "Me/Profile.md", before + "\n- owner wrote this\n")
    vault_git.checkpoint(s)
    assert subjects(s)[0] == "owner edits: Me/Profile.md"
    assert "owner wrote this" in vault.read("Me/Profile.md", s)


# ---- restore ----------------------------------------------------------------------------------------------

def test_restore_is_a_new_commit_and_brings_the_old_content_back(s):
    vault.write("Me/Learned", "# Learned\n\n- one\n", s)
    commit_all(s, "learn", "one")

    @vault_git.recorded("learnloop", "fix: replaced two")
    def change():
        vault.write("Me/Learned", "# Learned\n\n- two\n", s)
        vault.write("Ideas/New", "# New\n", s)

    change()
    n = int(git(s, "rev-list", "--count", "HEAD"))
    changed = vault_git.log(1, s)[0]
    old_head = git(s, "rev-parse", "HEAD")
    res = vault_git.restore(changed["sha"], s)
    assert res["ok"] and res["sha"] and sorted(res["files"]) == ["Ideas/New.md", "Me/Learned.md"]
    assert "- one" in vault.read("Me/Learned", s) and "- two" not in vault.read("Me/Learned", s)
    assert not (root(s) / "Ideas" / "New.md").exists()  # a note the change added is removed again
    assert int(git(s, "rev-list", "--count", "HEAD")) == n + 1  # a NEW commit, nothing rewritten
    assert git(s, "merge-base", "--is-ancestor", old_head, "HEAD") == ""  # old history still reachable
    assert subjects(s)[0].startswith("restore: undid learnloop's change")
    assert vault_git.restore(res["sha"], s)["ok"]  # and the restore can itself be undone
    assert "- two" in vault.read("Me/Learned", s)


def test_restore_of_a_deleted_note_puts_it_back_and_warns_about_later_changes(s):
    vault.write("Projects/Hive", "# Hive\n\n- v1\n", s)
    commit_all(s, "learn", "v1")

    @vault_git.recorded("purge", "removed 1 line")
    def purge_like():
        vault_git.note_write(root(s) / "Projects" / "Hive.md", s)  # like the purge: marked, then changed
        (root(s) / "Projects" / "Hive.md").unlink()

    purge_like()
    gone = vault_git.log(1, s)[0]
    assert gone["files"] == ["Projects/Hive.md"]
    res = vault_git.restore(gone["sha"], s)
    assert res["ok"] and "v1" in vault.read("Projects/Hive", s)

    vault.write("Projects/Hive", "# Hive\n\n- v2\n", s)
    commit_all(s, "digest", "v2")
    res = vault_git.restore(gone["sha"], s)  # a later change touched the same note
    assert res["ok"] and res["later"] == ["Projects/Hive.md"] and "undone too" in res["message"]


def test_restore_refuses_unknown_first_and_garbage_ids(s):
    vault.write("Me/Likes", "# Likes\n", s)
    first = vault_git.log(5, s)[-1]["sha"]
    assert "first snapshot" in vault_git.restore(first, s)["message"]
    assert "don't know" in vault_git.restore("deadbeef", s)["message"]
    assert "don't know" in vault_git.restore("--force", s)["message"]
    assert "don't know" in vault_git.restore("HEAD; rm -rf", s)["message"]


# ---- facts snapshot ---------------------------------------------------------------------------------------

def test_the_facts_table_is_snapshotted_in_the_same_commit_and_diffs(s):
    vault.write("Me/Likes", "# Likes\n", s)
    commit_all(s, "learn", "baseline")
    fid = learn._insert({"kind": "like", "text": "Likes jazz piano", "source": "tool", "origin": "user"})

    @vault_git.recorded("learn", "filed like fact: jazz piano")
    def file_it():
        vault.append("Me/Likes", "- Likes jazz piano", s)

    file_it()
    last = vault_git.log(1, s)[0]
    assert FACTS_REL in last["files"] and "Me/Likes.md" in last["files"]
    rows = [json.loads(x) for x in (root(s) / FACTS_REL).read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == [fid] and rows[0]["text"] == "Likes jazz piano" and rows[0]["status"] == "active"
    learn._set_status(fid, "replaced", None, invalid_at="2026-11-20")  # a fact-only change: its own commit
    assert vault_git.commit(None, "learnloop", "closed a fact", s)
    diff = git(s, "show", "--format=", "HEAD", "--", FACTS_REL)
    assert '"status": "active"' in diff and '"status": "replaced"' in diff


def test_a_restore_leaves_the_facts_snapshot_and_the_database_alone(s):
    vault.write("Me/Likes", "# Likes\n", s)
    commit_all(s, "learn", "baseline")
    learn._insert({"kind": "like", "text": "Likes tea", "source": "tool", "origin": "user"})

    @vault_git.recorded("learn", "filed like fact: tea")
    def file_it():
        vault.append("Me/Likes", "- Likes tea", s)

    file_it()
    sha = vault_git.log(1, s)[0]["sha"]
    res = vault_git.restore(sha, s)
    assert res["ok"] and res["files"] == ["Me/Likes.md"]
    assert "tea" not in vault.read("Me/Likes", s)
    assert len(learn.facts("active")) == 1  # knowledge.db is not rolled back by a note restore
    assert "Likes tea" in (root(s) / FACTS_REL).read_text(encoding="utf-8")


# ---- the real writers -------------------------------------------------------------------------------------

def test_learn_file_fact_commits_the_note_and_the_fact_together(s):
    ok, fid = learn.file_fact({"kind": "like", "text": "Likes bee themed names"}, s, source="t")
    assert ok
    last = vault_git.log(1, s)[0]
    assert last["subject"] == "learn: filed like fact: Likes bee themed names"
    assert set(last["files"]) == {"Me/Learned.md", FACTS_REL} or set(last["files"]) == {"Me/Likes.md", FACTS_REL}


def test_a_learnloop_run_names_the_loop_as_the_writer(s):
    from waggle import learnloop

    @vault_git.recorded("learnloop", "learning-loop pass")
    def pass_():
        learn.file_fact({"kind": "like", "text": "Likes quiet cafes"}, s, source="t")

    pass_()
    assert vault_git.log(1, s)[0]["subject"].startswith("learnloop: filed like fact")
    assert learnloop.run.__wrapped__  # the real entry points carry the decorator
    assert learnloop.undo.__wrapped__


def test_the_digest_purge_ingest_and_drop_entry_points_are_recorded(s):
    from waggle import digest, dropbox_ingest, dropbox_tidy, ingest, purge
    for fn in (digest.run, purge.purge, purge.restore, ingest.run_due, dropbox_ingest.scan_pass, dropbox_tidy.run,
               learn.file_fact, learn.consolidate):
        assert hasattr(fn, "__wrapped__"), fn.__qualname__


def test_a_real_drop_folder_pass_commits_the_new_note_under_dropbox(s):
    from test_dropbox_ingest import NOW, Fake, put
    from conftest import bag_embed
    from waggle import dropbox_ingest as di
    put(s, "notes.txt", "Meeting notes. Due on 2026-12-01. Costs 40 dollars. " * 4)
    di.scan_pass(s, now=NOW, ask=Fake(), embedder=bag_embed, stable_s=0)
    last = vault_git.log(1, s)[0]
    assert last["subject"].startswith("dropbox: Drop folder: 1 file(s) filed")
    assert any(f.startswith("Sources/2026-11/") for f in last["files"])
    assert not any(f.startswith(("Drop/", "Sources/_files/")) for f in git(s, "ls-files").splitlines())


def test_a_real_purge_is_one_named_commit_and_restorable(s):
    from waggle import purge
    vault.write("Me/Learned", "# Learned\n\n- keep me\n- zebra test tomorrow quokka meet\n", s)
    commit_all(s, "learn", "baseline")
    owner_edit(s, "Me/Learned.md", "# Learned\n\n- keep me\n- zebra test tomorrow quokka meet\n- owner line\n")
    from waggle import traces
    traces.record("ugh I have a quokka meet Saturday and a zebra test tomorrow", session="dev", platform="cli",
                  reply="ok")
    from test_knowledge import word_embed
    rep = purge.purge(s, apply=True, phrases=("zebra test tomorrow quokka meet",), embedder=word_embed)
    assert rep["vault"]["notes"] >= 1
    subs = subjects(s, 4)
    assert subs[0].startswith("purge: removed") and "owner edits: Me/Learned.md" in subs[1:3]
    assert "zebra" not in vault.read("Me/Learned", s) and "owner line" in vault.read("Me/Learned", s)
    res = vault_git.restore(vault_git.log(1, s)[0]["sha"], s)
    assert res["ok"] and "zebra" in vault.read("Me/Learned", s) and "owner line" in vault.read("Me/Learned", s)


# ---- the commands -----------------------------------------------------------------------------------------

def test_the_cli_log_show_restore(s, capsys):
    vault.write("Me/Likes", "# Likes\n", s)

    @vault_git.recorded("digest", "journal digest: 1 note")
    def go():
        vault.write("Journal/2026-11-20", "# day\n\nhello\n", s)

    go()
    sha = vault_git.log(1, s)[0]["sha"]
    assert vault_git.main(["log", "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert "digest: journal digest: 1 note" in out and sha in out
    assert vault_git.main(["show", sha]) == 0
    out = capsys.readouterr().out
    assert "Journal/2026-11-20.md" in out and "+hello" in out
    assert vault_git.main(["restore", sha]) == 0
    assert not (root(s) / "Journal" / "2026-11-20.md").exists()
    assert vault_git.main(["restore", "0000000"]) == 1
    assert vault_git.main(["status"]) == 0
    assert '"repo": true' in capsys.readouterr().out


def test_waggle_memory_in_telegram_lists_shows_and_restores(s):
    assert "No memory history" in engine_commands.serve("/waggle memory", s)

    @vault_git.recorded("learn", "filed like fact: jazz")
    def go():
        vault.append("Me/Likes", "- jazz", s)

    go()
    sha = vault_git.log(1, s)[0]["sha"]
    listing = engine_commands.serve("/waggle memory", s)
    assert "learn: filed like fact: jazz" in listing and sha in listing and "restore" in listing
    assert "Me/Likes.md" in engine_commands.serve(f"/waggle memory show {sha}", s)
    assert engine_commands.serve("/waggle memory log 1", s).count(sha) == 1
    reply = engine_commands.serve(f"/waggle memory restore {sha}", s)
    assert reply.startswith("Restored 1 note(s)") and "jazz" not in vault.read("Me/Likes", s)
    assert "don't know" in engine_commands.serve("/waggle memory restore ffffff", s)
    assert "/waggle memory" in engine_commands.HELP
    assert "Use" in engine_commands.serve("/waggle memory frobnicate", s)


def test_waggle_memory_says_so_when_the_feature_is_off(settings):
    settings.vault_git = False
    settings.save()
    assert "off" in engine_commands.serve("/waggle memory", settings)


# ---- windowless processes ---------------------------------------------------------------------------------

def test_every_git_call_is_started_without_a_window(s, monkeypatch):
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append((cmd, kw))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(vault_git.subprocess, "run", spy)
    vault.write("Me/Likes", "# Likes\n", s)
    commit_all(s, "learn", "x")
    vault_git.log(3, s)
    vault_git.restore(vault_git.log(1, s)[0]["sha"], s)
    assert len(calls) > 5
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for cmd, kw in calls:
        assert cmd[0] == "git" and kw.get("creationflags") == flag == vault_git.NO_WINDOW
        assert kw.get("capture_output") and not kw.get("shell")
