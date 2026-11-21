# Handoff (2026-11-20, late night)

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

Photos, image documents, other files, audio files (transcribed up to 10 min), captions, stickers and `/waggle`
now go through the engine too (`waggle/media_intake.py`, `waggle/engine_commands.py`; files under
`~/.waggle/media/<chat>/`, kept 14 days). The old path (Hermes turn through the plugin's router) still answers:
videos, locations, Hermes's own slash commands (/new /status /model ...), other chats.

## Added 2026-11-20 night (main 459dfd1 + chat fix 1dc0d71)

| Part | State |
|---|---|
| Mail watch (`waggle/mailwatch.py`) | Live. IMAP + app password (Windows Credential Manager `waggle-mail-imap:personal`), no Claude. Core timer `knowledge.mailwatch` every 5 min 06:00-23:00. Triage: rules + local model `mailwatch_local_model` = gemma4. Seen-store `~/.waggle/mailwatch.db`. Real-inbox dry run caught the college email correctly; school account is blocked by its admin. `python -m waggle.mailwatch status|run --dry-run|login` |
| Drop folder (`waggle/dropbox_ingest.py`) | Live. Desktop shortcut "Feed Waggle" -> `WaggleVault/Drop`; files become `Sources/<YYYY-MM>/*.md` with local summaries; weekly tidy plan Sunday 10:00. No Claude |
| Calendar watch (`waggle/calendar_watch.py`) | Deployed; idle until the owner runs `python -m waggle.calendar_watch login` (steps in USER_TESTS.md). Google Calendar's secret iCal address (Credential Manager `waggle-calendar-ics:<name>`), stdlib only, no Claude. Core timers `calendar.fetch` every 15 min 06:00-22:45 and `calendar.headsup` every 5 min: a text 30 min before each timed event, once. Today's events feed the morning brief (`brief.calendar_today`). `python -m waggle.calendar_watch login|status|today|run --dry-run` |
| PC voice (`waggle/pcvoice.py`, task "Waggle PC Voice") | Live, untested with the owner's real voice. Hotkey Ctrl+Alt+` (Ctrl+Alt+Space is taken), wake word "Waggle" on, speaks Waggle's own messages when the owner is at the PC (`waggle/presence.py`) |
| Test client (`tools/testclient/`, venv `~/.waggle-testclient`) | Logged in as the owner. `ask.py "question"` sends `[waggle-test] question` to the owner's normal bot chat (@hermes_t2pnnyl42gz3f5eu_bot) and prints every reply; the mark makes the owner's turn origin "canary": answered, delivered (reminders arrive), owner-authorised in the owner's own engine chat, never learned from. `--file x.png` sends a photo or document with the marked caption; `--raw "/waggle"` sends a slash command unmarked (in Git Bash set MSYS_NO_PATHCONV=1). No Waggle Lab group |
| Local models | `local_model` = qwen3.6:35b-a3b (MoE, 75 tok/s), mail = gemma4. Eval 2026-11-20 22:40 against qwen3.8:27b (downloaded, kept on disk, unused): triage 15/16 vs 14/16 (the 27b missed the Google security alert), reasoning 7/8 vs 6/8, short answer 0.5 s vs 2.7 s, 3-step plan 8 s vs 72 s (the 27b does not fit the 12 GB GPU: 13 tok/s). Kept qwen3.6. Scripts: `tools/model_eval.py`, `tools/model_reason_eval.py` |
| Real usage | Every stream-json `claude -p` run carries a `rate_limit_event` (five_hour utilization and resetsAt). `quota.note_real_usage` keeps it in `~/.waggle/real_usage.json`; `quota.status()` reports `source: real` with the true percent over the fixed window (the old rolling 5 h sum read about 2x: 75% when the real number was 54%). The cap is calibrated from readings |
| Daily canary (`waggle/canary.py`, `tools/testclient/canary.py`) | Live. 12:30 daily: a marked round trip (2.2 s) and a 1-minute reminder (65 s), both deleted from the chat after a pass; a failure sends one text and one ntfy push a day. `python -m waggle.canary run [--no-reminder] [--no-alert]`, `status`; ledger `canary.roundtrip` / `canary.reminder` |
| Settings by chat (`waggle/comfort.py`, MCP tool `settings`) | Live, proven: "set your voice speed to 1.2" changed it, "set it back to 1.3" restored it. Allowlist: voice, speed, quiet hours, brief and evening time, mail watch on/off; never a guard CONFIG_KEY; only in the owner's own turn |
| Off switch (`waggle/core/offswitch.py`) | Live, proven: `python -m waggle.core off --for 3m` stopped the core and PC voice, and Waggle came back by itself on time. Also `on`, `--all`, `/on` in Telegram |
| Owner rule and variables | A file named through a plain one-time variable (`$p = "..."; Remove-Item $p`, Join-Path, GetFolderPath) counts as that file; anything trickier makes the hook answer RETRY_LITERAL (never "reply yes"). Proven: a desktop file created and deleted by chat with no ask |
| Mail alerts | Same sender and subject in a pass become one text ("Google (4 messages): ..."), not repeated within 60 min; summaries say "you"; a deadline only when the mail writes the date |
| Learning loop (`waggle/learnloop.py`) | Live. Timer 30 min before the evening note: the owner's corrections (origin user only), quiz misses and failed turns go to the ledger `improvements` in knowledge.db; fact fixes are re-checked against retrieval and the held-out quiz, kept or reverted, and listed in the evening note with `undo L<n>` (typed, no model). Items that need code are listed, not auto-built. Sandbox proof on real data: `py -3.14 tools/learnloop_proof.py` (7/7 on 2026-11-20) |
| Code proposals (`waggle/proposals.py`) | Live. `python -m waggle.proposals file --title .. --evidence ..` (or the owner asks in chat: MCP tool `file_proposal`). A Sonnet job builds it in `~/autonomy-worktrees/waggle/<id>`; the pipeline verifies (the diff has tests, the new test fails on the base, targeted and FULL suite under `~/.waggle-locks/full-suite.lock`, safety-file check), pushes `proposals/<id>` and texts "Reply 'merge CODE' / 'drop CODE'". merge goes to overnight, then tools/deploy.ps1; a canary failure reverts and redeploys. First real one: 33HN (polite "no thanks" counted as a correction), building since 2026-11-20 23:22 |
| Autonomy | ON since 2026-11-20 23:00: goals schoolwork, ftc-robot, portfolio, waggle, money (new: $0, lawful, honest, research and notes only, expires 2026-12-20), at most 2 steps a day, only while the real window is under 60%. First step (money) proven in 26 s: `WaggleVault/Goals/money/plan-shortlist.md` |
| Open-source survey | `C:\Users\hiheo\Claude\reports\open-source-jarvis-2026-11.md` (Opus, 52 projects, 12 read in depth); closest is KenKaiii/Roamies (AGPL: ideas only). Borrow-list rows 3-13 being built overnight on rebuild/gates, rebuild/lessons, rebuild/memgit, rebuild/voice2, rebuild/tools |
| Hands-free phone line | Tailscale Serve live at https://pranavaura.tailf0a221.ts.net/voice (answers 401 without the token); owner's iPhone Shortcut set up, not yet proven |
| Tools | All Hermes toolsets enabled; chat tools include Write/Edit; hidden on purpose: Higgsfield (paid), Onshape (school cap), REA (size), Miro (sign-in) |

### Next, in order (2026-11-20 23:00)
1. Learning loop from feedback (plan step 7): a miss or a correction leads to a proposed fix, a re-check, keep or
   revert, reported in the evening note with undo.
2. Waggle proposing its own code changes (step 13): a gated worktree pipeline; the owner approves each merge.
3. Autonomy on, with charters: the four seeded goals are disabled and `autonomy` is off. Add the money experiment goal
   (lawful, $0, honest, no outward channel without the owner's yes) and switch on with the step limit.
4. Phone calls (Twilio): needs the owner's account; trial credit only, never paid.
5. Calendar: the owner runs `python -m waggle.calendar_watch login` (USER_TESTS.md). Then `ingest.sync_upcoming` and
   the evening note should read `calendar_watch` instead of the Claude connector.
6. Videos and locations still go the old path; `/waggle media clean` has no confirmation.

## Overnight 2026-11-20 -> 21 (owner asleep; mandate: build without asking, owner keeps/deletes in the morning)

Deployed 90edd27 (00:05): gates before Claude (`waggle/gates.py`, `notifycheck.py`, `cooldowns.py`; off: config
`gates_enabled` / `notify_check_enabled` / `engine_cooldowns_enabled` false), keyword lessons (`waggle/lessons.py`,
`python -m waggle.lessons list|add|remove|off`; one live lesson repos-472274 "use gh with private repos"; context
marker on every trace + canary `context` check), fact ordering (`learn._stale_against`), vault under local git
(`waggle/vault_git.py`, `/waggle memory`, `restore <sha>` = new commit; off: `vault_git` false), learnloop fix (a
regex held literal backspaces instead of \b). All proven live with ask.py.

Deployed later the same night, each proven live with ask.py or the module's own CLI:
- 7ca9e39: voice2 (PC voice intent judge 49/50, echo rejection, stop phrases, sentence streaming; off `pc_voice_smart`),
  tools (cad_convert, cad_measure, print_estimate, doc_to_pdf, media_convert, image_convert, portfolio_audit,
  explain_file; removal lines in skills/README.md), smallwins (Wolfram MCP `wolfram_mcp`, FTCScout tool, seven-day
  usage stop `seven_day_stop_at` 0.85, nightly backups 03:30 to %USERPROFILE%\WaggleBackups, proposals flake guard),
  memory2 (FTS5 hybrid search `memory_hybrid`, Mem0-style add/update/replace/skip `memory_fact_decide`; nomic-embed-text
  kept after the embedding A/B), outreach2 (natural-break hold `hold_for_breaks`, per-kind welcome odds
  `outreach_ev_enabled`, Ollama JSON schemas).
- 5c8df60 (about 03:10): cascade (`waggle/cascade.py`: background answers local -> Haiku -> Sonnet, `cascade_enabled`,
  `cascade_local`; `python -m waggle.cascade status|try "..."`; live try answered locally, 1 Claude call avoided) and
  checklist judging (`waggle/checklist.py`, `checklist_judge`). Suite 3116 passed before deploy; canary passed.
- Owner's keep-or-delete list with every off switch: overnight/MORNING.md.
- 8127b88 (about 01:30): proposal 7XJP's fix (traces.CORRECTION skips "no thanks/worries/problem", "no, that's
  fine"). Its own merge (1afd175) failed the deploy canary with "database is locked" because an ask.py run still held
  the Telethon session; the proposal rolled itself back (67b6a1b) correctly. Fix: tools/testclient/tclib.SessionLock
  (ask/canary/ping/login hold it while connected, wait up to 180 s; canary child timeout includes it), then the
  revert was reverted. Proven live: canary started during an ask.py run waited 27 s and passed. Suite 3138 passed.
  The stale "reply merge 7XJP" text (outbox id 22) was expired as superseded.

- e6151a4 (about 01:40): voice3. Silero VAD gate in PC voice (`waggle/vad.py`, `mic.SileroGate`, default `pc_voice_vad` silero; off: "energy"); Smart Turn v3.2 built but `pc_voice_endturn` stays "silence" (it cut 163/184 thinking pauses on the synthetic bench). Models fetched with `python -m waggle.vad fetch` into ~/.waggle/models, sha256-pinned; pcvoice.log shows no fallback after the restart; Silero on tests/fixtures/endturn/complete.wav 49/51 frames speech, silence 0.01. Untried on a real voice. Bench: tools/endturn_eval.py, docs/PC_VOICE.md.

Nothing from the overnight list is still open.

Research reports (Claude\reports): open-source-jarvis-2026-11.md, tools-mcp-skills-2026-11.md,
research-for-waggle-2026-11.md, adopt-vs-build-2026-11.md; vault summaries in WaggleVault/Sources/2026-11/.
Vault notes written: Waggle/How to use Waggle.md, Waggle/Changelog.md, Waggle/Local model choice.md.
Autonomy first step (money) proven: WaggleVault/Goals/money/plan-shortlist.md (note for the owner: most freelance and
payment platforms need 18+ or a parent's account).
Lesson: the Bash tool collapses double backslashes; write any patch with backslashes through a file (Write tool).
Lesson: run the full suite only while few agents run tests, or rerun failures alone before believing them.

## Proven on the real machine

- Core killed -> back in 57 s. Core frozen -> replaced in 51 s (older watchdog); the new loop watchdog is tested
  (worst case about 85 s) but not re-measured live.
- 2026-11-20 night, all with tools/testclient: a reminder set at 21:48:16 for 21:50 arrived at +128.7 s; a desktop
  file created and then deleted on the owner's word with no ask; a photo read and described correctly through the
  engine; `/waggle` answered in 1.6 s with no model; a setting changed and restored by chat; the canary round trip
  (2.2 s) and reminder (65.5 s), cleaned up afterwards; the off switch held 3 minutes and came back by itself.
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
# in the dev worktree (branch overnight): merge the feature branches, then
python -m waggle.proposals suite              # the full suite under the machine-wide lock (exit 75: another run holds it)
git push origin overnight
# then one command on the live checkout: merge, install the plug-in if it changed, guard approve + verify, restart
# the watchdog, core, PC voice, dashboard and gateway, check the running version, run a live canary
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\hiheo\Claude\waggle\tools\deploy.ps1
```
Never rely on `hermes gateway stop` alone: it misses a gateway started by hand (`tools/restart-gateway.ps1` does not).

## Rules learned the hard way

- Never run CPU-saturation tests on the machine that runs the live core.
- Development tests never touch the live home, and can never reach the real Telegram (`telegram.real_api_allowed`).
- Every child process needs the no-window handling (`tests/test_no_window_calls.py`); a visible window is a
  user-facing defect.
- Files written under AppData from inside a packaged app are private to that app. Probe what an outside
  process sees with a test-origin turn submitted to the live core with an invalid address.
- Long-lived processes keep the code they started with. The watchdog, PC voice, the dashboard and the gateway must
  be restarted on every deploy (tools/deploy.ps1), not only the core.
- A worker's report is not evidence. Run the suite, read the records, and prove it on the real interface.
- To check what really happened in a turn: `traces.db` (origin, answered_by, timings, tools with results,
  outcome), `runtime.db` (inbox, outbox, timers, jobs, events), and the Claude session file of the chat.
