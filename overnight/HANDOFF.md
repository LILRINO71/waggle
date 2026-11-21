# Handoff (2026-11-20 evening)

Read this first, then `overnight/REBUILD_PLAN.md` (the governing plan) and `overnight/USER_TESTS.md`.

## What is live

The live checkout is `main`. Merging to `main` is a deploy: scheduled scripts and the tool hook run straight
from it. Feature work happens in worktrees on `rebuild/*` branches, all pushed to origin.

| Part | State |
|---|---|
| Core process (`python -m waggle.core run`) | Task Scheduler task "Waggle Core" at logon; one long-lived watchdog ("Waggle Core Watchdog", `watch --loop`); health on 127.0.0.1:8896 |
| Gateway (Hermes) | Task "Waggle Gateway" at logon (`tools/install-gateway-task.ps1`), not the Startup folder |
| Chat | The owner's typed messages and voice notes go plugin admission hook -> core `POST /turn` -> `chat_engine` -> Bot API. Sonnet answers; thinking is off for chat; hard work goes to background jobs |
| Voice | Voice note -> `waggle/stt.py` -> engine -> `waggle/voice_reply.py` (at most 3 sentences / 45 words) -> voice message, then full text. Voice `bm_george`, speed 1.1 |
| Outreach | `waggle.tick` plug-in: look pass every 5 min (idle pass starts no processes), brief, evening note, reminders as one-shot core timers, "look again" decisions |
| Knowledge | `waggle.knowledge_core` plug-in: intake every 30 min, digest 21:00 and 08:00, quiz 21:30. Learns only from turns whose origin is `user` |
| Safety guard | Per-checkout manifest under `~/.waggle-guard/<hash>/`; hook and in-process gate fail closed; learned budget estimate lives in `budget.db`, clamped to 0.1x-4x of the user's value |

Old path (Hermes turn through the plugin's router) still answers: photos, files, stickers, audio files,
captions, `/commands`, other chats.

## Proven on the real machine

- Core killed -> back in 57 s. Core frozen -> replaced in 51 s (older watchdog); the new loop watchdog is tested
  (worst case about 85 s) but not re-measured live.
- Typed turns and voice notes answered through the engine on real Telegram; a reminder asked by voice fired on
  time and was delivered; two real voice replies delivered (2.8 s and 14.2 s of audio).
- No visible windows, focus changes or busy cursor from Waggle's processes (`tools/window_probe.py`, live, 242
  process starts, and again during a voice turn).
- GitHub CLI works from the gateway's context (private repos listed).
- The purge removed a false fact, learned from a development test prompt, from every store.

## Not proven or known wrong

1. The owner did not notice the reminder: it arrives as plain text. A reminder asked by voice should come back
   as a voice note plus text.
2. The chat session carries old context: it still called GitHub "public repos only" although `gh` now works.
   Check whether the session resumes yesterday's conversation, and start fresh sessions on a new day.
3. Outreach's judgement has no real-model evidence: `python tools/decision_check.py --real` was refused by
   Waggle's own budget check. Run it when the usage window is under 60%.
4. Waggle's background work (outreach decisions, digest, quiz) is deferred whenever the 5-hour window reads
   above 60%, and every other Claude session on the account counts toward it. Decide whether cheap decision
   calls get their own small allowance.
5. `python -m waggle.core stop` does not pause the watchdog (it restarts the core within 10 s). There is no
   single "Waggle off" command; uninstall with `tools/install-core-task.ps1 -Uninstall`.
6. Four guard holes are kept as expected-failure tests in `tests/test_guard.py`.
7. Not built: end-to-end test client (plan step 5; needs the owner's one-time Telegram login), the closed
   learning loop (step 7), cron jobs moved to core timers (step 10), evaluation suites (11), autonomous work
   (12), Waggle changing its own code (13). The README is out of date.
8. The stray second gateway task "Hermes_Gateway" re-enables itself.

## Deploy procedure

```
# in the dev worktree (branch overnight): merge the feature branch, then
python -m pytest -q -p no:cacheprovider        # must pass; real-process tests included
git push origin overnight
# in the live checkout
git merge --ff-only overnight ; git push origin main
python install.py                               # only when hermes_plugin/ changed (no --cron: fast)
python -m waggle.guard approve --yes ; python -m waggle.guard verify   # LIVE checkout only
py -3.14 -m waggle.core restart
hermes gateway stop --yes ; Start-ScheduledTask "Waggle Gateway"       # only when gateway-side code changed
py -3.14 -m waggle.core status
```

## Rules learned the hard way

- Never run CPU-saturation tests on the machine that runs the live core.
- Development tests never touch the live home, and can never reach the real Telegram (`telegram.real_api_allowed`).
- Every child process needs the no-window handling (`tests/test_no_window_calls.py`); a visible window is a
  user-facing defect.
- Files written under AppData from inside a packaged app are private to that app. Probe what an outside
  process sees with a test-origin turn submitted to the live core with an invalid address.
- A worker's report is not evidence. Run the suite, read the records, and prove it on the real interface.
- To check what really happened in a turn: `traces.db` (origin, answered_by, timings, tools with results,
  outcome), `runtime.db` (inbox, outbox, timers, jobs, events), and the Claude session file of the chat.
