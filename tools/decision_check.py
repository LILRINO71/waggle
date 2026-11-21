"""Run the initiative decision step on made-up findings, in a temporary Waggle home, one model call per finding.

    python tools/decision_check.py            # dry: a fake model, prints the prompt of the first finding
    python tools/decision_check.py --real     # the project's own Claude wrapper (budget check included), 8 calls

Nothing is sent and nothing real is read for the findings: the home is a temp folder (WAGGLE_HOME, HERMES_HOME), the
findings, the user's activity and the conversation are made up below. The Claude wrapper still applies Waggle's own
budget check; when it refuses (usage window full), the script says so and stops: it never goes around it.
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HOME = tempfile.mkdtemp(prefix="waggle-decision-check-")
os.environ["WAGGLE_HOME"] = str(Path(HOME) / "home")
os.environ["HERMES_HOME"] = str(Path(HOME) / "hermes")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from waggle import claude_cli, initiative, watchers  # noqa: E402
from waggle.config import Settings  # noqa: E402

DAY = dt.date(2026, 11, 20)


def at(h, m=0):
    return dt.datetime.combine(DAY, dt.time(h, m)).timestamp()


ACTIVITY = {"robot-code": ["Add intake sensor driver", "Fix the hopper timeout"]}
CHAT = ["[Thu 15:40] user: I'll test the shooter tomorrow after practice | waggle: Good plan, tell me how it goes",
        "[Fri 10:05] user: the intake sensor keeps reading 0 on the robot | waggle: Check the I2C address first"]

# (name, expected, now, signal)
CASES = [
    ("meeting in 20 minutes", "text now", at(14, 10),
     watchers.signal("m1", "upcoming", "FTC team meeting starts in 20 minutes (14:30)", "Deadline.", urgent=True)),
    ("pull request waiting 3 days", "text or evening", at(14, 10),
     watchers.signal("p1", "github_pr", "robot-code: PR #12 'Add intake sensor' waiting for your review for 3 days",
                     "Opened by a teammate.", project="robot-code")),
    ("newsletter", "evening or none", at(14, 10),
     watchers.signal("n1", "mail", "Newsletter: 'This week in robotics' from Robotics Weekly", "")),
    ("tests failing on today's project", "text now", at(14, 10),
     watchers.signal("t1", "github_ci", "robot-code: 'tests' failed on main", "The run ended failure.",
                     project="robot-code")),
    ("check-in: test the shooter", "text (ask how it went) or later", at(19, 5),
     watchers.signal("followup:c1", "followup", "You said yesterday you'd test the shooter after practice",
                     'Their words: "I\'ll test the shooter tomorrow after practice"', "followup:c1")),
    ("marketing mail", "none", at(14, 10),
     watchers.signal("k1", "mail", "Flash sale: 40% off all servos this weekend only!", "Promotional email.")),
    ("failing build on a stale project", "evening or none", at(14, 10),
     watchers.signal("s1", "github_ci", "old-game-prototype: 'build' failed on main",
                     "Latest run failure. Last commit to this repo was 3 weeks ago.", project="old-game-prototype")),
    ("deadline tomorrow", "evening", at(14, 10),
     watchers.signal("d1", "upcoming", "Scholarship application due (Sat Nov 21)", "Deadline.")),
]


def main():
    real = "--real" in sys.argv
    s = Settings.load()
    s.vault = str(Path(HOME) / "vault")  # an empty vault: the real one is never read
    s.quiet_hours = [23, 7]
    s.watch_roots = []
    s.save()
    base_context = initiative.context

    def context(settings, now, activity=None):
        return base_context(settings, now, activity) + "\n\nRecent conversation (newest first):\n" + "\n".join(CHAT)

    initiative.context = context
    shown = []

    def fake(prompt, **k):
        if not shown:
            shown.append(1)
            print(prompt)
            print("---")
        return {"json": {"decisions": []}}
    for i, (name, expected, now, sg) in enumerate(CASES):
        sg = {**sg, "urgent": bool(sg.get("urgent"))}
        try:
            got = initiative._ask_claude([sg], s, now, asker=None if real else fake, activity=ACTIVITY)
        except claude_cli.ClaudeError as e:
            print(f"STOPPED at '{name}': {e} (kind={getattr(e, 'kind', '')}). The real check could not run.")
            return 2
        if not real and i == 0:
            print(initiative.SYSTEM, "\n---\n")
        d = got.get(sg["key"]) or {}
        print(f"{name:36s} expect {expected:32s} -> {d.get('action')}"
              + (f" in {d.get('look_again_minutes')} min" if d.get("look_again_minutes") else "")
              + f" | {d.get('reason')} | {d.get('message')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
