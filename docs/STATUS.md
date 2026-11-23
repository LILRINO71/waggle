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
| Chat brain (Sonnet session, tools, Google) | yes | yes | yes | partly | p50 6.6 s, p90 22 s, max 37 s; 6 of 61 owner turns degraded. About 120,000 tokens of fixed context per call |
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
| Permission hook (guard + policy) | yes | yes | yes | **over-blocks** | 3 of 61 owner turns lost their work to a false ask; 11 of 17 approvals never answered |
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
