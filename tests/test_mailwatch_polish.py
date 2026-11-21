"""Mail watch polish: repeated alerts are one text, the summary speaks to the owner as "you", and a deadline is shown
only when the mail itself writes that date. Same fakes as test_mailwatch (fake IMAP, fake local model, fake clock via
run_pass(now=...)); nothing real is reached."""
import datetime as dt
import re
import sqlite3

import pytest

from test_mailwatch import FakeImap, Local, at, mail, set_up_imap, texts

from waggle import mailwatch
from waggle.config import data_path

GOOGLE = "Google <no-reply@accounts.google.com>"


def google(n, subject="Security alert", when=None, frm=GOOGLE):
    return mail(n, frm, subject, f"Body number {n}: a new sign-in to your account.", when or at(11, n))


def verdict(msgs):
    """High for everything; the summary names which message it read (so the newest one can be told apart)."""
    body = msgs[1]["content"]
    n = body.split("Body number ")[1].split(":")[0] if "Body number " in body else "?"
    return {"importance": "high", "want": f"check the sign-in (message {n})", "deadline": ""}


def rows():
    con = mailwatch._db()
    try:
        return {r["id"].split(":")[1]: dict(r) for r in con.execute("SELECT * FROM messages")}
    finally:
        con.close()


# ---- 1. repeats are one alert -----------------------------------------------------------------------------------

def test_normalised_subjects_and_group_keys():
    assert mailwatch.norm_subject("  RE: Fwd:  Security   ALERT ") == "security alert"
    assert mailwatch.norm_subject("Fw: re: fwd: Hello") == "hello"
    assert mailwatch.norm_subject("Re:") == ""
    a = {"account": "p", "address": "No-Reply@Accounts.Google.com", "subject": "Re: Security alert"}
    b = {"account": "p", "address": "no-reply@accounts.google.com", "subject": "SECURITY  alert"}
    assert mailwatch.group_key(a) == mailwatch.group_key(b) is not None
    assert mailwatch.group_key({**b, "account": "school"}) != mailwatch.group_key(b)
    assert mailwatch.group_key({**b, "subject": "Re:"}) is None  # nothing to group on


def test_four_google_alerts_in_one_pass_are_one_text_with_the_newest_summary_and_link(settings, monkeypatch):
    other_sender = mail(7, "Other <alerts@other.example.com>", "Security alert", "Body number 7: hello", at(11, 7))
    other_subject = google(8, "Password changed")
    inbox = [google(1), google(2, "Re: Security alert"), google(3, "FWD:  security   alert"), google(4),
             other_sender, other_subject]
    set_up_imap(monkeypatch, FakeImap(inbox))
    out = mailwatch.run_pass(now=at(12), local=Local(verdict))
    assert out["high"] == 6 and out["alerted"] == 3 and out["accounts"]["personal"]["grouped"] == 3
    group = [t for t in texts() if "(4 messages)" in t]
    assert len(group) == 1 and len(texts()) == 3
    assert group[0] == ("📬 [personal] Google (4 messages): Security alert - check the sign-in (message 4)\n"
                        "https://mail.google.com/mail/u/owner@example.com/#inbox/18b0000000000004")
    assert any(t.startswith("📬 [personal] Other: check the sign-in (message 7)") for t in texts())
    assert any(t.startswith("📬 [personal] Google: check the sign-in (message 8)") for t in texts())
    got = rows()
    assert all(r["alerted_at"] is not None and r["group_key"] for r in got.values())
    covered = {k: r["grouped_with"] for k, r in got.items() if r["grouped_with"]}
    assert set(covered) == {"18c0000000000001", "18c0000000000002", "18c0000000000003"}
    assert set(covered.values()) == {"personal:18c0000000000004"}


def test_a_group_stays_one_text_when_it_falls_under_the_more_summary(settings, monkeypatch):
    inbox = [google(n, f"Alert {n}") for n in range(1, 7)] + [google(10), google(11)]  # 6 singles + 1 pair
    set_up_imap(monkeypatch, FakeImap(inbox))
    out = mailwatch.run_pass(now=at(12), local=Local(verdict))
    assert out["alerted"] == mailwatch.MAX_ALERTS_PER_PASS + 1  # five texts and one "more" summary
    assert all(r["alerted_at"] is not None for r in rows().values())  # nothing is left to be sent again
    assert len(texts()) == 6


# ---- across passes, with a fake clock ---------------------------------------------------------------------------

def test_no_second_text_within_the_hour_but_one_after_60_minutes(settings, monkeypatch):
    server = set_up_imap(monkeypatch, FakeImap([google(1, when=at(11, 55))]))
    local = Local(verdict)
    assert mailwatch.run_pass(now=at(12), local=local)["alerted"] == 1
    # 20 minutes later another one from the same sender+subject (Re: and case do not matter): held back
    server.inbox.append(google(2, "re: SECURITY ALERT", when=at(12, 20)))
    out = mailwatch.run_pass(now=at(12, 25), local=local)
    assert out["alerted"] == 0 and out["accounts"]["personal"]["repeats_held_back"] == 1 and len(texts()) == 1
    held = rows()["18c0000000000002"]
    assert held["alerted_at"] is not None and held["outbox_id"] is None and held["grouped_with"] == "personal:18c0000000000001"
    # the held message is never read or sent again, and being held did not restart the hour
    assert mailwatch.run_pass(now=at(12, 30), local=local)["new"] == 0
    server.inbox.append(google(3, when=at(12, 58)))
    out = mailwatch.run_pass(now=at(13, 1), local=local)  # 61 minutes after the first text
    assert out["alerted"] == 1 and len(texts()) == 2
    assert "message 3" in texts()[1]
    assert mailwatch.run_pass(now=at(13, 6), local=local)["new"] == 0 and len(texts()) == 2


def test_the_hour_is_exactly_60_minutes(settings, monkeypatch):
    server = set_up_imap(monkeypatch, FakeImap([google(1, when=at(11, 58))]))
    local = Local(verdict)
    mailwatch.run_pass(now=at(12), local=local)
    server.inbox.append(google(2, when=at(12, 56)))
    assert mailwatch.run_pass(now=at(12, 59), local=local)["alerted"] == 0
    server.inbox.append(google(3, when=at(12, 58)))
    assert mailwatch.run_pass(now=at(13), local=local)["alerted"] == 1  # 60 minutes: allowed again


def test_a_different_subject_or_account_is_not_held_back(settings, monkeypatch):
    server = set_up_imap(monkeypatch, FakeImap([google(1, when=at(11, 55))]))
    local = Local(verdict)
    mailwatch.run_pass(now=at(12), local=local)
    server.inbox.append(google(2, "Password changed", when=at(12, 5)))
    assert mailwatch.run_pass(now=at(12, 6), local=local)["alerted"] == 1


def test_an_older_store_is_migrated_and_a_recent_alert_in_it_still_counts(settings):
    path = data_path(mailwatch.DB_NAME)
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE messages (id TEXT PRIMARY KEY, account TEXT, thread_id TEXT, received TEXT, received_ts REAL,
        sender TEXT, address TEXT, subject TEXT, importance TEXT, want TEXT, deadline TEXT, url TEXT,
        seen_at REAL, alerted_at REAL, outbox_id INTEGER);
    """)
    old.execute("INSERT INTO messages VALUES ('personal:old1','personal','','',1.0,'Google',"
                "'no-reply@accounts.google.com','Security alert','high','x','','',?,?,5)", (at(11, 50), at(11, 50)))
    old.commit()
    old.close()
    con = mailwatch._db()
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    assert {"group_key", "grouped_with"} <= cols
    assert con.execute("SELECT group_key FROM messages").fetchone()[0] == (
        "personal|no-reply@accounts.google.com|security alert")
    con.close()
    mailwatch._db().close()  # opening it again changes nothing
    m = {"id": "personal:new", "account": "personal", "address": "no-reply@accounts.google.com", "sender": "Google",
         "subject": "Security alert", "received_ts": at(12), "importance": "high"}
    con = mailwatch._db()
    send, _extra, _texts, report = mailwatch._decide([m], [], 1, con, at(12, 10))
    con.close()
    assert send == [] and report["repeats_held_back"] == 1 and m["grouped_with"] == "personal:old1"


# ---- 2. the summary speaks to the owner ---------------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    ("The user needs to reply by Friday.", "You need to reply by Friday."),
    ("The user must pick two electives.", "You must pick two electives."),
    ("The user should confirm the time.", "You should confirm the time."),
    ("The user has to submit the form.", "You have to submit the form."),
    ("The user is asked to bring the drill.", "You are asked to bring the drill."),
    ("The user wants to join? Reply.", "You want to join? Reply."),
    ("the user needs to pay", "You need to pay"),
    ("He needs to send his transcript.", "You need to send your transcript."),
    ("He must RSVP.", "You must RSVP."),
    ("She has to choose a club.", "You have to choose a club."),
    ("He is required to sign up.", "You are required to sign up."),
    ("They need to confirm the room.", "You need to confirm the room."),
    ("They are asked to bring a laptop.", "You are asked to bring a laptop."),
    ("  He needs to reply.", "You need to reply."),
])
def test_a_leading_third_person_subject_becomes_you(before, after):
    assert mailwatch.to_you(before) == after


@pytest.mark.parametrize("text", [
    "Your teacher wants a reply.",
    "Asks which electives you want next term.",
    "You need to reply.",
    "They moved the meeting to Friday.",
    "He is away next week.",
    "She is out until Monday and asks you to reply.",
    "She needs to talk to you about the schedule.",  # "you" is already in it: she is someone else
    "He needs to talk to her about it.",  # another person is in the sentence
    "They need their coach to approve it.",
    "The user's reply is needed by Friday.",
    "Someone told the user he needs to reply.",  # only a LEADING subject is touched
    "Then he needs to reply.",
    "Hello",
    "",
])
def test_text_that_does_not_clearly_address_the_owner_is_left_alone(text):
    assert mailwatch.to_you(text) == text


def test_the_model_prompt_addresses_the_reader_as_you_and_never_says_he_him_or_the_user():
    prompt = mailwatch.LOCAL_SYSTEM.lower()
    for word in ("him", "his", "he", "she", "her", "the user"):
        assert not re.search(rf"\b{word}\b", prompt), word
    assert "the reader" in prompt and 'as "you"' in prompt


def test_the_rewrite_is_applied_to_the_text_the_owner_gets(settings, monkeypatch):
    set_up_imap(monkeypatch, FakeImap([google(1)]))
    local = Local({"importance": "high", "want": "He needs to confirm the new sign-in.", "deadline": ""})
    mailwatch.run_pass(now=at(12), local=local)
    assert texts()[0].startswith("📬 [personal] Google: You need to confirm the new sign-in.")
    assert mailwatch.normalise({"id": "abc123", "subject": "s", "want": "The user has to pay."})["want"] == (
        "You have to pay.")


# ---- 3. no invented deadlines ---------------------------------------------------------------------------------------

D = dt.date(2026, 11, 24)


@pytest.mark.parametrize("text", [
    "Please reply by Nov 24.", "due November 24th", "due on 24 November", "24th of November", "Nov. 24, 2026",
    "reply by Tue, Nov 24", "Tuesday, November 24", "due 11/24", "due 11/24/2026", "due 11/24/26",
    "due 2026-11-24", "deadline: 2026-11-24 at noon", "by Tuesday the 24th", "Tues 24th", "NOV 24",
])
def test_an_explicit_date_in_the_mail_keeps_the_deadline(text):
    assert mailwatch.date_in_text(D, text)
    assert mailwatch.grounded_deadline("2026-11-24", f"Subject\n{text}") == "2026-11-24"


@pytest.mark.parametrize("text", [
    "early next week", "tomorrow", "by Friday", "Tuesday", "next Tuesday", "end of the month", "ASAP", "",
    "Nov 25", "November 2026", "Nov 240", "24th", "Monday the 24th",  # wrong day, no month, or wrong weekday
    "2026-11-25", "11/25", "24/11", "5 Nov", "may 24 people",
])
def test_relative_or_missing_dates_give_no_deadline(text):
    assert not mailwatch.date_in_text(D, text)
    assert mailwatch.grounded_deadline("2026-11-24", f"Subject\n{text}") == ""


def test_a_deadline_that_is_not_a_date_is_dropped():
    assert mailwatch.grounded_deadline("Friday", "reply by Friday Nov 20") == ""
    assert mailwatch.grounded_deadline("", "Nov 20") == ""


def test_model_invented_deadline_is_dropped_and_a_written_one_is_kept(settings, monkeypatch):
    invented = mail(1, "Ms. Rivera <rivera@lincoln-high.example.edu>", "Course requests",
                    "Which electives do you want? Please reply early next week.", at(11, 30))
    written = mail(2, "Coach <coach@robots.example.org>", "Practice", "Can you come? Reply by Nov 24 please.",
                   at(11, 40))
    set_up_imap(monkeypatch, FakeImap([invented, written]))
    local = Local({"importance": "high", "want": "asks you to reply", "deadline": "2026-11-24"})
    mailwatch.run_pass(now=at(12), local=local)
    first, second = texts()
    assert "Deadline" not in first
    assert second == ("📬 [personal] Coach: asks you to reply\nDeadline: Tue Nov 24\n"
                      "https://mail.google.com/mail/u/owner@example.com/#inbox/18b0000000000002")
    stored = {r["subject"]: r["deadline"] for r in rows().values()}
    assert stored == {"Course requests": "", "Practice": "Tue Nov 24"}


def test_a_date_in_the_subject_counts_and_the_rules_only_path_shows_no_deadline(settings, monkeypatch):
    m = mail(1, "Ms. Rivera <rivera@lincoln-high.example.edu>", "Forms due 11/24", "Please send the forms.", at(11, 30))
    set_up_imap(monkeypatch, FakeImap([m]))
    mailwatch.run_pass(now=at(12), local=Local({"importance": "high", "want": "asks for the forms",
                                                "deadline": "2026-11-24"}))
    assert "Deadline: Tue Nov 24" in texts()[0]
    # Ollama down: the rules alone decide and never invent or extract a deadline
    set_up_imap(monkeypatch, FakeImap([mail(2, "Ms. Rivera <rivera@lincoln-high.example.edu>", "Forms due soon",
                                            "Could you send the forms by Tuesday?", at(12, 5))]))
    out = mailwatch.run_pass(now=at(12, 10))
    assert out["local_model"] == "down" and "Deadline" not in texts()[1]
