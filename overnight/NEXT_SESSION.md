# Next session: where to pick up

Read in order: MASTER_PLAN.md -> DECISIONS.md -> FEATURE_MATRIX.md (bottom section = phase 2) -> PROGRESS.md (last
section) -> BLOCKERS.md.

## North star (keep this in view)
Waggle should be the assistant the user *chooses* over opening ChatGPT or Claude: Claude-level conversation plus
persistent knowledge of their projects, voice, Telegram, and tasks done independently while they're at school,
asleep or busy. Make the assistant better, not just the infrastructure bigger.
- Quality first: a cheaper answer that is noticeably worse is not an optimisation.
- Reliability, safety and finished work beat feature count; never weaken approvals or verification for a test.
- Small, tested, reversible changes; claim "works" only after the real behaviour is tested.
- Reuse existing tools from GitHub and combine their strengths instead of rebuilding them.
- 2026-11-19: Waggle work moved from the mission 3 session to the user's main orchestrator chat, which may
  delegate per task (the earlier "one session, no subagents" rule no longer applies). The full mission briefs and
  that session's transcript are archived locally, outside the repo.

## Layout
- Dev worktree: `C:\Users\hiheo\Claude\waggle-dev` (branch `overnight`). Live: `C:\Users\hiheo\Claude\waggle` (main).
  Both are at the same commit after each deploy.
- Tests: `cd C:\Users\hiheo\Claude\waggle-dev; python -m pytest -q` (290 pass, ~13 s, fully offline).
- Deploy a tested milestone: in the live checkout `git merge --ff-only overnight`, then `.\reload-waggle.ps1`
  (exit code 255 from that script is PowerShell piping noise; read its lines).
- Health: `python -m waggle.cli doctor`. Dashboard: http://127.0.0.1:8897.
- Rollback: `git reset --keep 0e74e97` in the live checkout + `.\reload-waggle.ps1` (pre-phase-2 main).

## State
- Phase 1 (agent stack, learning, dashboard, reliability), phase 2 (objectives, knowledge graph, continuity,
  verified skills, proactive objective watch) and mission 3 (background budget, crash-safe objectives, approval
  codes, graph quality, operations panels) are deployed. Mission 3 live runs are in PROGRESS.md.
- Rollback: `git reset --keep known-good-pre-m3` (= b69987c) in the live checkout + `.\reload-waggle.ps1`.
  Data written by mission 3 (budget.db, approvals.db, new objectives/graph columns) is ignored by the old code.
- Budget: `python -m waggle.cli budget` (status) / `budget set 6` (units per day). Units are weighted-token
  estimates (~0.06 per Haiku agent step, ~0.2 per Sonnet step); look at a few days of `waggle budget` before tuning.
- Approvals: "approve K7QH" / "deny K7QH". A bare "yes" only answers what the same chat was just asked.
- Open blockers: B-04 (voice), B-05 (calendar account), B-06 (wake-word mic).
- Chat: Sonnet answers and tool jobs, Opus only for deep+hard (config.json chat_* keys). Router prompt v2 is
  active (`python -m waggle.cli learn` history; rollback with prompts.rollback("router")). Chat benchmark:
  `python bench/chat_bench.py waggle` (results in ~/.waggle/bench). Voice: turbo STT + voice_turn fixes.

## Read this first (2026-11-20)
This file's list below is from 2026-11-19 morning. Since then Waggle was rebuilt around an always-on core
(`REBUILD_PLAN.md`, its Status section, and the last sections of `PROGRESS.md` are current). Open now:
the knowledge builder (it must filter what it learns by origin), an outreach "later" outcome, `core stop`
should pause the watchdog, the README, and the user's own checks in `USER_TESTS.md`.
Usage: on 2026-11-20 evening the plan's weekly limit stood at 94 % until 2026-11-25 06:00 PT, and Waggle's own
answers draw on the same allowance: no agent waves until it resets; small solo changes only.

## Next tasks, in order
0. **User checks on the phone**: the voice note worked (10:23); the text never reached the gateway, so get one
   typed message through, and one voice note to hear the first spoken reply (af_heart 1.05x, voice_only
   auto_tts is on). Look in traces.db (platform telegram) and gateway.log for "Auto voice reply". Latency: chat turns ~8-12 s vs ~5 s for bare Sonnet; most of it
   is longer, personalised replies, not overhead.
1. **Telegram leg**: when the user is back, have them send `objective: ...` and an `approve <code>` from Telegram
   (the plugin path is the same as the terminal one tested live, but Telegram itself wasn't driven in mission 3).
2. **Approval matching for non-shell tools**: shell commands now match on the command; Write/Edit/MCP calls still
   match on all arguments, so a re-run that changes e.g. file content is asked again. Decide per tool what
   identifies "the same action" (path for Write? never for messaging).
3. **Planner hedging**: the failure-test plan had a step "Write note or report failure" and step 1's verifier
   accepted "file missing" as done. Tell PLAN_SYSTEM that a step's done_when must be the success condition only.
4. **Budget calibration**: after 3-5 days compare budget.db background units with the real plan usage page and
   adjust `background_daily_units` / AGENT_COST defaults.
5. **Ground truth beyond files**: the disk check covers vault and absolute file paths; emails, calendar events and
   web results still rely on the Haiku check of the agent's report.
6. From the old list: objective templates, voice "started/finished" lines, objective failures -> learning loop.
