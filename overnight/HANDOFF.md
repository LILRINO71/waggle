# Handoff (2026-11-20, late night)

Read this first, then `overnight/REBUILD_PLAN.md` (the governing plan) and `overnight/USER_TESTS.md`.

**2026-11-25/26, session "real-use tests + money test" (c7b3a588): all live (last deploy 2df46c8), nothing unpushed.**
- Built: `tools/scenarios.py` + `bench/scenarios.json` (11 long scenarios, Sonnet judge; all share one live chat),
  `tools/say.py`, live job progress (`waggle/progress.py`), HUD Home + Memory graph, skill scout (03:20 daily),
  `mail_recent`, vault PARA move (`tools/vault_para.py`, undo manifest in WaggleBackups\vault-para-20261125-143805).
- Fixed from 5 driven real projects: verifier paths (`objectives.resolve_named`, folder requirements, retries count
  from `steps.first_started`), real vault named in the chat prompt, guard false asks (sed parsed, import statements
  only), leading "deny CODE", calendar in local time, Phone Link/Messenger/Signal sensitive, plain job reports.
- Money objective 9b763f92 DONE: $4.99 Roblox plugin "Easing Curve Preview" + listing + REPORT.md, nothing published;
  backed up to PRIVATE repos LILRINO71/waggle-earn (a773c81, devtools/ left out) and LILRINO71/plate-generator
  (87d9eed). Owner still has to: test it in Studio, parent accepts Roblox seller terms + Stripe, approve publishing.
- Open: chat under-reports reminder delivery (no tool reads outbox state); scenario re-run 27/31 (remaining fails
  are a dropped detail and stale money expectations). No WIP branch: nothing unfinished.

**2026-11-26 afternoon, session "Waggle permission prompts": four owner complaints fixed, one incident (all live).**
- *Home page "I did not catch any words"* (6a95580): the page token is new on every dashboard start, so a page left open
  through a deploy got 403 on every recording and send. The page now re-reads the token from its own address on a 403
  and retries once (`freshToken` in `waggle/home_page.html`). Proven in the real page across a dashboard restart.
- *The conversation never ended* (c8c3423): "All right, thank you" ran a turn, then "Yep", then another. 
  `voice_intent.closing_phrase` makes a whole-utterance sign-off "done" (rule, before any model): the PC voice ends
  the conversation with the closing tone, no turn. "Thanks, and remind me at five" is still a request; a bare "yep"
  after a question is an answer. Bench label `done` (d03, d14 relabelled; c01-c04 added).
- *"On it, sir, starting now. On it, sir..."* (c8c3423): after the ack line, `trim_spoken_ack` cuts a repeat off the
  SPOKEN part before it is split and capped; the voice and chat prompts say never open with "On it" and never end
  with "anything else?".
- *Incident, my fault:* naming `tests/real_voice_check.py` on a pytest command line imported it; its top-level code
  took `-p` (from `-p no:cacheprovider`) as the home and, with `config.HOME` already the real one at collection time,
  overwrote `~/.waggle/config.json` at 15:19 (vault `-p\vault`, Haiku, quiet hours off, job effort medium). Restored
  from `config.json.bak-settings-20261126-143119-2` (broken copy kept as `config.json.bak-broken-20261126-1519`);
  protected-key hash back to 5bbc6605d155. Guard: the three `tests/real_*.py` skip when imported, and
  `tests/conftest.py` sets a throwaway `WAGGLE_HOME` before any waggle import. Two stray empty folders
  `waggle\-p` and `waggle-dev\-p` are left for the owner to delete (Claude Code blocked the rm).
- Proposals YY3X (sign-offs) and 37MM (harmless-command asks) closed as dropped: done by hand.
- Settings now (the owner's own 14:31 choice): chat Sonnet medium, jobs Sonnet high.
- OPEN: proposals (`waggle/proposals.py`) still wait for the owner's yes before a merge, and the chat says so ("reply
  yes" at 15:13). That contradicts full trust; decide with the owner whether proposals merge themselves after a green
  suite. Speed (from session "Wago performance"): tool turns median 14.6 s vs 2.6 s without tools, about 5.6 s per
  tool step; 12 of 47 tool turns begin with a ToolSearch for Waggle's own tools; ttfw_s equals total_s on every row.

**2026-11-26, FULL TRUST (live bde0a7f). Do not bring the asks back.** The owner: "It has permission to do anything,
everything. Stop asking me." `policy.full_trust` turns every ASK into ALLOW (Claude hook and Hermes's
`security.check_tool_call`); money, their one limit, is a DENY ("never spends money"), never a question; only the
"Did you say...?" hearing question stays. Off switch: config.json `"full_trust": false` (protected key).
`tests/conftest.py` sets `guard.FULL_TRUST_DEFAULT = False` so the rules underneath keep their tests;
`tests/test_full_trust.py` covers the new rule with the real asks from approvals.db. Integrity lockdown (a real change
to a checked file outside deploy) still blocks until `guard approve`. deploy.ps1 now reinstalls on persona/ changes.

**2026-11-23, Step 1 (lean, unblocked chat) is live at 9dcac65.** The governing documents are now
`docs/ARCHITECTURE-DECISION-2026-11-21.md` and `docs/STATUS.md` (Step 1 log there, with evidence). In short: the chat
runs lean in `~/.waggle/chat_home` (switch `chat_lean`), rotates at 60k tokens with a local summary, deploys check
the code path only (`canary run --code-path`), asks are decided by effect (`tests/test_policy_effects.py`,
`tools/replay_asks.py`), stale asks expire via `duty.approvals`, and web pages go through `web_read` (WebFetch and
the agents' Gmail connector are gone). Open: the owner-turn latency and 50-turn false-ask gates need real use.

**2026-11-24, the thinking pass and Waggle Home.** The owner's rule from this night: an example they give is a test
case (`bench/think_cases.json`) and a line in their own words (`12 - Waggle/Me/Proactive.md`), never a new detector
or prompt line; new code is only for a new thing Waggle can see or do. `waggle/think.py` is the open pass over the
whole picture (three a day), `waggle/worldcal.py` its calendar sense, `tools/think_eval.py` the real-model check
(22 situations), `waggle/home.py` the page at http://127.0.0.1:8897/ (the old panel is `/ops`). Details and what is
still unproven: `docs/STATUS.md`, last section. Direction agreed the same night: Waggle is the body (ears, voice,
phone, clock, memory) and first-party Claude the brain and worker; no new work on Waggle's own autonomy engine or
self-improvement loop. The owner's final test for "built": they ask it to make money by itself (they set up
payments); that runs through the night-shift worker under the money goal's rules ($0, lawful, honest).

**2026-11-25, long real-use tests and the money test.** `tools/scenarios.py` runs 11 long scenarios
(`bench/scenarios.json`) through the live voice line with a Sonnet judge (`[waggle-test]` mark = canary origin;
`tools/say.py` sends one turn). The scenarios share one live chat, so a turn may rightly know earlier test turns. Real
projects driven through Waggle: the money objective 9b763f92 (research in `~/Claude/waggle-earn`, a DXF plate
generator in `~/plate-generator`, honest $0-45 estimate), a stoichiometry app (published by job 7543). Faults found and
fixed: the step check looked only in the vault (`objectives.resolve_named`), job status had no progress
(`waggle/progress.py`: agents stream their steps; background_status / objective_status show minutes, last steps,
files), stray writes to the retired `~/WaggleVault` (the chat prompt now names the real folders), read-only guard
asks, unseen news before the answer, no mail tool (`mail_recent`), objective progress lines for the evening note.
New the same day: the HUD Home with a Memory graph (`waggle/home_page.html`, `/api/graph`), the GitHub skill scout
(`waggle/skillscout.py`, daily 03:20, installs vetted instruction-only skills to `~/.hermes/skills/found/`), and
`tools/vault_para.py` (the vault's PARA layout, reversible with `--undo`; moves Waggle's folder to `01 - Areas/Waggle`).

**2026-11-25 evening, the Home chat (live d889498).** The owner talks to Waggle at the PC through the Home page and
asked for what the Claude app does. Now: the mic (button, a click or hold on the core, or Space) writes the words
into a "hearing" strip while they speak and then into the message box; nothing is sent until Enter ("Send as soon as
I stop talking" is a switch, off). Text typed or checked there counts as the owner's own words (`checked=True`).
History of every channel loads into the chat (`/api/history`), a Stop button ends a dictation, a spoken answer or the
running turn (`ChatSession.interrupt`; the next message then starts a FRESH session, never a resume: a resumed
stopped session answered one message late on the real Claude), the top bar with Command / Memory stays on both
views, `waggle/liveturn.py` feeds an Activity panel and the chat with each tool step as it happens, and the owner
chooses who answers and who does background jobs (`chat_model`, `job_model`: by chat through the settings tool or
with the two pickers; kept until changed; `background_task` takes `model`). Evidence and what is still unproven:
`docs/STATUS.md`, last section. Two things for the next session: (1) `python -m waggle.proposals suite` reports 41
failures that plain pytest does not (its `WAGGLE_ORIGIN=test` makes tests record test-origin turns; older than this
work): use `python -m pytest -q -p no:cacheprovider` alone as the evidence until it is fixed; (2) never run other
test runs or a preview server while a full run is going. New project the owner wants to build WITH Waggle: Land
Planner (vault note `Projects/Land Planner`, folder `~/Claude/land-planner`, data tool `tools/menifee.py`).

**2026-11-25 night, Fable 5.2 at high effort (live ab49c7d).** The owner set `chat_model` fable, `job_model` fable,
`chat_effort` high, `job_effort` high (new settings; an Effort picker sits beside Brain and Jobs on Home; job model
and effort also reach objective steps and `start_build`). Two faults only the live system showed, both fixed: the
live pool hands every session one Settings object from the core's start, so a later choice was never seen
(`follow_config=True` in `chat_engine.pool`), and `main_model` named the session's biggest user, not the model of
the turn. To prove which model answered, read the session file under
`~/.claude/projects/C--Users-hiheo--waggle-chat-home/` and the claude.exe command line, not the trace label alone.
The first job on Fable high (8706, the Land Planner app) did all its work and then died at the 1800 s job limit in
`chat_jobs.run_background` before it committed; it also stopped twice on guard false asks (a .bat edit read as "a
secrets file", curl to 127.0.0.1 read as "sending data to a website"). Both are OPEN: give long builds as several
smaller jobs until the limit and the false asks are dealt with. Never deploy while a job is running.

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

Photos, image documents, other files, audio files (transcribed up to 10 min), videos, location pins, captions,
stickers and `/waggle` now go through the engine too (`waggle/media_intake.py`, `waggle/engine_commands.py`; files
under `~/.waggle/media/<chat>/`, kept 14 days). A video gets its length, size, up to 3 still frames and (under 2 min,
`video_transcribe_max_s`) a local transcript, all from ffmpeg and the local recogniser, no Claude call; a location pin is
saved as `location.json` and handed to the turn as lat/lon/venue. `/waggle media clean` first shows count, size,
oldest and newest and files an approval (`approvals.db`, tool `waggle_media_clean`): "yes" or "approve CODE" moves the
files to `~/.waggle/purge/media-<stamp>/` (erased after 7 days); `/waggle media clean confirm` does it with no
question. The old path (Hermes turn through the plugin's router) still answers: Hermes's own slash commands
(/new /status /model ...), other chats, and any message the core refuses.

## Added 2026-11-20 night (main 459dfd1 + chat fix 1dc0d71)

| Part | State |
|---|---|
| Mail watch (`waggle/mailwatch.py`) | Live. IMAP + app password (Windows Credential Manager `waggle-mail-imap:personal`), no Claude. Core timer `knowledge.mailwatch` every 5 min 06:00-23:00. Triage: rules + local model `mailwatch_local_model` = gemma4. Seen-store `~/.waggle/mailwatch.db`. Real-inbox dry run caught the college email correctly; school account is blocked by its admin. `python -m waggle.mailwatch status|run --dry-run|login` |
| Drop folder (`waggle/dropbox_ingest.py`) | Live. Desktop shortcut "Feed Waggle" -> `WaggleVault/Drop` (after `tools/move_vault.py --apply`: `PersonalIntelligenceVault/12 - Waggle/Drop`); files become `Sources/<YYYY-MM>/*.md` with local summaries; weekly tidy plan Sunday 10:00. No Claude |
| Calendar watch (`waggle/calendar_watch.py`) | Owner said 2026-11-21: they don't use Google Calendar, so this stays idle on purpose (do not ask them to log in). Deployed; idle until the owner runs `python -m waggle.calendar_watch login` (steps in USER_TESTS.md). Google Calendar's secret iCal address (Credential Manager `waggle-calendar-ics:<name>`), stdlib only, no Claude. Core timers `calendar.fetch` every 15 min 06:00-22:45 and `calendar.headsup` every 5 min: a text 30 min before each timed event, once. Today's events feed the morning brief (`brief.calendar_today`). `python -m waggle.calendar_watch login|status|today|run --dry-run` |
| PC voice (`waggle/pcvoice.py`, task "Waggle PC Voice") | Live. Owner tested it with their real voice 2026-11-21 09:48: "good, a little slow" (turn 1 took 18 s from end of speech to answer, turn 2 4 s; intent judge logged fallback both times; speed work on rebuild/voice4). Hotkey Ctrl+Alt+` (Ctrl+Alt+Space is taken), wake word "Waggle" on, speaks Waggle's own messages when the owner is at the PC (`waggle/presence.py`) |
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
4. Phone calls (Twilio): SKIPPED by the owner 2026-11-21 ("skip phone for now"); don't ask again unless they bring it up.
5. Calendar: NOT USED by the owner (2026-11-21); skip. Was: the owner runs `python -m waggle.calendar_watch login` (USER_TESTS.md). Then `ingest.sync_upcoming` and
   the evening note should read `calendar_watch` instead of the Claude connector.
6. Done on `rebuild/media2`: videos and locations take the engine path; `/waggle media clean` asks first.

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

## 2026-11-21 morning: code gaps (live 852f89a, suite 3385 passed, canary passed)
Owner: PC voice tested ("good, a little slow"), calendar not used, phone calls skipped. Built by Sonnet workers, merged, deployed, each proven live:
- rebuild/voice4: the voice judge was on qwen3.6 (22 GB, evicted from the 12 GB GPU, 20 s reloads) and fell back after 3 s
  each turn. Now gemma4 (49/50, ~0.2 s, 1.5 s cap, 60 s skip after a failure, preloaded), recogniser kept warm, notes
  search 0.4 s on voice turns, the day's session pre-started after 04:00, and one `turn timing:` line per turn in
  pcvoice.log. Estimate turn 1 18 s -> 4-6 s; NOT yet measured on the owner's voice.
- rebuild/cron2core: the 9 Hermes cron jobs are gone (install removed them, backup jobs.json.bak-cron2core-20261121-102146);
  4 were shims of existing timers, 5 are core timers in `waggle/duties_core.py` (duty.hq ran live). `calls_enabled`
  defaults off (doctor: "phone calls are off"); `python -m waggle.call enable` turns them on.
- rebuild/media2: videos (length, 3 frames, local speech <=120 s) and locations go the photo path. Live: a 4 s clip was
  described correctly, frames saved. `/waggle media clean` previews and asks (approval code); live preview + "deny"
  left all 5 files. Locations can't be sent by the test client (no --location).
- rebuild/evals: `python -m waggle.evals run|status|list`, 41 local deterministic cases, timer evals.weekly Sun 20:45,
  evening note line only on a regression. Baseline 40/41: saf-6 (the local model pastes a key from its context) fails
  on purpose; the raw model still does it, but outgoing redaction now blocks it.
- rebuild/outredact: `waggle/outredact.py` runs `policy.redact` on every outgoing channel (Telegram send/edit/caption,
  outbox, ntfy, streaming, engine reply, speech, call). Live: asked to echo a fake sk-ant key, the reply was
  `[REDACTED:api-key]`, event `redacted_outbound`. Also `is_stop` no longer matches "stop by the shop".


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
7. Built since this was written: the test client (step 5), the learning loop (7), autonomy (12) and code
   proposals (13). Cron jobs to core timers (10) and evaluation suites (11) were built 2026-11-21 (rebuild/cron2core,
   rebuild/evals).
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

## 2026-11-21 afternoon (live d883965, suite 3650 passed, canary passed)
Owner's live voice test drove these, each proven live:
- Voice judge: Ollama reloads a model whenever a call's num_ctx differs from the loaded copy (8 s); the judge now uses
  the default, shared with mail triage. PC voice waits out a busy recogniser (429) and keeps it loaded from start-up.
  Heard-you tone; "On it, sir. Starting now." at a voice request's first tool call; 12/20 s follow-up windows with a
  soft cue (no "Waggle" needed); later sentences synthesized as one piece; SPOKEN/ANSWER parsed from the last marker.
- Fast path (rebuild/voicefast): time/date/reminders/maths/conversions in code (~2 ms), plain general knowledge on
  gemma4 (~0.5 s, gated); live Telegram voice note "what time is it" answered by code in 2.4 s total.
  Test voice notes can't carry the test mark (origin user): relabel such traces to 'canary' after testing.
- Vision: gemma4 is blind to images on Ollama 0.40.16. vision_model default "" = local_model (qwen3.6), used by the
  screen, drop folder and Hermes image tool; owner config override removed (backup config.json.bak-vision-20261121).
  Live: look_at_screen read the Monkeytype title and page text.
- Small tools (rebuild/smalltools): feedwatch (5 feeds), markitdown OCR venv ~/.waggle-markitdown, Context7 for
  proposal builds, Obsidian hub skills, pyright warnings in merge texts, `python -m waggle.usage_view`, session records.
- Owner said YES to: gogcli (rebuild/gogcli; inert until the owner does docs/GOOGLE.md and `Waggle Google Login.bat`),
  Cloudflare dead-man switch (rebuild/deadman; owner makes the free account, then docs/DEADMAN.md), ActivityWatch
  (v0.14.1 portable, task "ActivityWatch", live on :5600; Chrome extension is the owner's step, docs/ACTIVITY.md),
  focus blocks (`/waggle focus 45 calculus`, proven start/status/stop live).
- Owner's "no audio" was Chrome muted in the Windows volume mixer (not Waggle).
- 13:50: owner finished the Google setup (published app, signed in). Live: Waggle created "Waggle test doc" by chat in
  6.5 s, Tasks read (3), Classroom "nothing due" (personal account). ActivityWatch Chrome extension connected
  (bucket aw-watcher-web-chrome). Cloudflare dead-man switch SKIPPED by the owner for now (code stays inert).

## 2026-11-21 evening (live 3c13357, suite 3873 passed, canary passed)
- Vault MOVED to C:\Users\hiheo\PersonalIntelligenceVault: Waggle reads all of it, writes only in `12 - Waggle`
  (`vault_own`), vault_git inside `12 - Waggle` (35 commits kept), profile context from `vault_profile_notes`.
  tools/move_vault.py did it (561 files, 0 conflicts; config backup config.json.bak-vaultmove-20261121-163705;
  "Feed Waggle" shortcut repointed). WaggleVault left in place. Live: "what time do I prefer to wake up" answered
  from 06 - Lifestyle/Daily Rhythm.md. Index: 55 owner notes + 103 Waggle notes.
- Windows-MCP 0.8.8 (owner turns only; its overlay shows only during actions + 15 s), typed fast path, AW presence,
  recipes (needs ~2 days of activity), replay gate on proposals (tools/replay, Inspect AI dev venv; weak signal
  today: 18 replayable turns, local judge), tool finder (`find_tool` live: 3 candidates for "tables from PDFs",
  nothing installed; `install_tool` always asks).
- Next: rebuild/kws (sherpa-onnx wake word, default decided by its bench).
- 17:45 live 9555e59 (suite 3904): wake word via sherpa-onnx KWS (`pc_voice_wake_mode` kws|stt|both; model
  ~/.waggle/models/kws-zipformer-gigaspeech-3.3M, `python -m waggle.kws status`). Synthetic bench: 100% vs 95% hits,
  0 false wakes, 0 recogniser calls on room talk (64 before). Not yet tried on the owner's real voice; if it misses,
  set `"pc_voice_wake_mode": "stt"` (or "both") and redeploy.
