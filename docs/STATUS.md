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
   Known gaps: `curl` of an HTML page in the shell, Hermes browser tools (wrapped `<untrusted>`, not reduced), and
   Drive/Docs connector content for agents.

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
- Seen while measuring: every turn re-sends the context block (~2k tokens), so the session grows ~3k tokens a turn
  and rotates after about 12-14 turns. A candidate for the next step (send the block only when it changed).
