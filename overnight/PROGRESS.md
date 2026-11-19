# Progress

## 2026-11-18
- 20:30 Stage 1 audit done (see MASTER_PLAN.md). Worktree `waggle-dev` on branch `overnight` from main 9d7aa49.
  Baseline: 107 tests passing.
- User answers: full auto inside projects (D-03), Telegram only (D-04), Calendar being connected (B-01).
- 21:40 **Milestone 1 (f7e1980) — Claude agents with real tools.** policy.py + PreToolUse hook, bridge inside the
  gateway, Waggle MCP server, agent runner, skills registry (224 skills), traces, `act` route. 141 tests pass.
  **Deployed** to main (rollback: `git reset --keep 9d7aa49` in the live checkout + reload-waggle.ps1).
  Live checks: bridge serves 38 Hermes tools, computer_use list_windows 1.2 s through the bridge; Haiku agent used
  hermes_tool + skill_search in 6.9 s; `hermes chat -q "create a folder..."` routed to `act`, created the file,
  12 s end to end, trace recorded with its tool step; deleting a file correctly came back as an approval request.
- 21:05 Live router benchmark baseline: tuning **0.85** (40), held-out **0.80** (20). Misses: proofs/derivations/
  debugging answered by Haiku instead of "deep" (8), one regex false positive ("close the Chrome window with my
  email" -> Gmail; fixed with router.wants_connectors). Optimisation round started (learning.optimize_router).
- **Milestone 2 (08e6a44)** learning loop + prompts + bench, models/engines, agents, routines.
- **Milestone 3 (0cdb814)** command center (127.0.0.1:8897), memory_care, past-conversation recall, quota lockout fix
  (raising the learned cap left usage at 87% > 85% stop line -> Waggle would lock itself out once calibrated).
- **Milestone 4 (c91c685)** job crash recovery, waggle doctor, config backup + validation, CLI commands. 182 tests.
- 21:20 **Deployed 2-4** to main (rollback: `git reset --keep f7e1980` + reload). Live: doctor 12/12; dashboard 200;
  7 cron jobs; default routines installed (nightly-project-review 21:30, weekly-review Sun 18:00);
  `hermes chat -q "Every weekday at 6:45am, remind me..."` -> agent created the routine via MCP in 15 s
  (test routine removed afterwards).
- 21:35 **Learning loop, first live result:** optimize_router round 1 (Sonnet reflective rewrite of the router prompt,
  evaluated with real Haiku calls). Tuning 0.85 -> **0.975**, held-out 0.85 -> **1.0** => deployed as router prompt v1
  (`~/.waggle/prompts/router/v1.txt`). Change: "answer" narrowed to short/easy replies; "deep" broadened (proofs,
  derivations, step-by-step math, debugging, detailed trade-offs); "opus" for multi-part programs. Rollback:
  `python -c "from waggle import prompts; prompts.rollback('router')"`.
- Calendar: connected route + agents pick up Google Calendar/Tasks/Sheets automatically once connected (B-01);
  connector calls now also go through the policy hook. Tests no longer run a real `claude mcp list`. 183 tests.

## Phase 2 (autonomous development mission), 2026-11-18 evening

- **1ab65a1** objectives engine (persistent goals -> Sonnet plan -> background worker with specialist agents,
  per-step verification, 1 retry, block + notify, 24 h approvals for background sessions, report + vault note),
  cross-interface continuity (`traces.recent_elsewhere`), knowledge graph (`graph.py`: vault links, dated
  project decisions, people, local-model extraction of entities/relations/decisions from chats; decisions filed
  into the project's Decisions section; graph context per turn; `graph_query`/`graph_note`; `waggle-graph`
  cron every 6 h with silent stdout).
- **cc256b9** four reviewed skills shipped in `skills/` and installed by install.py: verified-computer-use,
  verified-browser-task, research-brief, objective-planning. Agents are told to load the verified-* skills.
- **c554063** proactive objective watch (blocked 12 h / waiting for yes 3 h / stalled 2 h, weekly dedupe) and
  objectives panels in the Command Center and the HQ note. **222 tests.**
- 21:44 **Deployed** to main (rollback point: `0e74e97`). Live checks:
  - `waggle doctor` 12/12 (8 cron jobs incl. waggle-graph; Twilio now configured).
  - Graph job on the real vault: 3.7 s, silent; 50 nodes / 48 edges / 13 decisions; BIOBUZZ query returns
    the project's real decisions.
  - Skill search against the real 228-skill index: each of the 4 new skills ranks #1 for its test request.
  - `hermes chat -q "objective: write a short note ..."`: replied in 7 s; worker planned 1 step, ran it,
    verified it, wrote `Inbox/Waggle deploy check.md`, reported: **done in 21.9 s, no human input**.
  - Earlier: direct objective test (2 steps, 41.9 s) and the nightly-project-review routine ran at 21:30
    (status "quiet": nothing worth reporting).

## Mission 3 (reliability, trust, independent operation), 2026-11-19 06:20-07:25

Audit first (before changing anything): branch `overnight` = main = `b69987c`, clean. 3 of the 222 tests failed when
run between 23:00 and 07:00 (they expected a phone message during quiet hours), fixed in the test setup. Found in the
code: a bare "yes" approved the *first* pending objective approval of any objective; a verifier error passed the step
unchecked (`agents.verify` returned True on exceptions); two workers could run the same step (check-then-set PID, no
lease); approvals were a JSON file rewritten by several processes; no background budget at all (objectives only had
the shared 30 % Waggle share of the 5-hour window).

What shipped (all on `overnight`, deployed to main, pushed):
- **6faf467** `waggle/budget.py`: daily background allowance (`background_daily_units`, default 4 weighted units),
  separate interactive/background accounting in `~/.waggle/budget.db` per objective/step/purpose/model, measured
  tokens vs estimated limit cost (`measured` flag), atomic reservations (`BEGIN IMMEDIATE`) so parallel workers can't
  take the last slot, max 2 background calls at once, background stops at 60 % of the 5-hour window (chat keeps going
  to 85 %), background waits 60 s after the user's last message, downgrade Sonnet -> Haiku when < 25 % is left
  (never for coder/robotics: those wait), every deferral/downgrade logged with its reason, backstop inside
  `claude_cli.ask` and `agent.run` so no background call can skip the check. Objective engine: worker lease
  (compare-and-swap), one-claim steps, `verifying` checkpoint (work saved before it is judged; a crash never re-runs
  it), interrupted steps re-run with an "interrupted, check what exists, never repeat irreversible actions" note and
  block after 3 interruptions, a check that can't run waits instead of passing, exactly-once finish, done/cancelled
  objectives can't be restarted, budget parking (`deferred_until`) + supervisor resume. Approvals moved to
  `approvals.db` with 4-letter codes, bound to their session, one use, `approve <code>` / `deny <code>`; a bare "yes"
  never approves background work. Atomic vault writes (temp file + replace).
- **35b0118** Command Center panels (background budget, waiting approvals with codes/expiry, active/parked/blocked
  objectives, recent failures, live workers, last recovery, local vs Claude turns, model usage by kind); doctor check
  "Background work"; routines, proactive, brief and learning cron jobs run as background (routines reserve budget).
- **5761363 / 0ef7d38** knowledge graph quality (see FEATURE_MATRIX mission 3).
- **386f2b6, a605445, 3ac8099, 51065e7, 4323846, 884c6b3**: fixes found by the live end-to-end runs (below).

Live end-to-end runs (real gateway, real Claude, real vault; `hermes chat -q` = the terminal interface):
1. `objective: Read my BIOBUZZ Robot project note and write ... Inbox/M3 test - BIOBUZZ open problems.md` ->
   **found bug**: the objective the user had just started was parked 5 min by the "user is chatting" yield. Fixed
   (386f2b6). The supervisor cron resumed it at the next tick.
2. Killed the worker (`Stop-Process`) 3 s into step 2 -> next supervisor tick re-queued step 2 as interrupted; the
   re-run saw the orphaned run had already written the note and **left it unchanged**; done, one notification, file
   content correct.
3. Budget set to 0.01 -> new objective parked ("background budget used: 0.07 of 0.01 units today", retry 04:00),
   nothing ran, Command Center showed it. Raised to 4 -> resumed at the next tick (a605445 added this).
   **Found bug**: the note landed in `C:\Users\hiheo\Claude\Inbox\` (agent's working folder) and the report-only
   check passed it. Fixed (4323846): steps are told where the vault is, and a file a step promises must exist on
   disk. Misplaced test file removed.
4. Approval: create + delete a test note -> step 2 asked `[3REH] deleting files`. Bare "yes" -> listed the request
   with its code, approved nothing. `approve 3REH` -> **found bug**: the re-run sent the same `rm` with a different
   free-text description and was asked again (`AP3T`). Fixed (51065e7: shell approvals match the command). `deny
   AP3T` -> objective blocked "you said no to ...". `retry objective c6b88aea` (new chat session) -> asked `373M`
   -> `approve 373M` -> re-run used it exactly once (`used`), file actually deleted, done. Also found: approval pauses
   counted as failed attempts (884c6b3).
5. Failure reporting: `objective: Copy the exact text of C:\Users\hiheo\m3-missing-source.txt into a note` ->
   agent refused to invent text, disk check confirmed no note, 1 retry, **blocked** (not done), message delivered on
   the next chat turn. The gateway was restarted in the middle of this run: the detached worker carried on.
6. Regression: "what is 17 times 23?" -> 391; BIOBUZZ focus/decisions question answered from the vault; doctor
   13/13; no Waggle errors in `errors.log` / `gateway.log` since the deploy.
7. Graph job on the live graph: old noise cleaned; the fake "decision" an old run had filed into `Projects/Waggle.md`
   is retracted and struck through there (0ef7d38: before that fix the rebuild restored it).

Tests: **272 passed** (222 before; +50 in `test_trust.py`/`test_graph.py`/`test_dashboard.py`). Rollback point:
tag `known-good-pre-m3` = `b69987c`.

## First real Telegram session (user at school), 2026-11-19 08:10-08:50

What went wrong for the user, from the traces: voice note transcribed as "BioBus Robot ... PDF I can use for for for
extra bodies" (whisper `small`); the robotics agent built the portfolio PDF but could not deliver it (no file tool;
mailing it to the user's own Gmail needed approval; "Send it to the email" in between lost the in-chat "yes"; the next
"yes" re-ran and was blocked again, showing an internal hex id); Haiku claimed Waggle can't send email.
School Wi-Fi blocks Telegram on the phone ("Connecting..."): mobile data works.

Fixed and deployed (d81a055, e4bf9aa, e44d4e3, 453fe07; 277 tests):
- `send_to_me` MCP tool: text and/or files (PDF, images, up to ~50 MB) to the user's Telegram via `hermes send
  MEDIA:<path>`; allowed without approval. Live: "send me <note> here on Telegram as a file" -> delivered in 16 s.
- Mail whose recipients are all in `self_emails` (local config.json only) needs no approval.
- Hook approval text shows the 4-letter code, never the internal id.
- Chat models separate from background: `chat_model`/`chat_agent_model` = sonnet, `chat_deep_model` = opus (local
  config); background verify/report/objective steps keep haiku/sonnet. `local_first` off (Claude answers everything
  non-trivial). Chat turn from the terminal ~13-16 s.
- Router: act covers "send it here / to me"; file paths containing "Inbox" and send-to-me requests no longer go to the
  read-only Gmail answerer (it said "I can't send files" and triggered a useless Opus self-build, which was killed).
- Hermes STT (config.yaml, not in repo): `stt.local.model: turbo`, `language: en`, `initial_prompt` with the user's
  vocabulary. On the user's real voice note: "BioBus ... extra bodies" -> "BIOBUZZ robot repositories ... PDF I can use
  for portfolio", 0.3 s on the GPU.
- Voice: three Kokoro samples (A current bm_daniel 1.25x, B af_heart 1.05x, C am_michael 1.05x) sent to Telegram;
  waiting for the user's pick.

## Telegram & voice intelligence upgrade, 2026-11-19 09:00-09:25

Benchmark first (bench/chat_bench.py: the same 9-turn conversation through the live gateway vs direct Sonnet/Opus;
results in ~/.waggle/bench, not the repo). Causes of the weak answers, from the traces:
- Wrong math: the "answer" reply is written inside the routing JSON at low effort, headline first (1.01 m for a
  3.14 m drive question). The learned router prompt v1 also told it to keep answers "short and easy".
- Lost follow-ups: each remembered turn was cut to 1200 chars and the history lived only in gateway memory.
- "quick: derivative of x^2 sin x" got a quiz: the tutor guide always checked understanding first.
- Deep questions with deep_model == router model got no answer and fell through to a tool job.
- Voice notes reached the model with the Hermes wrapper and raw mishearings ("bio bus", "ethics").

Fixed and deployed (43d8b17, 9e87582, fb1e389, d24fb06; 289 tests):
- waggle/voice_turn.py: voice notes become what was said (wrapper stripped, FTC terms fixed, personal
  `voice_fixes` from config.json), marked as spoken so the reply is short spoken sentences; a failed
  transcription gets "I couldn't make out that voice message..." instead of a guess.
- Conversation memory: 8 exchanges, replies up to 2500 chars, rebuilt from traces after a gateway restart.
- Router: answers at full quality, numbers worked out in "reason" first, "quick" means answer. Router prompt v2
  (~/.waggle/prompts/router) scored 0.975 tuning / 1.0 holdout, same as v1; a first draft scored 0.95/0.90 (too
  many "debug"/"rigorous" questions answered inline) and was not activated.
- Deep: free-text Sonnet at medium effort; deep+hard -> chat_deep_model (Opus) with 150 s limit and Sonnet fallback.
- Approval lines name the exact command: "deleting files `rm C:/...` [JDVH]".
- Objective verifier judges against the task's stated requirements (a correct note was failed twice for
  citation details nobody asked for); a report missing a required step still fails.

Live checks (through the gateway with `hermes chat -q`; the Telegram app itself needs the user):
- STT on the user's real voice note: small "BioBus Robot ... for for for extra bodies"; turbo "BioBus robot ... for
  portfolio for ethics robotics"; turbo + vocabulary prompt "BIOBUZZ robot repositories ... PDF I can use for
  portfolio, for ethics robotics" (1.1 s on the GPU); voice_turn then makes it "for FTC robotics".
- Benchmark after: drive math 3.14 m (correct), derivative answered first, follow-ups keep "that", voice note read as
  BIOBUZZ + FTC with spoken-style reply. Median wall time 11.7 s (10.7 s before; direct Sonnet ~5 s without context).
- PDF to Telegram: "send me ...Portfolio.pdf here on Telegram" -> act -> send_to_me ok, no approval, 26.7 s.
- Approval: "delete the file ..." -> asks with code JDVH; "approve JDVH" ran exactly that command, file gone,
  approval state used.
- Objective from chat: "objective: write a short note ..." planned 2 steps; stuck on the old verifier; after the fix
  "retry objective e826f2da" from chat -> done, report sent.
- Gateway restart: question, reload-waggle (new PID), follow-up "how long would that take?" answered about the
  transfer it had recommended.
- Hard proof: routed deep (not hard) -> Sonnet medium, correct, 12.7 s. Opus path covered by tests only.
- Budget after the session: background 0.343 / 4 units, interactive 1.54; doctor 13/13.

## Phone check + spoken replies, 2026-11-19 10:23-10:45

- The user's real Telegram voice note (10:23) arrived, was transcribed ("Thanks for happening [helping] with all the
  repos") and answered by Sonnet in 4.4 s; 11.3 s end to end because it was the first note since the 09:19 reload
  (Whisper turbo loads on first use, then stays loaded: stt.local.unload_after_idle_seconds defaults to 0 = never).
  The user's text message never reached the gateway (Telegram API connection dropped twice around 10:05).
- That reply told the user to "approve VWV4", an email approval that had expired at 09:02. Fix (14f630f): when the
  recent conversation mentions approvals, the context now says which codes are really open (or that none are).
  Self-email itself is allowed by policy now (policy.decide on Gmail send_message to the user's own address -> allow).
- Reply voice: Kokoro af_heart at 1.05x (sample B; user said "choose any"). Hermes config: voice.auto_tts true,
  auto_tts_mode voice_only, so voice notes get a voice reply before the text and typed messages stay text.
  Checked with Hermes's own auto-TTS path after `pm.install.activate()` (what the gateway does at startup): the
  bundled ffmpeg (.hermes/tools/ffmpeg-9.0.1) turns Kokoro's WAV into Ogg/Opus, voice_compatible true, 2.1 s.
  kokoro_say.py also writes Ogg/Opus itself for .ogg outputs (0ff5ef8), a fallback if ffmpeg is ever missing.
  Desktop playback stays WAV (Hermes plays WAV through sounddevice on Windows; other formats need ffplay).
- Not yet heard on the phone: the first spoken reply needs the user to send a voice note.
