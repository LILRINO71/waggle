"""Step 5: Waggle's own promises become dated check-ins unless a tool in the same turn already keeps them."""
import datetime as dt

from waggle import followups
from waggle.core import store

NOW = dt.datetime(2026, 11, 23, 16, 0)


def teardown_function():
    store.close_all()


def test_a_dated_promise_is_found():
    out = followups.promises("Got it. I'll check the shooter numbers tomorrow morning and let you know.", [], NOW)
    assert len(out) == 1 and out[0][0].startswith("check the shooter numbers")
    assert dt.datetime.fromtimestamp(out[0][1]).date() == dt.date(2026, 11, 24)


def test_a_promise_kept_by_a_tool_or_without_a_date_is_not_tracked():
    assert followups.promises("I'll remind you at 7pm.", ["mcp__waggle__remind_me"], NOW) == []
    assert followups.promises("I'll check it.", [], NOW) == []
    assert followups.promises("If you want, I'll check it tomorrow?", [], NOW) == []
    assert followups.promises("Done: the doc is ready.", [], NOW) == []


def test_capture_files_the_promise_once(settings):
    t = NOW.timestamp()
    row = {"id": 5, "time": t, "request": "can you look into the shooter later", "origin": "user",
           "platform": "telegram", "session": "telegram:1", "chat": "telegram:1",
           "reply": "Sure. I'll look into the shooter speed tomorrow at 10am.", "steps": []}
    added = followups.capture(settings, now=t, rows=[row])
    promised = [a for a in added if a["kind"] == "promise"]
    assert len(promised) == 1 and "shooter speed" in promised[0]["what"]
    sig = followups.signal_for(promised[0], now=t + 86400)
    assert "Waggle said" in str(sig) and "it would look into the shooter speed" in str(sig)
    assert followups.capture(settings, now=t + 60, rows=[row]) == []  # the cursor moved on
