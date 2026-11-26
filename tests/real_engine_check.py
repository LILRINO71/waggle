"""Manual check (not collected by pytest): the long-lived chat session against the REAL claude CLI, in a
throwaway Waggle home, with no gateway and no Telegram. Uses the user's Claude login and quota.

usage: python tests/real_engine_check.py <repo> <empty_temp_home> prep cold warm tool killmid resume killearly stop
       pre-warm check: ... prep cold pause prewarm oneline oneline2 pause prewarm oneline stop
       (add `strict` before `cold` to compare a start with --strict-mcp-config)
       streaming check (needs `python -m waggle.guard approve --yes` in the repo for this WAGGLE_HOME):
       ... warm plain toolpara oneline stop  -> per step: first_visible_s (first post, a tool status line included),
       ttfw_s (first real words), total_s and every post / edit with its time
Every step is one user message (one real call), except killearly (the killed send plus the automatic retry).
Prints one JSON line per step; raw stream-json goes to <home>/raw_stream.jsonl.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

if __name__ != "__main__":  # imported by pytest (named on its command line): never run against a real home.
    import pytest  # 2026-11-26 it read "-p" from pytest's argv as the home and overwrote ~/.waggle/config.json
    pytest.skip("a manual script: run it with python, not pytest", allow_module_level=True)

repo, home = Path(sys.argv[1]), Path(sys.argv[2])
os.environ["WAGGLE_HOME"] = str(home)
os.environ["WAGGLE_ORIGIN"] = "test"
sys.path.insert(0, str(repo))

from waggle.config import Settings  # noqa: E402

work = home / "work"
work.mkdir(parents=True, exist_ok=True)
for n in ("alpha", "beta", "gamma"):
    (work / f"{n}.txt").write_text("x", encoding="utf-8")
s = Settings(vault=str(home / "vault"), jobs_dir=str(home / "jobs"), agent_workdir=str(work), quiet_hours=[0, 0],
             edit_interval_s=1.2)
s.save()

from waggle import chat_engine, traces  # noqa: E402
from waggle.chat_session import ChatSession  # noqa: E402
from waggle.delivery import CollectDelivery, Delivery  # noqa: E402

raw = open(home / "raw_stream.jsonl", "a", encoding="utf-8")
cmds = open(home / "cmds.txt", "a", encoding="utf-8")


def popen(cmd, **kw):
    t = time.time()
    p = subprocess.Popen(cmd, **kw)
    cmds.write(json.dumps({"t": t, "flags": [c for c in cmd if c.startswith("--")]}) + "\n")
    cmds.flush()
    real = p.stdout

    class Tee:
        def __iter__(self):
            for line in real:
                raw.write(line)
                raw.flush()
                yield line
    p.stdout = Tee()
    return p


CHAT = "test:engine-real"
sess = ChatSession(CHAT, settings=s, popen=popen, transcript=traces.chat_transcript)
sess.origin = "test"  # the pre-warmed process must already be the one the test turns use


class TimedDelivery(Delivery):
    """Records every post and edit with the seconds since the message was sent."""
    can_edit, max_len, name = True, 4096, "timed"

    def __init__(self):
        self.log, self.t0 = [], time.time()

    def post(self, text):
        self.log.append(("post", round(time.time() - self.t0, 2), text))
        return 1

    def edit(self, handle, text):
        self.log.append(("edit", round(time.time() - self.t0, 2), text))
        return True


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


class KillOnFirstText(CollectDelivery):
    """Kills the claude process (whole tree) the moment the first words are shown: a crash mid-answer."""
    def post(self, text):
        h = super().post(text)
        if len(self.messages) == 1:
            self.killed_at = time.time()
            kill_tree(sess.proc.p.pid)
        return h


def turn(label, text, delivery=None):
    d = delivery or CollectDelivery()
    t0 = time.time()
    rec = chat_engine.run_turn(CHAT, text, d, platform="test", origin="test", context="", settings=s, session=sess,
                               t0=t0)
    out = {"step": label, "answered_by": rec.answered_by, "outcome": rec.outcome, "why": rec.outcome_why,
           "first_visible_s": rec.first_visible_s, "ttfw_s": rec.ttfw_s, "total_s": rec.total_s,
           "posts_edits": [(k, t, text[:40]) for k, t, text in getattr(d, "log", [])], "reply": rec.reply[:300], "session": rec.session,
           "tools": [{k: st.get(k) for k in ("tool", "ok", "denied")} | {"result": (st.get("result") or "")[:120]}
                     for st in rec.steps], "trace_id": rec.trace_id, "claims": rec.claims}
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return rec


for step in sys.argv[3:]:
    if step == "prep":  # the daily `claude mcp list` cache, so the cold turn measures the session start only
        from waggle import claude_cli
        t = time.time()
        names = claude_cli.mcp_servers(refresh=True)
        print(json.dumps({"step": "prep", "mcp_servers": len(names), "s": round(time.time() - t, 1)}), flush=True)
    elif step == "strict":  # compare: only the --mcp-config servers (waggle), no user/plugin/claude.ai servers
        import waggle.chat_session as cs
        _orig = cs.build_cmd

        def strict_cmd(*a, **k):
            cmd, model = _orig(*a, **k)
            return cmd + ["--strict-mcp-config"], model
        cs.build_cmd = strict_cmd
        print(json.dumps({"step": "strict"}), flush=True)
    elif step == "cold":
        turn("cold", "Reply with exactly the word: ready")
    elif step == "warm":
        turn("warm", "Remember the word PELICAN for later. Reply with one short sentence.")
    elif step == "tool":
        turn("tool", "Use the Glob tool to list the .txt files in the current folder, then tell me how many there "
                     "are and their names. Keep it to one sentence.")
    elif step == "killmid":
        turn("killmid", "Write the numbers from one to sixty as words, one per line, nothing else.",
             KillOnFirstText())
    elif step == "resume":
        turn("resume", "What word did I ask you to remember? Answer with that one word.")
    elif step == "killearly":
        real_send = None

        def send_then_die(text, _orig=None):
            sess.proc.__class__.send(sess.proc, text)
            sess.proc.send = real_send
            kill_tree(sess.proc.p.pid)  # dies after receiving the message, before any words
        if not sess.alive():
            sess._start()
        real_send = sess.proc.send
        sess.proc.send = send_then_die
        turn("killearly", "What is 6 times 7? Answer with the number only.")
    elif step == "pause":  # stop the process cleanly (as the reaper or a core restart would)
        sess.stop(grace=5.0)
        print(json.dumps({"step": "pause", "alive": sess.alive()}), flush=True)
    elif step == "prewarm":  # start the process without a message (what chat.prewarm does at core start)
        t = time.time()
        r = sess.warm()
        time.sleep(15)  # the user's message comes later; MCP servers finish connecting meanwhile
        print(json.dumps({"step": "prewarm", **r, "warm_call_s": round(time.time() - t - 15, 2)}), flush=True)
    elif step == "plain":
        turn("plain", "Write a plain-prose explanation of how a rainbow forms, about 150 words, in two short "
                      "paragraphs. No lists.", TimedDelivery())
    elif step == "toolpara":
        turn("toolpara", "Run the shell command `echo hello-from-shell` with the Bash tool, then write a paragraph "
                         "of about 80 words about why shells are useful. No lists.", TimedDelivery())
    elif step == "oneline":
        turn("oneline", "In one short sentence: what is the capital of France?", TimedDelivery())
    elif step == "oneline2":
        turn("oneline2", "In one short sentence: what is the largest planet?")
    elif step == "stop":
        sess.stop(grace=5.0)
        print(json.dumps({"step": "stop", "alive": sess.alive()}), flush=True)
