"""Job kinds and a fake transport that the core process tests load into a real core through
WAGGLE_CORE_PLUGINS. Every effect is appended to a file in the (temporary) Waggle home so the tests can count
how often something ran. No tests live here."""
import json
import os
import time
from pathlib import Path

import waggle.config as config
from waggle.core import inbound, jobs, sender


def _log(name, line):
    with open(Path(config.HOME) / name, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def echo(payload):
    _log("runs.log", f"echo {payload.get('n')} late={payload.get('late_s')}")
    return payload


def slow_first(payload, job):
    _log("runs.log", f"slow start attempts={job['attempts']}")
    if job["attempts"] == 0:
        time.sleep(300)
    _log("runs.log", "slow done")
    return {"attempts": job["attempts"]}


def detached(payload):
    _log("runs.log", f"detached start {os.getpid()}")
    time.sleep(payload.get("sleep", 3))
    _log("runs.log", f"detached done {os.getpid()}")
    return {"pid": os.getpid()}


def hang(payload):
    time.sleep(300)


class SendError(Exception):
    def __init__(self, message, *, retry_after=None, permanent=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


def transport(row):
    body = row["body"]
    if body == "slow":
        _log("sent.log", f"begin {row['id']}")
        time.sleep(2)
    if body == "fail-once" and row["attempts"] == 1:
        raise SendError("HTTP 429", retry_after=1)
    if body == "never":
        raise SendError("chat not found", permanent=True)
    _log("sent.log", f"sent {row['id']}")
    return f"fake-{row['id']}"


def turn(msg):
    """Inbound handler. Text drives it: "sleep:<s> ..." sleeps, "die-once" hangs on attempt 1 (the test kills the
    core there), "die-always" hangs on every attempt, "raise" fails."""
    text = msg["text"]
    record = {"chat": msg["chat"], "text": text, "attempt": msg["attempt"], "turn_id": msg["turn_id"]}
    _log("turns.log", json.dumps({**record, "event": "start", "t": time.time()}))
    if text.startswith("sleep:"):
        time.sleep(float(text.split()[0].split(":")[1]))
    if text == "die-always" or (text == "die-once" and msg["attempt"] == 1):
        time.sleep(300)
    if text == "raise":
        raise RuntimeError("handler broke on purpose")
    _log("turns.log", json.dumps({**record, "event": "end", "t": time.time()}))


def register_core():
    jobs.register("t.echo", echo)
    jobs.register("t.slow", slow_first, with_job=True, lease_s=5)
    jobs.register("t.detached", detached, detached=True, lease_s=5)
    jobs.register("t.hang", hang, timeout_s=1)
    jobs.register("t.dhang", hang, detached=True, timeout_s=2, max_attempts=1)
    sender.register("text", transport)
    inbound.register(turn)
