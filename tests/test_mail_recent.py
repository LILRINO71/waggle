"""mail_recent: the chat's read of the mail watch's triage (no Claude, no IMAP in the turn)."""
import json
import time

from waggle import mailwatch, policy, toolkit, waggle_mcp


def add(con, mid, ago_h, importance, want, subject="s", sender="x"):
    t = time.time() - ago_h * 3600
    con.execute("INSERT INTO messages (id, account, received, received_ts, sender, subject, importance, want, deadline, "
                "url, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mid, "personal", "2026-11-25T08:00", t, sender, subject, importance, want, "", "https://mail/x", t))


def body(out):
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


def test_only_mail_that_needs_something_unless_all(settings):
    con = mailwatch._db()
    add(con, "1", 2, "high", "reply with your shirt size", "Team shirts", "Coach")
    add(con, "2", 3, "normal", "nothing", "Newsletter")
    add(con, "3", 5, "skip", "", "Sale")
    add(con, "4", 80, "high", "sign the form", "Old")
    con.close()
    out = toolkit.mail_recent({})
    assert out.startswith("<untrusted")             # mail text is outside data
    got = body(out)["messages"]
    assert [m["subject"] for m in got] == ["Team shirts"] and got[0]["want"] == "reply with your shirt size"
    assert [m["subject"] for m in body(toolkit.mail_recent({"all": True}))["messages"]] == ["Team shirts", "Newsletter", "Sale"]
    assert "Old" in [m["subject"] for m in body(toolkit.mail_recent({"days": 7}))["messages"]]


def test_no_store_yet_is_said_plainly(settings):
    assert "no records yet" in toolkit.mail_recent({}) or body(toolkit.mail_recent({}))["messages"] == []


def test_it_is_a_read_and_always_loaded():
    assert policy.WAGGLE_TOOLS["mail_recent"][0] == policy.ALLOW
    assert "mail_recent" in waggle_mcp.ALWAYS_LOAD and len(waggle_mcp.ALWAYS_LOAD) <= 15
