# Handoff (2026-11-20 night)

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

## Added 2026-11-20 night (main 459dfd1 + chat fix 1dc0d71)

| Part | State |
|---|---|
| Mail watch (`waggle/mailwatch.py`) | Live. IMAP + app password (Windows Credential Manager `waggle-mail-imap:personal`), no Claude. Core timer `knowledge.mailwatch` every 5 min 06:00-23:00. Triage: rules + local model `mailwatch_local_model` = gemma4. Seen-store `~/.waggle/mailwatch.db`. Real-inbox dry run caught the college email correctly; school account is blocked by its admin. `python -m waggle.mailwatch status|run --dry-run|login` |
| Drop folder (`waggle/dropbox_ingest.py`) | Live. Desktop shortcut "Feed Waggle" -> `WaggleVault/Drop`; files become `Sources/<YYYY-MM>/*.md` with local summaries; weekly tidy plan Sunday 10:00. No Claude |
| Calendar watch (`waggle/calendar_watch.py`, branch rebuild/calendar) | Built, not live until merged and the owner runs login. Google Calendar's secret iCal address (Credential Manager `waggle-calendar-ics:<name>`), stdlib only, no Claude. Core timers `calendar.fetch` every 15 min 06:00-22:45 and `calendar.headsup` every 5 min: a text 30 min before each timed event, once. Today's events feed the morning brief (`brief.calendar_today`). `python -m waggle.calendar_watch login|status|today|run --dry-run` |
| PC voice (`waggle/pcvoice.py`, task "Waggle PC Voice") | Live, untested with the owner's real voice. Hotkey Ctrl+Alt+` (Ctrl+Alt+Space is taken), wake word "Waggle" on, speaks Waggle's own messages when the owner is at the PC (`waggle/presence.py`) |
| Test client (`tools/testclient/`, venv `~/.waggle-testclient`) | Logged in as the owner. `ask.py "question"` sends `[waggle-test] question` to the owner's normal bot chat (@hermes_t2pnnyl42gz3f5eu_bot) and prints every reply; the mark makes the turn origin "test" (`chat_engine.test_marked`). No Waggle Lab group |
| Local models | `local_model` = qwen3.6:35b-a3b (MoE, 73 tok/s, cold load ~2 min), mail = gemma4. Eval (16 emails): both 14/16, waggle-4b 12/16. qwen3.8:27b not yet downloaded: download only at night (see memory no-heavy-work-while-user-at-pc) |
| Hands-free phone line | Tailscale Serve live at https://pranavaura.tailf0a221.ts.net/voice (answers 401 without the token); owner's iPhone Shortcut set up, not yet proven |
| Tools | All Hermes toolsets enabled; chat tools include Write/Edit; hidden on purpose: Higgsfield (paid), Onshape (school cap), REA (size), Miro (sign-in) |

### Found by the first end-to-end test (fix 1dc0d71, deploy pending if not on main)
Waggle refused the owner's message "usage limit reached" while the real window was 51%: chat consulted
`quota.can_escalate` (estimate said 97%) and the core inbound path passed no `local=`. Now chat is refused only on a
recorded block (`quota._state()["blocked_until"]`) and the local model answers then. Prove with
`tools/testclient/ask.py` after deploy. The estimate itself (`quota.learned_cap`) is still wrong for background work.

### Next, in order
1. Deploy 1dc0d71, then prove with ask.py: a normal question, a reminder, a file create+delete (owner permission rule).
2. Fix the window estimate for background work (it reads ~2x the real usage), or read the real usage if any local source has it.
3. Mail alert polish, seen live 2026-11-20 20:35: 4 Google security alerts became 4 texts (group same sender+subject within an hour into one); the summary says "He needs"/"The user needs" (write to the owner as "you"); the model invented "Deadline: Tue Nov 24" from "early next week" (only state a deadline written as a date in the mail).
4. Infrastructure still to build: learning loop from feedback; Waggle proposing its own code changes (owner approves merges);
   daily automatic end-to-end checks with ask.py; cron jobs -> core timers + one off switch; photos/files/commands on
   the engine; autonomy switched on with charters (money experiment: lawful, $0, honest); calendar without Claude;
   phone calls (Twilio); settings changes by request (voice, speed, quiet hours).

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
5. `python -m waggle.core stop` still does not pause the watchdog (it restarts the core within 10 s); that is by
   design. The one off switch is `python -m waggle.core off [--for 2h] [--all]` / `on` (also `python -m waggle
   off|on|status`): it writes `~/.waggle/OFF`, which the watchdog, the logon tasks, `control.start()` and the
   gateway plug-in obey (branch rebuild/offswitch; live proof steps in its commit message). A damaged OFF file
   counts as ON, on purpose.
6. Four guard holes are kept as expected-failure tests in `tests/test_guard.py`.
7. Not built: end-to-end test client (plan step 5; needs the owner's one-time Telegram login), the closed
   learning loop (step 7), cron jobs moved to core timers (step 10), evaluation suites (11), autonomous work
   (12), Waggle changing its own code (13). The README is out of date.
8. The stray second gateway task "Hermes_Gateway" re-enables itself. It belongs to a second Hermes home,
   `%LOCALAPPDATA%\hermes` (the default home, so Hermes gives it the bare name; `~/.hermes` gets
   `Hermes_Gateway_<suffix>`). Hermes registers it (`hermes_cli/gateway_windows.py`: `install()` -> `_install_scheduled_task`,
   and `reconcile_scheduled_task()` from `start()` re-registers it when its definition drifts). Its launcher
   `gateway-service\Hermes_Gateway.vbs` is currently renamed `.vbs.disabled`, so the task is registered but starts
   nothing. `off --all` does not stop that second gateway (cron jobs of that home); stop it with `hermes gateway
   stop` under HERMES_HOME=%LOCALAPPDATA%\hermes.

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
