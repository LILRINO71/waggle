# Waggle: final architecture decision document (revision 3, 2026-11-21)

Planning only. Nothing was changed, installed, disabled or deleted. No subagents. Evidence comes from the live
machine (repo, import graph, git, Task Scheduler, databases, both vaults, the account usage card), the GitHub
API and PyPI, and web search. Anything not verified is listed in section 9.

**Owner decision recorded:** Waggle uses `PersonalIntelligenceVault`, not `WaggleVault`. Section 3 shows the
read/write evidence agrees.

## 0. What changed in this revision (I challenged my own plan)

1. **Pipecat is downgraded from "trial to adopt" to "deferred".** It would not fix the owner's actual voice
   complaints, and it has four concrete blockers on this PC (section 1.3).
2. **The Agent SDK is no longer on the critical path.** The 120k-token fix needs two command-line flags the
   current code can pass today. The SDK is a plumbing replacement with a real Windows risk; it becomes an
   experiment, done after autonomy is proven.
3. **The roadmap was bottom-up, which is how Waggle got broad and unused.** The new order puts one real
   overnight objective, end to end on today's machinery, *before* rebuilding the work ledger. The ledger is then
   built against observed failures instead of imagined ones.
4. **New code does not go inside the always-on core.** The Agent SDK alone pulls in about 20 packages. Ledger
   and workers run in a second supervised process with its own environment, so a fault there cannot stop mail,
   reminders or delivery.
5. **The cleanup list in revision 2 was too optimistic.** The import graph shows `router`, `specialists` and
   `proactive` still have live callers. Cleanup is now tiered and evidence-gated (section 4).

---

## 1. Validation of the three reuse decisions

### 1.1 Claude Agent SDK (MIT, v0.2.187, alpha, releases almost daily, 680 open issues)

| Risk found | Evidence | Consequence |
|---|---|---|
| Console window flashes from a windowless parent | Open issue #480 "Hide subprocess terminal for Windows system". Waggle's core is `pythonw`; this is the same class of fault that made the owner switch Waggle off on 11-19 | Must be proven to show zero windows before any live use |
| Windows wheel gaps | Issue #1349: no `win_amd64` wheel for 0.2.160-0.2.163. Today's 0.2.187 has one | Pin an exact version; never auto-upgrade |
| Refuses npm `.cmd` shims; long prompts overflow the command line | Source `subprocess_cli.py`; issues #883, #1096 | Point `cli_path` at a real `claude.exe`; keep prompts in files |
| A tool can run twice after a crash on resume | Issue #1549 | Every autonomous step must be safe to repeat |
| Restricted-execution pipe failure | Issue #1457 (WinError 5) | Check under Task Scheduler's context, not only a terminal |
| Dependency weight | Needs `mcp` + `jsonschema`; `mcp` is not installed in the core Python and brings a large tree | Runs in the work process, never in the core |
| In-process hook failure mode unknown | Today the policy hook is a separate process that fails closed | Prove that a crashing or slow hook denies, not allows |
| Python 3.14 not in its classifiers | `pyproject.toml` lists 3.10-3.13 | Prove on 3.14 |
| Which login it uses | The bundled CLI is a different binary from the one Waggle calls today | Prove it runs on the existing login with no new sign-in |

**Trial (throwaway home, test chat only, pinned version):** 30 turns including tool use, launched from
`pythonw` under Task Scheduler. **Pass only if all hold:** 0 windows, 0 focus changes (`tools/window_probe.py`);
fixed context under 20k tokens; a hook that raises or sleeps 60 s results in a denied tool call; resume after a
kill works; p50 latency no worse than today; the usage event still reports the normal 5-hour and weekly windows.
**If it fails:** stay on the current CLI path indefinitely. Nothing in the roadmap depends on it.

### 1.2 DBOS Transact (MIT, v3.6.0, Production/Stable, Python 3.10-3.14, wheels available for 3.14 on Windows)

| Risk found | Evidence | Consequence |
|---|---|---|
| The SQLite backend is young | Closed in recent months: #940 (queue concurrency exceeded on SQLite), #939 (write skew), #922 (concurrent first migration fails), #920 (README did not mention SQLite). Open: #966 (ordering within one millisecond), #974 ("cancelling a failed workflow does nothing") | Trial must hammer concurrency, crash and cancel on SQLite specifically |
| Recovery is tied to an application version | `_dbos.py`: the version defaults to a hash of the workflow source; config text says to "set application_version explicitly" to keep deployments on one version | Without pinning, a deploy can orphan a multi-day objective. Pin the version and keep the workflow function tiny and stable (a generic interpreter that reads the plan from the database) |
| A step that dies re-runs from its start | Standard at-least-once semantics | A 20-30 minute agent step must be restartable: state file, worktree commits, session resume |
| SQLite limits | "Filtering workflows by attributes is not supported on SQLite"; notifications are polled | Acceptable; note for the status view |
| Dependencies | SQLAlchemy 2.1 and psycopg (LGPL-3.0, installed but unused on SQLite; issue #973 asks to make it optional) | Work process only. LGPL as a dependency is compatible with a public MIT-style repo; no code is copied |
| Outbound connections | Conductor and OTLP are opt-in by key | Confirm no listening port and no outbound traffic by default |

**Trial (throwaway home):** one 4-task objective with a dependency and an approval wait. Kill the process
mid-step three times; change unrelated code and redeploy mid-objective; run two workers against one file;
cancel one task. **Pass only if:** every task completes exactly once from the ledger's view; the objective
survives the redeploy; the approval wait survives a restart; idle CPU under 1 %; no network traffic; works
under `pythonw`. **If it fails:** extend today's `objectives.py` (keeps working code) and borrow loopx's
contract. loopx itself is tried only then (one maintainer, several releases a day, needs Node 22).

### 1.3 Pipecat (BSD-2, v1.16.0, release every 1-2 weeks): deferred

| Blocker | Evidence |
|---|---|
| Its local audio transport cannot install on Waggle's Python | It uses PyAudio; PyAudio 0.2.14 (last upload 2023-11) has no Python 3.14 wheel for Windows |
| Its Kokoro service cannot run on Waggle's Python | `kokoro-onnx` requires Python < 3.14 (Waggle already isolates Kokoro in its own environment) |
| No echo cancellation for open speakers | Code search finds it only inside cloud services. With a webcam microphone and speakers, interruption handling would hear Waggle's own voice |
| No wake word | Waggle's sherpa keyword spotter would have to stay in front |

It also does not address what the owner reported. "Sounds robot-like" is the speech model, and "slow end of
speech" is turn detection that Waggle already built and switched off. **Decision:** keep the current voice
pipeline; in the Reach phase try a different speech model (Chatterbox Turbo or Nano) first; reconsider Pipecat
only if turn-taking still misses its targets afterwards, and then in its own Python 3.12 environment. One
useful finding: Pipecat's Smart Turn v3 runs on `onnxruntime` alone, so the model itself is cheap to retry.

---

## 2. The subscription-cost assumption

### 2.1 Confirmed on this account today (first-hand)

- Plan: **Pro**. **Extra usage: disabled.** So a limit can stop Waggle but cannot bill the owner.
- The usage card has three meters: 5-hour (10 %), weekly all models (82 %), weekly Fable (9 %). **There is no
  separate meter for headless or Agent SDK use.**
- Waggle's own `claude -p` calls receive limit events for the same two windows. At 18:27 the event said weekly
  0.79 and 5-hour 0.31, matching the 79 % and about 34 % the owner read off the dashboard. Headless calls are
  being counted against the normal subscription meters.
- This planning session moved weekly all-models from 79 % to 82 % and weekly Fable from 0 % to 9 %.
- Waggle's recorded use over 3 days: about 47 million input-side tokens (almost all cached re-reads) and 0.26
  million output tokens.

### 2.2 Assumed or unknown

- Whether Anthropic will meter `claude -p` / Agent SDK use separately. A change was reported for 2026-06-15,
  then reported paused. Secondary sources only.
- What a Pro credit would be if it happens (reported: $20 a month).
- Whether an always-on personal agent counts as "ordinary individual use" under the consumer terms.
- How cached tokens are weighted against plan limits (unpublished).

### 2.3 Consequences if headless use becomes separately metered

- At assumed Sonnet-class list prices, Waggle's current behaviour is roughly **$10 a day** (estimate; prices not
  verified). After the lean-context and canary fixes, perhaps **$1-3 a day** for chat plus light background.
- A $20 monthly credit would cover about one to two weeks of chat and **no night work**. With extra usage off,
  Waggle would go quiet rather than charge anything.
- Design rules that follow, all adopted:
  1. Duties (mail, reminders, alerts, brief) never need Claude. Already the owner's rule.
  2. Chat has a ladder: code -> local model -> Claude. When Claude is unavailable Waggle says so and the local
     model answers.
  3. The night worker sits behind a seam. The ledger stores briefs and results in the database and files; a
     worker is "anything that can read a brief and write back". The fallback worker is a first-party scheduled
     Claude Code session, which is ordinary interactive use. The owner's Titan autopilot already worked this way.
  4. Every objective carries a token budget; there is a daily and weekly cap.
  5. Waggle alerts the owner the first time a limit event names a meter it has not seen before.

---

## 3. The vault question, resolved by evidence

| Check | `WaggleVault` | `PersonalIntelligenceVault` |
|---|---|---|
| Last file written | 16:36:15 today (dashboard note) | 20:53 today, still changing every minute |
| Last git commit | 14:48 today (35 commits) | 20:48 today (48 commits, in `12 - Waggle`) |
| Notes present | 132 | all 132 at the same relative paths in `12 - Waggle`, plus 2 newer |
| Notes that exist only here | **0** | 2 |
| Writes outside Waggle's folder | n/a | only `.obsidian` files, timed with the owner opening the vault in Obsidian at 15:53 |
| Open in Obsidian | no (still registered) | yes |
| `config.json` `vault` | no | yes |
| "Feed Waggle" shortcut, Hermes config, Hermes skills, routines, search index | 0 references | all point here |

**Conclusion:** since 16:37 today every read and write goes to `PersonalIntelligenceVault`; Waggle writes only
inside `12 - Waggle`; the old vault holds nothing unique. The owner's choice and the evidence agree.

**Loose ends (to fix, with approval):**
1. `waggle/config.py:14` still defaults to `~/WaggleVault` when the setting is missing. A fresh or damaged
   config would silently start writing to the old vault. Change to fail closed.
2. Same fallback in `tools/make-drop-shortcut.ps1` and `tools/learnloop_proof.py`.
3. The old folder: zip it to `WaggleBackups`, verify the zip opens, rename to `WaggleVault.archived-2026-11-21`,
   watch for 7 days for anything that complains, and delete only on the owner's word.
4. The dashboard note is rewritten every minute inside the owner's vault and committed. Write only on change.

---

## 4. Dependency-safe cleanup plan

**Rule:** nothing is removed until five things are true: (1) callers checked three ways (static import graph;
dynamic references such as the plug-in list, `-m waggle.X` strings, timer rows, Task Scheduler, the Hermes
plug-in; and a run in a throwaway home), (2) a backup exists and a restore from it was tested, (3) the change
was first made in its reversible form, (4) it soaked 48 hours with duties intact, (5) the rollback step is
written down.

**What the evidence shows now**

- Git: `main` equals `origin/main`. Every branch is merged into `main` except `wip/2026-11-21-untested` (on
  origin). 19 `worktree-agent-*` branches exist only locally, all merged. No worktree has uncommitted changes.
- Backups: one nightly archive (13 MB, 03:30 today) and the vault-move backup. **No restore has ever been tested.**
- Static importers of the revision-2 "retire" candidates:

| Module | Importers | Other references | Verdict |
|---|---|---|---|
| `selfbuild`, `pushsetup` | 0 | none | Dead. Remove in tier 3 after the dynamic check |
| `router` | bench, cli, evals, learning | **Hermes plug-in** (the fallback path when the core is down) | Live. Goes only when the Hermes fallback goes |
| `specialists` | agents, bench, evals, router | none | Live |
| `proactive` | scout, watchers | none | Live (helper functions) |
| `calendar_watch` | brief | core plug-in list; 2 timers | Switch off, do not remove |
| `deadman` | tick | core plug-in list; 1 timer; setup script | Switch off, do not remove |
| `call` | 7 modules | Hermes plug-in; 62 tests | Leave inert |
| `jobs` | 15 modules | Hermes plug-in | Live until the ledger replaces it |
| `routines`, `hq` | 6 and 1 | Hermes plug-in, installer | Live |
| `bench`, `feedwatch`, `usage_view`, `scoreboard`, tool packs | 0 | run as commands or loaded by name | **Not dead.** Shows why the static graph alone is not enough |

**Tiers**

| Tier | Action | Reversible form | Rollback |
|---|---|---|---|
| 0 | Take a backup; restore it into a throwaway home and start the core there; tag `main` as `pre-cleanup` and push the tag | n/a | n/a |
| 1 | Git housekeeping: `git worktree remove` (refuses if dirty), `git branch -d` (refuses if unmerged). Keep `wip/...` and all remote branches | Everything is on `main` and tagged | Re-create from the tag |
| 2 | Stray `Hermes_Gateway` task | `Disable-ScheduledTask`, not delete | `Enable-ScheduledTask` |
| 2 | Calendar and dead-man timers; per-minute dashboard | Config switches; first prove in a throwaway home what a disabled timer does at the next core start | Flip the switch back |
| 3 | Dead modules (`selfbuild`, `pushsetup`) | A commit that removes them and their tests | `git revert` |
| 4 | Entangled legacy (`router`, `specialists`, `proactive`, `jobs`, `routines`, `call`) | None now | Each is removed only by the phase that replaces its callers |
| 5 | Old vault | Zip, verify, rename | Rename back |
| never | Databases, config backups, logs, the owner's notes | — | — |

---

## 5. Roadmap order, revisited

**What was wrong:** phases 0-2 delivered infrastructure for weeks before the owner saw any autonomous result,
and the SDK migration sat in front of everything.

**Missing or too late in revision 2:**
- A tested restore before any cleanup (now step 0).
- Feedback capture ("useful / not useful", accept / reject) from day one. The improvement loop needs months of
  it, so collection starts immediately even though the loop is last.
- A real-request replay set, recorded from day one.
- The untrusted-content rule before autonomous workers get web access (now in step 1, not step 5).
- The weekly planning conversation. It needs no code and gives every later step real work to run on.
- A complexity budget: the count of own modules must go down at each step.

### Final recommended implementation order

| Step | Name | What the owner gets | Gate to the next step |
|---|---|---|---|
| 0 | Safety net and truth | Tested backup; freeze; status table; baseline numbers; reboot test; vault default fixed; cleanup tiers 0-2; feedback and replay capture switched on; first weekly plan (3 objectives, by voice) | Restore works; duties intact 48 h |
| 1 | Lean, unblocked chat (current CLI path) | Same abilities, far cheaper and faster; no false asks; untrusted-content rule; canary reduced | Fixed context under 20k; 0 false asks in 50 turns |
| 2 | Isolated trials | Nothing visible. Agent SDK and DBOS judged against 1.1 and 1.2 in a throwaway home | Written pass/fail for each |
| 3 | **First night** | One real approved objective worked overnight on today's machinery, in a sandbox, independently verified, delivered as a branch, with a morning report to accept or reject. Repeated for 5 nights | At least 3 of 5 nights produce an accepted result; failure modes written down |
| 4 | Work ledger and supervisor | One answer to "what are you working on"; five work paths become one; built on DBOS if its trial passed, shaped by step 3's failures | A 3-night objective survives a restart and a deploy |
| 5 | Memory, commitments, safe intake | Facts land the day they are said; promises are tracked | 18 of 20 facts retrievable next day |
| 6 | Attention with feedback | Few unprompted messages, most marked useful | 70 % useful over 7 days |
| 7 | Improvement loop and earned autonomy | A weekly line: what changed, by how much, what was reverted | 2 kept gains, 1 proven auto-revert |
| 8 | Reach | Better voice (speech-model trial), own Telegram polling and Hermes removed, Agent SDK migration if its trial passed, calls if wanted | Each by measured trial |

Rules for every step: one at a time, one branch, one builder; recorded end-to-end evidence from the owner's real
use; a 2-3 day soak; a usage cap; the status table updated; nothing working removed until its replacement ran
in parallel.

---

## 6. The exact end state

### 6.1 A normal night

- **21:00** Evening note: what Waggle plans to work on tonight, drawn from the approved weekly objectives. The
  owner can change it by voice or one line.
- **23:00-07:00** Night shift.
  - The supervisor takes the highest-priority runnable task (owner-approved objectives first, then deadlines
    and commitments).
  - Each task: a written brief -> a worker in a sandbox (a worktree or the objective's folder) -> independent
    verification -> result and evidence recorded -> next task.
  - It never sends a message to another person, never pushes to a main branch, never spends money, never
    installs third-party code. Anything like that becomes a question and the task parks while others continue.
  - It stops at the night's budget.
  - Duties keep running: mail is triaged locally; only a P0 (a deadline within 3 hours, a security event, an
    alarm the owner set) wakes the owner.
- **07:30** Morning report, one message:
  - per objective: what was done, the evidence (tests, diff size, link to the branch), what it cost;
  - what is blocked, as a question with a default answer;
  - anything Waggle noticed and proposes, never something it started on its own outside the approved goals.
  - The owner answers each item with one word: accept, reject (with a reason if they like), or redo.

### 6.2 How it decides what matters

1. Objectives the owner approved in the weekly plan, in the owner's order.
2. Dated commitments and deadlines from the owner's notes, mail and chat.
3. Signals from sensors that pass a cheap gate and one batched reasoning call.
4. Nothing else. Waggle may *propose* new goals in the morning report; it does not adopt them alone.

### 6.3 How it verifies

1. Checks code can run: tests, build, lint, file diff, link check, reading back from an API.
2. Where code cannot judge (research, writing): a second model call with the evidence and a checklist, in a
   fresh context.
3. The owner's accept or reject. This is the only measure that counts as success; the first two exist to keep
   bad work from reaching the owner.

### 6.4 How it learns

- Signals: accept / reject, corrections in chat, "useful / not useful" on notifications, failed checks.
- Weekly: one review clusters the failures into hypotheses; each becomes an experiment with a metric; it is
  tested on a replay of the owner's real requests; kept only on a measured gain; reverted automatically on a
  regression; recorded with its delta.
- Trust per domain rises with accepted work and falls on a rejection.

### 6.5 Measurable definition of a fully working Waggle (rolling 14 days)

| Area | Threshold |
|---|---|
| Duties | Up at least 99 % of waking hours; 0 missed reminders; back within 5 minutes of sign-in after a reboot; work at 100 % Claude usage |
| Autonomy | At least 10 of 14 nights yield one or more accepted deliverables; one objective spanning 3+ nights completed |
| Acceptance | At least 70 % of morning items accepted |
| Verification | 0 of 10 audited "done" items fail their check when re-run |
| Safety | 0 actions from the always-ask list taken without a yes; 0 owner notes modified |
| Chat | p50 at most 5 s, p90 at most 15 s; at most 1 false ask per 100 owner turns; fixed context at most 20k tokens |
| Memory | 18 of 20 stated facts retrievable next day; 15 used correctly when relevant |
| Attention | At least 70 % of unprompted messages marked useful; at most 8 a day; 0 seeded P0 missed |
| Learning | At least 2 kept improvements a month with a measured gain; 1 proven auto-revert |
| Cost | Waggle's share within the owner's cap; never above $0 |
| Review | The morning review takes the owner at most 10 minutes |
| Size | Own code at most half of today's 160 modules / 3 MB |

---

## 7. Where this design could still fail

1. **Unattended judgement.** The model may plan poorly or drift from the goal with nobody watching. Passing
   checks is not the same as being useful. Mitigation: owner-approved objectives, acceptance rate as the only
   success measure, small tasks. Not eliminated.
2. **The loop depends on the owner.** No weekly plan and no morning answers means no direction and no learning
   signal. The design cannot work around an absent owner; it can only make review take minutes.
3. **Subscription economics.** If headless use is metered at a small credit, night work on Claude ends. The
   fallback (first-party scheduled sessions) is untested here and gives less control.
4. **Too little data to learn from.** About 20 turns a day gives weak statistics. The improvement loop may
   produce a handful of real gains a month, not continuous growth. Claiming more would be dishonest.
5. **Reach of the tools.** Much of the owner's real work is in Onshape, school portals behind logins, and a
   physical robot. Waggle's autonomous value will concentrate in code, research, documents and mail triage.
6. **Night-time desktop control.** UI automation needs an unlocked interactive session. Work that drives apps
   on screen cannot run while the PC is locked, and collides with the owner when it is not.
7. **One machine.** A Windows update, a power cut or a full disk stops everything. There is no second host.
8. **Correlated verification.** A verifier from the same model family can share the worker's blind spots.
9. **Wrong memory poisons autonomy.** One false fact, trusted, can steer many nights. Mitigation: origin rules,
   observation class, history. Not eliminated.
10. **Trust is fragile.** One bad unattended action costs more than fifty good ones earn.
11. **Complexity regrowth.** The habit that produced 160 modules is still there, in the builder as much as in
    the plan. The size budget and one-step-at-a-time rule are the only guard.
12. **Dependence on fast-moving parts.** An alpha SDK, a young SQLite backend and a vendor policy in flux.
    Everything adopted is pinned and trialled; none of it is risk-free.

---

## 8. Decisions

### 8.1 Ready (evidence sufficient; proceed on approval)

1. The vault is `PersonalIntelligenceVault`; Waggle writes only in `12 - Waggle`.
2. Keep the core runtime, transports, outbox, reminders, mail watch, voice models, memory index and fact
   model, vault git, Google and Windows tools, deploy and rollback.
3. The brain is first-party Claude Code. No runtime with its own model loop becomes the brain.
4. Duties run without Claude.
5. Lean chat context through CLI flags on the current path; rotate sessions by tokens; reduce the canary.
6. Permission by effect with a short always-ask list; untrusted content is read only by a model with no tools.
7. New ledger and workers run in a second supervised process, not in the core.
8. Not adopted as dependencies: Temporal, LangGraph, AutoGen (archived), CrewAI, OpenHands, OpenClaw, Letta,
   Mem0, Graphiti, Hindsight, LiteLLM, browser-use.
9. Pipecat deferred.
10. Inspect AI and GEPA for measurement and prompt improvement, development environment only.
11. The order in section 5: first night before ledger rebuild; improvement loop last.
12. Feature freeze; one builder; no parallel build waves; a usage cap per step.
13. Cleanup only through the tiers in section 4.

### 8.2 Requiring experiments (pass/fail criteria in sections 1 and 4)

1. Agent SDK adoption (1.1).
2. DBOS for the ledger (1.2); loopx only if DBOS fails.
3. What a disabled timer does at the next core start.
4. Backup restore into a throwaway home.
5. Reboot behaviour with one gateway.
6. First-party scheduled session as a night worker.
7. Chatterbox Turbo / Nano against Kokoro on this hardware.
8. `agent-memory` against `memory.py` on the owner's own questions.
9. How much accepted work one night's budget buys (measured in step 3).

### 8.3 Requiring the owner's approval

1. The always-ask list: money; passwords and keys; messages
   or posts to other people; permanent deletes; security settings; installing third-party code; Waggle's own
   policy files.
2. Whether Waggle may push branches named `waggle/*` to the owner's own repositories and open draft pull
   requests, never merging.
3. The night cap (suggestion: 25 % of the weekly allowance) and the night hours.
4. Reboot: Windows auto sign-in, or accept the gap with a phone alert.
5. Cleanup tiers 1-3 and archiving the old vault.
6. A second supervised process with its own Python environment and third-party packages.
7. Removing Hermes in step 8.
8. A fixed time for the weekly planning conversation, and the first three objectives.

---

## 9. Remaining unknowns

- Whether headless Claude use will be metered separately, and on what terms.
- Whether Sonnet plans and executes multi-hour work well enough unattended (step 3 measures it).
- DBOS's SQLite backend under real use on Windows; the Agent SDK under `pythonw`.
- How to verify non-code work reliably.
- Reboot behaviour; gateway failure rate; current test-suite state (not run in this review).
- How plan limits weight cached tokens; current Sonnet prices (the $10-a-day figure assumes list prices).
- Whether a first-party always-on agent is coming (rumour only).
- Whether the owner's review habit holds, which the whole loop depends on.

---

## Appendix A. Audit summary (from revision 1, verified 2026-11-21)

- Proven: core runtime (3,002 jobs in 49 h, 1 dead; kill -> back in 51-57 s), reminders (7 of 7), Telegram text
  and voice notes, the Sonnet chat brain (61 owner turns, p50 6.6 s, p90 22 s), mail watch (caught a real
  college deadline).
- Not working in practice: autonomy (4 steps in 2 days, all "write a note"), proactive reasoning (1 decision
  ever), self-improvement (0 kept changes), learning about the owner (6 facts).
- Cost drivers: about 120,000 tokens of fixed context on every model call; a deploy canary that spends as much
  as the owner; most of the weekly allowance went to building, not running.
- Structure: five ways to start background work; 15 databases; permission logic is the largest code (160 KB)
  and caused 3 of the owner's 61 turns to lose their work.

## Appendix B. Research verdicts (from revision 2)

Adopt: Inspect AI, GEPA. Adopt after trial: Agent SDK, DBOS. Borrow the design: loopx, nanoclaw (script gates,
one writer per file), OpenClaw (standing orders), OpenHands (stuck detection), Mem0 (update step, already
done), OpenJarvis (trace-learn-eval, already done). Keep as is: Windows-MCP, Playwright MCP, Waggle's timers
and outbox. Trial later: Chatterbox, `agent-memory`. Deferred: Pipecat. Avoid: section 8.1 item 8. Shrink then
remove: Hermes (its goals loop and task board need Hermes's own model, which the subscription does not cover).

## Sources

- GitHub API and source files, 2026-11-21: anthropics/claude-agent-sdk-python (issues #480, #883, #1096, #1349,
  #1457, #1549), dbos-inc/dbos-transact-py (issues #920, #922, #939, #940, #966, #973, #974),
  pipecat-ai/pipecat, loopx-project/loopx, and the repositories listed in revision 2.
- PyPI metadata, 2026-11-21: claude-agent-sdk, dbos, psycopg-binary, sqlalchemy, pipecat-ai, PyAudio,
  kokoro-onnx, onnxruntime.
- Anthropic, "Building always-on agents": https://claude.com/blog/building-always-on-agents
- Subscription policy (secondary): https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access ,
  https://thenextweb.com/news/anthropic-openclaw-claude-subscription-ban-cost/ ,
  https://venturebeat.com/ai/anthropic-reinstates-openclaw-and-third-party-agent-usage-on-claude-subscriptions-with-a-catch/ ,
  https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026 ,
  https://claudefa.st/blog/guide/development/agent-sdk-credit
- Local evidence: `~/.waggle/*.db`, `~/.waggle/real_usage.json`, both vault folders, `git` in
  `C:\Users\hiheo\Claude\waggle`, Task Scheduler, the account usage card.
