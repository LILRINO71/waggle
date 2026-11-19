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
