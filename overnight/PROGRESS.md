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
