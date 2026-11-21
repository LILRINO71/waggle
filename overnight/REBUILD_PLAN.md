# Rebuild plan v2 (2026-11-19): a foundation that can grow

Target, in the user's words: "a genuinely autonomous, continuously improving personal AI assistant... a system
that grows in capability over time, not another chatbot with more features." And: "Do not blindly add another
collection of cron jobs and scripts."

## Why v1 of this plan was not enough

v1 (same day) fixed the failures the live audit found, on the foundation that produced them: Waggle as a guest
inside the Hermes gateway, new logic hung on more scheduled scripts, "done" meaning tests passed. Two read-only
architecture reviews then checked the real code and logs against the six requirement areas (self-improvement,
autonomous work, persistent intelligence, proactive behaviour, performance, voice and communication).

What the audits and reviews measured:

| Area | Finding |
|---|---|
| Who answers | 11 of 16 real Telegram replies came from the local 4B model; Claude's work ran inside a 30 s Hermes hook |
| Host | The gateway restarted 40 times in 39 h (35 were Waggle deploys); its plugin API refuses to send or edit a message outside a live turn, so streaming, background results, follow-ups and proactive messages cannot live in it |
| Scheduling | Nine cron scripts, one attempt each, no catch-up; about 1,260 Python starts in 39 h; a brief and a digest were lost |
| Truth | All 75 trace rows say ok; fallback answers, timeouts and the lost reply were never recorded. Benches scored 100 % while real task success was 3 of 13 |
| Learning | Every learning part waits for a signal that never occurs (explicit corrections, tool sequences repeated three times) |
| Knowledge | 0 facts learned from real conversations; six chunks injected on every turn regardless of relevance |
| Safety | The agents can write to the policy, the hook, the budget code and the budget limits without asking; the promised hash check does not exist |
| Readiness | "Calls ready" is shown while no call is possible; nothing ever verified a real Telegram round trip |

## Decisions

1. **Waggle gets its own always-on core process.** Started at logon and restarted on failure by Task Scheduler,
   with a watchdog for hangs. It hosts the chat engine, the scheduler, background jobs, delivery and health.
   Deploys restart the core in about 2 s and no longer touch Hermes.
2. **One runtime store** (`~/.waggle/runtime.db`, SQLite WAL): events, inbox, chats, jobs with leases, timers
   (recurring and one-shot, each with a catch-up policy), one outbox, a capability ledger, daily metrics. Single
   writer per table family; idempotency keys; crash recovery at start. The PC is shut down at night, so the design
   catches up at logon instead of assuming it is always on.
3. **Waggle owns the Telegram transport, in reversible steps:** outbound first (direct Bot API through the
   outbox), then inbound through Hermes's `post_gateway_admission` hook (Hermes never starts a turn), then its
   own polling. Hermes keeps the tool bridge, desktop voice and the skills hub. Hermes cron and Hermes auto-TTS
   are replaced.
4. **"Ready" comes only from evidence.** A capability is ready when a recorded end-to-end success exists within
   its time limit and no failure followed it. Configuration being present never counts.
5. **Success means a checked outcome.** Files, commits and message ids a task claims are verified; a sampled
   judge sees the evidence, never the agent's own report; the user redoing the task elsewhere downgrades it.
6. **One improvement loop and one ledger,** not a second learning job: free per-turn failure detectors feed the
   ledger; once a day the loop keeps or rolls back yesterday's change, picks one weakness, proposes one change,
   gates it on local evaluation suites, deploys it on probation with a rollback point, and reports it in the
   evening note with an undo code. Knowledge misses and automation candidates go through the same ledger.
7. **A protected safety core.** Policy, hook, approvals, budget limits, evaluators and held-out sets are outside
   what any agent or loop may change: writes to them ask the user, the hook refuses everything on a hash mismatch,
   and the core re-checks hourly and before every deploy.
8. **Memory is a small maintained core plus tools,** not injected chunks: a generated core block (who the user
   is, current focus, commitments, deadlines, yesterday), memory tools the chat model calls, facts with validity
   dates, a nightly consolidation, and a quiz plus per-turn attribution to measure that knowledge is retained and
   used.
9. **Autonomous work runs through the objectives engine:** an activity model from the user's real work, a miner
   for repeated work, a proof step, three shadow runs with no outward effect, then the user switches it on.
   Standing goals with indicators make progress between conversations measurable.
10. **Every turn and every fact carries its origin.** Origins: the user's own channels, test, canary, bench,
    agent. Only the user's own words may feed the profile, facts, the digest, the quiz or the activity model.
    Development tests never run against the live home; end-to-end checks use a marked test chat. Found in real
    use: 50 of the first 78 recorded turns were development prompts sent through the CLI, one of them was filed
    in the profile as a fact, and Waggle then told the user about a test and a meet that do not exist.

## Architecture

```
              Telegram Bot API (send, edit, long poll; 24 h server-side queue)
                         ^  |
   +---------------------|--v-----------------------------------------------+
   | waggle-core (Task Scheduler: start at logon, restart on failure)       |
   |  transport  sender (outbox) . editor . poller (last step)              |
   |  engine     one long-lived Claude session per chat, streaming, tools   |
   |  scheduler  timers -> jobs, catch-up policy per timer                  |
   |  jobs       leases, retries, detached workers re-adopted after restart |
   |  governor   every Claude call; sub-allowances (chat first)             |
   |  loops      detectors . improvement cycle . consolidation . initiative |
   |  health     /health /metrics . capability ledger . guard checks        |
   +------|----------------|----------------|---------------|---------------+
     runtime.db        Hermes gateway    Kokoro, Ollama   claude CLI,
     (+ module dbs)    (tool bridge,                      objective workers
                        desktop voice)
   watchdog task: core heartbeat stale -> restart core
```

Kept: policy and approvals, budget reservations, the objectives engine, traces, vault and memory index, the
engine's stream parser and delivery interface, the ingest sources, the delivery logic of the outreach build.
Frozen or retired: the regex router and JSON-wrapped answers, the silent 4B fallback, per-message 4B fact
extraction, `selfbuild` as a trigger, the weekly learn job, journal route lines, the every-minute HQ rewrite,
every Hermes cron job (moved into core timers one at a time).
Not built: message brokers, containers, web frameworks, a second outbox, streaming audio for Telegram.

## Build order (each step is deployable and reversible on its own)

| # | Step | Proof before moving on | Rollback |
|---|---|---|---|
| 0 | Stopgap config: hook limit 300 s, queued follow-ups, restart notices off (done) | A real typed turn answered by Claude | Restore the config backup |
| 1 | Core skeleton: runtime.db, jobs, timers, events, capability ledger, health, logon task, watchdog; the guard for the safety core | Kill and hang tests; a tampered protected file fails closed; health green for a day | Disable the task |
| 2 | Outbound through the core: one outbox, direct Bot API, every notify path on it | A delivery confirmed by message id; a forced send failure is retried and delivered once | Switch the transport back to `hermes send` |
| 3 | Truth: the full turn record for every turn (who answered, timings, delivery, claims, version stamp), free failure detectors, the improvement ledger | The 16 real Telegram turns replayed as fixtures give the audit's counts | Detectors are read-only |
| 4 | Engine in the core, inbound through the admission hook, streamed replies | Text turn: first words within 2.5 s, 0 lost, restart mid-turn still answers | Per-chat flag back to the old hook path |
| 5 | Real end-to-end verifier (a test client logged in as the user, test-only) and canaries feeding the ledger | A text round trip recorded after every deploy and daily | Disable the canary |
| 6 | Knowledge on the core: ingest and digest as jobs, facts with validity dates, quiz with held-out split, vault under local git | Quiz baseline recorded; ingest idempotent on the real vault copy | Jobs off; vault git revert |
| 7 | Close one loop: quiz miss -> fix -> re-quiz -> keep or revert -> evening note with undo | The first real miss fixed overnight and reported; `undo` works from Telegram | Loop off; ledger keeps the record |
| 8 | Initiative on the core: watchers, reasoning step, follow-ups and one-shot reminders as timers, evening note | A synthetic finding delivered; a 2-minute reminder arrives; a missed note caught up | Timers off |
| 9 | Voice: short spoken answer plus text for voice notes, sent by the core; Hermes auto-TTS off | A real voice note gets a voice reply of at most 20 s and the text | Auto-TTS back on |
| 10 | Timers replace the nine cron jobs, one at a time; then inbound cut-over (the core polls) | Each job two good core runs before its cron entry is disabled | `hermes cron resume`; re-enable Hermes Telegram |
| 11 | Evaluation suites (real-turn replay, task sandbox), prompt/config/skill changes with probation and automatic rollback, weekly scoreboard | A simulated tripwire rolls a change back | Loop off |
| 12 | Autonomous work: activity model, repeated-work miner, shadow mode, goals and per-goal progress | One automation shadow-run three times and proposed | Disable |
| 13 | Waggle's own code through a gated worktree pipeline; core memory, consolidation, attribution | One real bug fix merged on the user's approval | Recorded rollback point |

## Definition of done per capability

Every capability needs all five: a stated done-condition, an automated test, a real end-to-end check, a metric
that shows it keeps working, and failure handling that leaves a record.

| Capability | Done when | Automated test | Real check | Metric |
|---|---|---|---|---|
| Text turn | Claude answers at least 95 % of turns while quota allows; 0 lost | Fake Bot API: turn, stream, final; core killed mid-turn still replies | Test client: first edit within 2.5 s, final text | Share answered by Claude; time to first words p50/p90; lost = 0 |
| Tool or background job | Result delivered exactly once, also across a core restart | Lease expiry and re-adoption; idempotent delivery | A 2-minute job with a restart in the middle | Dead jobs per day; time to result |
| Voice note | Every voice note gets at most three spoken sentences plus the text | Stored audio through STT, spoken-block length, valid Opus | Test client sends a voice note; reply holds a voice message | Share with spoken reply; voice p50/p90 |
| Proactive message | At least one useful unprompted message a day chosen by reasoning; 0 lost | Outbox backoff, quiet hours, cap, expiry | Synthetic finding delivered, id confirmed | Sent, failed, expired per day; replies to outreach |
| Follow-up or reminder | Fires once on time, or late with a note after the PC was off | Catch-up policies with a fake clock | "Remind me in 2 minutes" arrives | Timers late or missed |
| Phone call | A real call rings the user's phone | Client against a fake server | Trial call plus the user's confirmation | Readiness from the ledger only |
| Knowledge retained and used | Held-out quiz accuracy tracked nightly; knowledge used when a turn needs it | Planted miss fixed end to end on a temp vault | A real miss fixed overnight and shown | Quiz trend; re-told facts per week |
| Self-improvement | One change a day at most: measured, kept or rolled back, reported, undoable | Attempt lifecycle incl. tripwire rollback and undo | First real change kept or rolled back with numbers | Kept per attempted; changes without a report = 0 |
| Safety core | Protected writes ask; tampering fails closed | Policy tests per protected path; hash-mismatch test | Staged tamper in a throwaway copy trips the alert | Hourly integrity check |
| Autonomous work | One automation proved, shadow-run, enabled by the user, used | Miner on synthetic logs; objective with proof step | First real automation runs for two weeks | Use count; verified outcomes per goal per week |
| Core availability | Restarted within 2 minutes of a crash or hang; no orphan processes | Kill and hang simulations | Watchdog log and health endpoint | Uptime while the PC is on |

## Budget

All Claude use goes through one governor: chat first, then user-started work and deliveries, then consolidation,
judge samples, the evening note, improvement work, scouting, research. Improvement plus scouting are capped at
1.5 of the 4 daily background units; rollback and probation checks need no budget. Detectors, mining, clustering
and quiz scoring run locally for free.

## Status

Done 2026-11-19: four live audits; two architecture reviews; step 0 deployed (gateway restarted 12:08 with the
stopgap config). In progress on separate branches: the portable chat engine, knowledge intake, and the delivery
logic for outreach (paused at a checkpoint until step 1 exists). Nothing beyond step 0 is deployed.

2026-11-19 14:20: the open decisions were accepted as recommended. Step 1 is being built on two branches:
`rebuild/core` (runtime store first, then the always-on process) and `rebuild/guard` (the safety core). Step 0
is still unproven: no real Telegram message has arrived since the restart. The checks only the user can do are
in `overnight/USER_TESTS.md`.

2026-11-19 14:28: step 0 is proven for typed turns. Three real Telegram messages were all answered by Claude
(15.7 s, 3.1 s, 24.9 s); the second waited behind the first; the local model answered none. Two faults showed:

- Wrong facts volunteered from the profile. Cause: the per-message extractor filed development test prompts as
  profile facts (decision 10). Done: extractor retired (84cc091), the six machine-written lines moved out of the
  profile into a history note. Still open: the knowledge build must filter by origin before it learns from
  recorded turns, and test notes left in the real vault need the same clean-up.
- The agent said GitHub was not signed in. The same agent chain works from a normal shell, so the fault is in the
  environment of the gateway that was started from a tool session. The real output is unknown because agent tool
  results are not recorded anywhere. Step 3 must record them; a turn that falls back to memory because a tool
  failed must count as a failed task, not a success.

2026-11-19 17:05: step 1 is deployed. The core runs under Task Scheduler ("Waggle Core", "Waggle Core
Watchdog"), version stamped, health endpoint on 127.0.0.1.

| Check on the live machine | Result |
|---|---|
| Core killed | Healthy again after 57 s, restarted by the watchdog |
| Core frozen (process suspended) | Replaced after 356 s. Misses the 2-minute target; the watchdog moves to every minute |
| One real message through the outbox (step 2) | Sent with a Telegram message id in 2.6 s; the same key did not create a second row |
| Forced send failure retried and delivered once | Automated tests only, not yet on the live machine |
| Health green for a day | Pending; green since 17:04 |

Not yet on the core: the existing notify paths, the engine and the outreach timers. They are combined on
`rebuild/integration` (550 tests pass) and are not live. Merging to main is a deploy, because the scheduled
scripts and the tool hook run straight from the live checkout.
