# Waggle status

The one place that says what works. Four separate columns, never merged:

- **Implemented**: the code exists.
- **Functional**: it did the job at least once.
- **Tested**: automated tests or a scripted live check cover it.
- **Reliable**: it has worked in the owner's real use over days, with evidence.

A row moves right only with evidence written in the last column. Plan and order of work:
[`ARCHITECTURE-DECISION-2026-11-21.md`](ARCHITECTURE-DECISION-2026-11-21.md).

Last updated: 2026-11-21 (Step 0). Feature freeze in force: no new features until Step 3 ("first night") passes.

| Subsystem | Implemented | Functional | Tested | Reliable | Evidence (2026-11-21 unless dated) |
|---|---|---|---|---|---|
| Core runtime: jobs, timers, outbox, inbox, leases, watchdog | yes | yes | yes | mostly | 3,002 jobs in 49 h, 1 dead; killed core back in 51-57 s. Reboot path not yet tested |
| Backups | yes | yes | yes | **restore proven once** | Backup 21:03 (82 files, 15 databases); restored into a throwaway folder; every database passed `integrity_check`; config loaded from it |
| Reminders | yes | yes | yes | **yes** | 7 of 7 fired within seconds |
| Telegram text | yes | yes | yes | mostly | 61 owner turns answered and delivered. Inbound depends on the Hermes gateway, which the watchdog does not watch |
| Telegram voice notes | yes | yes | yes | mostly | Owner: "speed is perfect" (11-20) |
| PC voice | yes | yes | partly | **no** | Owner: robot-like, end of speech slow; hotkey fixes not yet confirmed by the owner |
| Chat brain (Sonnet session, tools, Google) | yes | yes | yes | partly | Before Step 1: p50 6.6 s, p90 22 s, max 37 s; 6 of 61 owner turns degraded; about 120,000 tokens of fixed context. **Step 1, live 9dcac65 (11-23):** first call of a fresh session 18,890 tokens (fixed part 16.4k); test-client "Hello" 2.2-2.8 s; Google Doc, vault note, GitHub lookup all worked. Owner-turn latency not yet measured (no owner turns since the deploy) |
| Mail watch (IMAP + local model) | yes | yes | yes | partly | 182 passes, 7 alerts, caught a real deadline; 4 near-duplicate alerts; no feedback recorded |
| Morning brief / evening note | yes | yes | yes | unknown | 1 brief, 2 evening notes sent |
| Background task from chat | yes | yes | partly | unknown | 2 real runs, results self-reported only |
| Objectives engine | yes | trivial tasks | unit | **no** | 11 objectives, 7 were developer tests; never longer than about 7 minutes; crash recovery never seen live |
| Autonomy (goals, hourly loop) | yes | **no** | unit | no | 23 firings, 4 steps, all "write a note" |
| Proactive reasoning | yes | **no** | unit | no | 415 passes, 1 decision ever ("do nothing") |
| Learning about the owner | yes | barely | unit | no | 6 facts from 61 owner turns |
| Self-improvement | yes | once | unit | no | 0 kept changes; 2 proposals (1 failed, 1 rolled back) |
| Evals | yes | yes | n/a | no | 1 run, 41 made-up cases |
| Memory retrieval / graph | yes | yes | quiz 73.5 % | partly | Effect on answers not measured |
| Permission hook (guard + policy) | yes | yes | yes | over-blocked; fixed, **not yet proven in use** | Before: 3 of 61 owner turns lost their work to a false ask; 11 of 17 approvals never answered. Step 1 (11-23): asks by effect; replay of all 76 real owner tool calls: 2 asks before, 0 after; 0 approvals filed in the post-deploy checks; stale asks now expire with one line. 50 real owner turns still to count |
| Background budget | yes | yes | yes | starves background | 22 of 22 decisions were "defer" |
| ActivityWatch, miner, recipes | yes | sensing only | unit | no | Mined patterns are `git add > git commit`; 0 recipes |
| Vault | yes | yes | yes | yes | All reads and writes in `PersonalIntelligenceVault`, Waggle's writes only in `12 - Waggle`. Since Step 0 there is no default vault: a config without `"vault"` makes Waggle refuse instead of using another folder |
| Calls, dead-man switch, hands-free line, tool installs, Smart Turn, calendar watch | yes | never used for real | synthetic | no | Waiting on owner steps, or skipped by the owner |

## Baseline (start of Step 0)

Numbers the later steps are measured against. Source: `~/.waggle` databases, the session transcript, the account
usage card.

| Measure | Value |
|---|---|
| Owner turns per day (11-21) | 43 |
| Chat latency p50 / p90 / max | 6.6 s / 22.3 s / 36.7 s |
| Fixed context on a fresh chat call | 119,491 tokens |
| Owner turns degraded or failed | 6 of 61 (10 %) |
| False permission asks that lost the work | 3 of 61 owner turns |
| Unprompted messages that were reasoned decisions | 1 ever |
| Facts learned | 6 |
| Verified autonomous steps | 4 (all "write a note") |
| Kept self-improvements | 0 |
| Background budget decisions that ran | 0 of 22 |
| Canary Sonnet turns vs owner Sonnet turns | 50 vs 46 |
| Jobs per day at idle | about 1,500 |
| Own Python modules / size | 160 / about 3 MB |

## Owner decisions (2026-11-21)

- Vault: `PersonalIntelligenceVault`; Waggle writes only in `12 - Waggle`.
- First objectives for the night shift: **FTC SimBench Pro** and **Waggle itself**.
- Night shift: **23:00-07:00**, at most **35 %** of the weekly Claude allowance.
- Reboot: accept the gap until sign-in; Waggle sends a phone message when it is back.
- Overnight work may be pushed as `waggle/*` branches with **draft** pull requests; Waggle never merges.

## Step 0 log

- 2026-11-21: decision document approved and saved here.
- Backup taken and a restore drill passed (see the table).
- Rollback tag `pre-cleanup-2026-11-21` pushed (main at c390eec).
- Cleanup tier 1: 34 merged worktrees removed and 19 merged local-only branches deleted; git refused none.
- Cleanup tier 2: disabling the stray `Hermes_Gateway` task needs administrator rights; left to the owner.
- Vault: the implicit `~/WaggleVault` default is gone (config, two tools); regression tests added.
- Full suite 3,910 passed; deployed eae8fe9 with `tools/deploy.ps1`; live canary answered in 11.0 s; guard clean.
- 48 h soak (to 2026-11-23): 1,961 jobs, 0 failed; 128 of 128 mail checks ran; reminders delivered; 23 of 26
  owner turns ok.
- Reboot, real (PC off 2026-11-22 15:39 to 2026-11-23 11:46): after sign-in every process was back within about
  75 s with one gateway; the stray `Hermes_Gateway` task did not start a second one. Outage of about 20 h: the
  evening note, morning brief and backup were skipped by design. **Reliable row for "one machine" stays open.**
- Back-online message added (owner's reboot decision).
- Timer experiment: in a throwaway home a disabled timer stays off through a core restart and `timers.enable`
  turns it back on. Then on live: `calendar.fetch`, `calendar.headsup`, `deadman.beat` disabled (undo:
  `timers.enable(id)`).
- HQ note and canvas rewritten only when they change (the canvas write was being logged as an owner edit).
- Feedback capture: welcome / ignored / unwelcome labels for every unprompted message origin, not only reasoning
  decisions. Replay capture: every owner turn is kept in `traces.db` (never pruned), which the replay gate reads.
- First objectives written with definitions of done: [`OBJECTIVES.md`](OBJECTIVES.md).

## Step 1 log (lean, unblocked chat on the current `claude -p` path)

Gate: fixed context under 20k tokens; p50 owner turn under 5 s and "Hello" under 3 s over real turns; 0 false asks in
50 real owner turns; Google Doc, vault edit and GitHub lookup still work through Telegram.

Built on `overnight` (95e79b4, 2e9756a, e8a3392, cb8c5b7, 63611b2), each with tests:

1. **Lean launch** (`chat_session.py`, switch `chat_lean`): own empty folder (`~/.waggle/chat_home`),
   `--strict-mcp-config`, `--setting-sources ""`, no auto memory / CLAUDE.md / git / bundled skills, MCP tools deferred
   behind Claude Code's ToolSearch except 13 core tools (`waggle_mcp.ALWAYS_LOAD`). Measured with the real CLI, one
   "Reply with just: OK" turn, context tokens from the result event:

   | Launch | Tools | Context tokens |
   |---|---|---|
   | Old (in `~/Claude`, 16 MCP servers incl. claude.ai connectors) | 207 | 115,686 |
   | + empty folder, strict MCP, no setting sources | 104 | 33,186 |
   | + MCP tools deferred, no memory/CLAUDE.md | 105 (85 deferred) | 14,532 |
   | Final (12 core tools always loaded), Sonnet | | **16,444** |

   Real `ChatSession` in a throwaway home (Sonnet): "Hello?" answered in 3.0 s cold (first words 2.63 s); a deferred
   tool found and used through ToolSearch ("FTC team 7831" -> ftc_team, 5.8 s); vault_search/recall called directly.
2. **Rotation by size**: a session whose last call passes `chat_session_max_tokens` (60,000) is replaced right after
   that turn. The local model summarises the owner's last 30 turns (schema-shaped, so a "ping" in the transcript
   can't turn it into a reply; canary turns left out) and a fresh process is pre-started carrying the summary plus
   the last 3 exchanges. Real data: 11 useful notes from 30 owner turns in 6.2 s, $0. Turn count only applies when
   the size limit is 0.
3. **Canary**: a deploy now sends "canary <id> (code path): ..." which the engine answers in code for origin
   `canary` only: Telegram in, gateway, core inbox, engine, outbox and Telegram out are proven with no Claude call
   (ledger `canary.codepath`). The 12:30 run keeps one full Sonnet round trip a day. Before: 32 Sonnet pongs.
4. **Permission asks by effect** (`policy.py`, `guard.py`): read-only web queries (curl -G, GraphQL queries,
   searches, no credentials) never ask; deleting inside scratch (TEMP, worktrees, job folders) and creating a new file
   outside the vault do not ask; `StructuredOutput` is a no-effect tool; the guard judges `$d\src\__init__.py` by
   the resolved path when every variable is one literal. Still asking: money, credentials, messages/posts, permanent
   deletes of the owner's data, system settings, **named third-party installs (new: `pip install x` asks;
   `pip install -r requirements.txt` does not)**, the guard's files. Answers: spoken "Yes, go ahead and do it" /
   "No, don't" count; consent inside a longer reply right after an ask counts (the owner's real voice reply in trace
   206 did not before); an unsure transcript grants nothing; `duty.approvals` expires stale asks every 5 min with one
   line to the owner.
   **Replay evidence** (`tools/replay_asks.py`, every tool call of the owner's real turns with inputs from the
   Claude session transcripts): 76 calls in 49 owner turns -> live policy 2 asks (both false: FTCScout GraphQL),
   new policy **0**. The recorded false asks from `approvals.db` (TMAF, WNVR, J9YC, RM6R, F3EK, P4EV, K44T, 7A3R,
   7HJX) are a regression test (`tests/test_policy_effects.py`).
5. **Untrusted content**: `web_read(url, question)` replaces WebFetch for the chat and for agents: the page is
   fetched (public http(s) only), trimmed to the paragraphs that match the question, and the local model (gemma4,
   no tools; a tool-less Haiku call without it) returns capped typed fields (title, answer, facts, found,
   tried_to_instruct). Live: 0.6-1.1 s warm (Wikipedia, GitHub, Python docs). Agents no longer load the Gmail
   connector; background builds run with WebFetch and Gmail off; Hermes `web_extract` points to `web_read`. Mail
   was already reduced to typed fields by the mail watch (local model) and the Gmail passes of `mailwatch`/`ingest`
   (connector read tools only + schema).
   Known gaps: Hermes browser tools (wrapped `<untrusted>`, not reduced) and Drive/Docs connector content for agents.
   Closed 2026-11-23 (live 10af2b9, suite 4,039 passed): a shell command that would print a web page into the session
   (`curl`, `wget -O-`, `Invoke-WebRequest`, `irm`; not an API, a download to a file, a headers check, a local service
   or output piped into `jq`) is answered `RETRY_WEB_READ` (never an approval). Replay of the owner's 76 real calls:
   0 asks, 0 redirects. Live: "Read <Wikipedia page> and tell me who founded FTC" -> `web_read` directly, 7.77 s,
   correct, no approval filed (trace 235).

**Deploy (2026-11-23, live 9dcac65).** Full suite 4,018 passed (`tools/locked_suite.py`). `tools/deploy.ps1`:
guard approved (agent, claude_cli, config, guard, policy changed), core healthy at 9dcac65, deploy canary passed (8.8 s;
this one run still used the old full round trip because PowerShell had read the old script). Then, by hand:
- Code-path canary: answered in code in 0.63 s, 2.2 s through Telegram, no Claude call (trace 223).
- The process started after the deploy resumed the old session (`e00b7346`): 127,598 tokens, because that session's
  history carried the old launch's injected memory and listings. It was over 60k, so the size rotation replaced it at
  once: a fresh lean session (`055ab1bb`, in `~/.claude/projects/C--Users-hiheo--waggle-chat-home/`) carrying the
  local summary. Lean resumes stay lean (throwaway home: 17,504 / 17,540 / 17,576 tokens over 3 restarts).
- **Fixed context gate: met.** First call of the fresh session 18,890 tokens, including that turn's context block and
  the carried summary; Claude Code's own fixed part 16.4k.
- Test client (origin canary, never the owner's turns), totals from `traces.db`: "Hello?" 2.78 s, "Hello" 2.22 s,
  Tokyo time 3.51 s, Google Doc created 6.36 s, vault note in `12 - Waggle/Waggle/Step 1 check.md` 10.41 s, GitHub
  "three most recent repos" 5.41 s (correct). p50 4.5 s. No approval was filed.
- **Still open (need the owner's real use):** p50 owner turn under 5 s and "Hello" under 3 s over real turns; 0 false
  asks over 50 real owner turns. Measure with `py -3.14 tools/replay_asks.py --since 1795468800` (asks) and the traces
  table (origin `user`, since the same time).
- Seen while measuring: every turn re-sent the context block (~2k tokens), so the session grew ~3k tokens a turn
  and rotated after about 12-14 turns. Fixed the same day (below).

**Context sent once per session (2026-11-23, live 2bd8bab).** `build_context` names its parts; the chat session
replaces a part whose exact text it already sent in the same Claude session (core, lessons, memory, projects, graph,
recall, voice hint) with a one-line pointer (`[core: unchanged, given earlier in this conversation]`). Kept on disk
per chat and session id, so a resumed process still knows; a fresh, rotated or new-day session, a failed turn or a
retry sends everything again; approvals, news, deliveries and the time always go. Trace marker
`extra.context.repeated` lists what was left out. Full suite 4,022 passed; deploy canary used the new code path
(2.2 s, no Claude call).
- Real CLI, throwaway home, 6,500-character core block, 4 turns: growth 2,812 -> 80 tokens a turn.
- Live (test client, session `055ab1bb`): turn 231 after the restart sent everything (4.32 s, process restart and
  resume); turns 232-233 left out core and projects, the session grew 147 and 144 tokens, totals 1.67 s and 1.65 s.
  At this rate a session reaches the 60k rotation after hundreds of turns instead of about a dozen.

## Step 2 log (trials: DBOS and the Agent SDK, 2026-11-23)

Both trials ran in throwaway places, never against the live home. Scripts and results: `tools/trials/`.

**DBOS Transact 3.6.0 (SQLite), decision doc 1.2: PASS with conditions.** `tools/trials/dbos/driver.py` drives one
4-task objective (T1 -> T2 -> approval wait -> T3 -> T4 as a child workflow), each task a 15 s step that records itself
in a separate ledger.
- Killed in the middle of T1, then of T2: each re-ran once on recovery; the ledger shows every task done exactly once.
- A code change between runs (pinned `application_version`) did not orphan the objective; killed during the approval
  wait, it resumed waiting, and `DBOSClient.send` from another process released it.
- A second worker started on the same database with the same executor id re-ran the in-flight T3 (absorbed by the
  idempotent ledger).
- Cancelling T4 marked it CANCELLED, but the step already running finished: cancellation acts between steps only.
- Idle cost 0.365 % CPU; only a loopback socketpair, no listening port, no outside traffic. A `pythonw` run completed.
- Conditions for adoption: one worker (or a unique executor id per worker), every step safe to repeat, and no promise
  that "cancel" stops a step in flight.

**claude-agent-sdk 0.2.188, decision doc 1.1: PASS only with two fixes; out of the box it FAILS the window gate.**
Same 30 prompts (`tools/trials/sdk/prompts.py`: 12 with Bash/Glob/Read, a code word set in turn 1 and asked back in
turn 16), same model (Haiku), tools, MCP config, Waggle policy hook (`agent_hook.py` via `--settings`), lean
environment and throwaway home for both paths. The SDK ran from `pythonw` under a one-time Task Scheduler task with
`tools/window_probe.py` watching; the claude process was killed after turn 15 and the session resumed.

| Check | Today's path (`ChatSession`, `claude -p`) | SDK as shipped | SDK + `CREATE_NO_WINDOW` |
|---|---|---|---|
| Turns OK | 30/30 | 30/30 | 30/30 |
| p50 / p90 per turn | 1.42 s / 2.17 s | 1.33 s / 2.11 s | 1.65 s / 2.13 s |
| First-call context | 15,354 | 15,354 | 15,354 |
| Resume after kill (code word back, same session id) | yes | yes | yes |
| Policy hook called for every tool call | 12/12 | 12/12 | (same launch) |
| Windows / focus changes | (Waggle's own launch is hidden) | **2 / 2** | **0 / 0** |
| Rate-limit event | (Waggle reads it today) | yes | yes |

- **Windows:** the SDK starts `claude.exe` with `anyio.open_process` and no creation flags, so from `pythonw` Windows
  opened a Windows Terminal window that took focus, once at start and once at the resume. Passing
  `CREATE_NO_WINDOW` (one wrapper around `open_process`, `sdk_trial.py --no-window`) removed both. SDK 0.2.188 has
  no option for it, so the wrapper would have to be kept and re-checked on every SDK upgrade.
- **Hooks** (in-process `HookMatcher`, timeout 10 s, Bash with `bypassPermissions`, a marker file shows whether the
  command ran): a hook that returns normally lets it run (control); a hook that **raises** lets it run with the
  default `on_failure` (**fails open**) and blocks it with `on_failure="block"`; a hook that hangs 60 s blocks it either
  way (13.5 s). So every policy hook would need `on_failure="block"`.
- **Latency:** equal within run-to-run noise (two SDK runs: 1.33 s and 1.65 s; CLI 1.42 s).
- **Usage windows:** the `RateLimitEvent` names the window that crossed a threshold (`seven_day` here), and its raw
  payload carries both windows (`unifiedWindows.five_hour` and `.seven_day`, utilisation and reset time). 2-5 events in
  30 turns.
- **Login:** the existing Claude login, no API key, no new sign-in (ANTHROPIC_* and CLAUDE_* removed from the env).
- Not tested: a long-lived SDK client across many hours; the SDK's own session store.

Verdict for Step 2's choice: the SDK gives no speed or context gain over today's lean `claude -p` path. It would add
a window fix to carry and a hook default that fails open. Adopting it is only worth it for something the CLI path
cannot do.

## Steps 3-7 log (built 2026-11-23, on the owner's "build the whole thing")

Code is in; every gate below needs real nights or real use, so each says what is measured and what is still open.

**Step 3, first night** (`waggle/nightshift.py`, timers `night.tick` every 10 min and `night.report` 07:30,
detached `night.task`). Works the owner's approved objectives from `~/.waggle/night/objectives.json` (seeded from
[`OBJECTIVES.md`](OBJECTIVES.md): SimBench PRs #12, #13, #15, then Waggle's idle churn), one task at a time,
23:00-07:00, never started after 05:00.
- Sandbox: a clone under `~/.waggle/night/repos` whose push URL is disabled, one worktree per task on its
  `waggle/*` branch. The worker (Sonnet through `agent.run`, the policy hook, files and shell) cannot push.
- Checks by code: new commits, nothing uncommitted, no conflict markers, `AGENTS.md`/`.github/` unchanged (or
  equal to main's), contains current main (O1), the objective's test command exits 0, a review note with a
  `Verdict:` line. Then a tool-less Haiku second opinion that can only stop a delivery.
- Delivery by code only: push `waggle/*` (no force; anything else refused) and open a **draft** PR whose body is
  the review note. A permission ask parks the task; a failed check retries once with the gap written into the brief.
- Morning: one message, numbered items with CI state, diff size, test line, verdict, link; the owner answers
  `accept 1`, `reject 2 <why>`, `redo 3`, `accept all` (typed or spoken; answered in code before Claude).
- Budget: stops for the week at `night_week_cap` (0.35) of the 7-day allowance, measured from the 7-day readings
  around each task, and never starts while the week is at or past `seven_day_stop_at` (0.85). On 2026-11-23 the
  week stood at 85 %, so **the first night waits for the weekly reset**; the 21:00 evening note says so.
- Live checks without Claude: `gh pr view` finds PR #12's branch; `gh pr checks` reads CI (green).
- Gate (open): at least 3 of 5 nights produce an accepted result.

**Step 4, one work list** (`waggle/worklist.py`, MCP tool `work_status`, policy: read). One answer across the night
shift, objectives, background tasks, autonomy goals, routines and running core jobs; a source that fails to read
is named. On live data it listed 5 enabled autonomy goals (including "Money experiment") and 2 routines. A night
task whose worker died with a restart or deploy is requeued on the next tick (within 10 min) with that said in
its brief. Test: a 3-night objective with the first night's worker killed ends with every task delivered once.
DBOS (trial passed with conditions) is **not** adopted yet: the core's job runtime plus `night.db` already give
the same recovery for one worker, and a second process with its own packages would add to the size budget.
Revisit if a multi-step objective needs mid-task checkpoints. Gate (open): a real 3-night objective survives a
restart and a deploy.

**Step 5, memory and promises.** Facts already land through the daily digest (16 filed since 11-21). New: a dated
promise in Waggle's own reply ("I'll check the shooter numbers tomorrow") with no reminder, job or task created in
that turn becomes a check-in of kind `promise` (`followups.promises`), raised at its time as "Waggle said it
would ...". Gate (open): 18 of 20 stated facts retrievable next day (not yet measured).

**Step 6, attention.** Already in place: `daily_text_cap` 8, the expected-value hold-back by kind, labels from the
owner's replies. New: `outreach_ev.gate()`. Live, last 7 days: 11 labelled, 8 welcome = **73 % useful**, busiest
day 0 unprompted initiative messages over the cap. Gate: 70 % over 7 days (met on this window; keep measuring).

**Step 7, learning from the nights.** The owner's reject/redo reasons and failed checks on an objective go into the
next brief of that objective ("Lessons from earlier nights"). The existing learning loop (`learnloop.py`) is
unchanged. Gate (open): 2 kept gains and 1 proven auto-revert.

**Gaps closed.** The watchdog now watches the Telegram gateway: when its process (`~/.hermes/gateway.pid`) is gone
it runs the "Waggle Gateway" task, at most once per 5 min, real home only (event
`gateway.relaunched_by_watchdog`).

**Not built: Step 8 (reach).** Own Telegram polling with Hermes removed, and the speech-model trial. Hermes removal
needs the replacement to run in parallel first (section 4 rule) and the voice trial needs large downloads, which
wait for a night with the owner away from the PC.

**Deploy (2026-11-23, live 4d9c943).** Full suite 4,069 passed. `tools/deploy.ps1`: guard approved (config,
policy), every long-lived process restarted (watchdog first, so it runs the gateway check), core healthy,
code-path canary 2.3 s. Live: timers `night.tick` (10 min) and `night.report` (07:30) registered; the evening line
reads "Tonight: SimBench PR #12 refresh; then #13; then #15 (+1). It will not start yet: the 7-day Claude limit is
at 85%". The week resets about 2026-11-25 06:00, so the first real night is 2026-11-25. Night tasks also need
`npm ci` from SimBench's own lockfile in the sandbox (`setup`, run by code).

## Thinking pass and Home page (built 2026-11-24, on the owner's word)

The owner's point: every example they gave turned into a new system. Cause, read from the code: the reasoning step
(`initiative.py`) only judges findings that a coded detector produced, so Waggle could only notice what somebody
had written a detector for.

**The thinking pass** (`waggle/think.py`, timers `outreach.think.0-2` at 07:40, 16:15, 19:45, job `outreach.think`,
switch `think_enabled`). One tool-less Sonnet call gets the whole picture (date, the world's calendar, the owner's
own words about what they want, profile, people, projects, coming up, what they said and did, weather, detector
findings, what was already told and how they reacted) and one open question. It may ask for up to three
read-only look-ups (vault, weather, one public page), answered in a second call. Each thought goes through the
existing guard rails (`initiative._apply`: topic repeat, daily cap, quiet hours, the expected-value check) as a
signal of kind `thought`, so it is logged, capped and labelled like every other unprompted message. At most one
question a day when it lacks a fact about the owner's life. Budget: its own allowance, 8 calls a day; unlike
long background work (which waits from 85 % of the week) these few small calls run until the week is at 97 %
(`budget.OWN_ALLOWANCE["think"]["week_stop_at"]`).

- Rule written into the module and tested (`test_the_prompt_names_no_situation`): a new situation is never a
  reason to add code or a prompt line. New code is only for a new thing Waggle can see. The owner's examples go
  into `bench/think_cases.json` and, in their own words, into the vault note `12 - Waggle/Me/Proactive.md`
  (`think.add_taste`, or the "Teach me" box on the Home page).
- Two things the first real-model runs showed, both fixed without naming a situation: Sonnet does not recall minor
  observances from a date alone ("Thu 2026-11-19: nothing specific"), so the pass got a sense for it
  (`waggle/worldcal.py`: the "Holidays and observances" list of each date's Wikipedia page, cached per month-day; the
  request names only a date); and with the list in front of it Sonnet still judged the day "not linked" until the
  picture carried the owner's stated taste.
- **Evidence** (`tools/think_eval.py`, real Sonnet, made-up pictures, nothing sent, results in
  [`THINK-EVAL.md`](THINK-EVAL.md)): 22 situations, none with code of its own. First full run 21 of 22 (the miss:
  it asked no question when it knew nobody; the question paragraph was reworded and that case moved to the tune
  group). Final run: tune 11 of 11, holdout 10 of 11 (the miss: on a day marked quiet it suggested a Thanksgiving
  thank-you two days ahead; the answers vary a little from run to run).
- Live picture from the real vault: 15.7k characters in 5.7 s; it has **no people section** (Waggle's `People`
  folder is empty), so the first passes will ask.
- Tests: `tests/test_think.py` (30), `tests/test_worldcal.py` (7).
- **Open (needs real days):** nothing has been sent to the owner by this pass yet. Gate: over 7 days, at least 70 % of its messages labelled welcome, and the owner names one it got right that
  nobody coded.

**Waggle Home** (`waggle/home.py`, served by the dashboard at http://127.0.0.1:8897/; the older panel is at `/ops`).
An orb to hold and talk to (or the Space key), a chat box, "On my mind" (the pass's thoughts with why, and a
"Think now" button), "Teach me" (adds a line to the taste note), what Waggle told the owner today, the work list,
the night report with Accept / Redo / Reject, reminders and usage.
- Typed and spoken turns go through the core's voice line as client `dash` (`voiceline.CLIENT_SOURCE`): the same
  engine turn, memory and permission hook as any owner turn. A typed message that starts with `[waggle-test]` is
  a canary turn on this path too (answered, never learned from).
- The page can now act as the owner, so every POST is checked (`home.check_post`): Host is this machine, Origin
  (when sent) is this page, and the request carries a token only this page was given. Tests: `tests/test_home.py`
  (12), including another site and another host name being refused.
- **Open:** the owner has not used it yet; the microphone path was not driven with real speech.

**Deploy (2026-11-23 23:45, live 3bd840e, then one Home page fix).** Full suite 4,118 passed
(`tools/locked_suite.py`); `tools/deploy.ps1`: guard approved (budget, config changed), core healthy, code-path
canary 2.2 s. Checked on live afterwards:
- Timers `outreach.think.0-2` enabled, next Tue 07:40.
- One real pass on the owner's real picture, dry (`python -m waggle.think run --dry-run`): 16,187 characters, one
  call, one thought nobody coded for (a trip starting in two days with no destination picked, clashing with a
  practice on the calendar). Nothing was sent.
- Home page: `GET /` 200, `/api/home` 200, `/ops` 200; a POST without the page token 403; a test-marked typed turn
  through the page answered in 4.2 s (trace 238, origin `canary`, source "typed in the Waggle dashboard").
- The owner's two lines are in `12 - Waggle/Me/Proactive.md`.
- Not done: no thought has reached the owner's phone yet (first scheduled pass Tue 07:40); the orb's microphone
  path was not driven with real speech; nobody has pressed Accept / Redo / Reject on a real night report.

**Home page: slow answers and job results now show in its chat (2026-11-25, live ffef25a).** The owner asked a long
question on Home; after the voice line's 25 s hold the page showed "Working on it" and the real answer went only to
Telegram, so it looked stuck. Now the voice line marks those copies `meta.late` (`voiceline.queue_copy`),
`/api/home` carries `late` (late dashboard answers plus background job results in the owner's chat,
`home.late_answers`), and the page shows anything newer than the moment it was opened, keeping the orb on
"Still working on it" until a held answer arrives. Full suite 4,123 passed (twice, 1b95b45 then ffef25a);
`tools/deploy.ps1` both times, code-path canary 2.2 s. Checked live: a test-marked "as a background job" question
typed on Home got "I'm on it", and the job's result appeared in the Home chat about 25 s later with no reload.
- Not driven live: a held answer (two test questions both finished under 25 s); covered by
  `tests/test_voiceline_core.py` (a real core, `meta.late` on the slow turn only) and `tests/test_home.py`.

**Long real-use tests and the fixes they forced (2026-11-25, live ad5c9c3).** The owner asked to "test this project to
hell and back" with real projects and to start the main test ("make money"). Two harnesses: `tools/scenarios.py`
(11 long scenarios, Sonnet judge, live voice line, canary-marked) and a 5-project workflow where Sonnet drivers played
the owner through `tools/say.py` and checked every claim against the real files, databases and GitHub.
- Real results: a stoichiometry app built and published by a background job; the money objective (9b763f92) did real
  research (16 platforms, 41 terms pages), built a DXF plate generator with 32 tests and a listing draft, and gave an
  honest estimate (most likely $0, best case $15-45 in month one); a college tracker note with sourced deadlines and a
  reminder that fired; PR #12 of ftc-simbench-pro refreshed on branch refresh/pr12-2026-11-25 (PR untouched); a full
  school day with every turn under 11 s.
- Fixed (each with tests): live job progress (`waggle/progress.py`, shown by background_status and objective_status),
  step checks that find files in the agent work folder (`objectives.resolve_named`), the chat prompt naming the real
  vault, own and work folders (stray writes to the retired ~/WaggleVault, a home-folder `find` that cost 100 s),
  guard false asks (sed scripts parsed instead of pattern-matched, `ForEach-Object Name`, prose mentioning Waggle no
  longer counts as an import), a leading "deny CODE" in a longer message counts, approvals stay out of unrelated
  answers, background reports start with done / not done, unseen news after the answer, `mail_recent`, objective
  progress lines for the evening note, calendar tool in local time, Home search Enter opens a note, test turns hidden
  from Home's Transmissions.
- Also new: the HUD Home with a Memory graph, the GitHub skill scout (daily 03:20; today's judge allowance was spent on
  dry runs, so the first real installs come tonight), and the vault in PARA folders (`tools/vault_para.py`, Waggle's
  folder is now `01 - Areas/Waggle`; undo manifest in WaggleBackups\vault-para-20261125-143805).
- Open: the money objective's step 3 waits on approval 7AP9 (a false ask the guard no longer makes; the owner decides);
  the chat under-reports reminder delivery (no tool reads the outbox state); an "approve CODE" inside a longer message
  still grants nothing by design; the money plan needs a parent account for any payout.
