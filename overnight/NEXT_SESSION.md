# Next session: where to pick up

Read in order: MASTER_PLAN.md → DECISIONS.md → FEATURE_MATRIX.md → PROGRESS.md (last lines).

- Dev worktree: `C:\Users\hiheo\Claude\waggle-dev` (branch `overnight`). Live: `C:\Users\hiheo\Claude\waggle` (main).
- Tests: `cd C:\Users\hiheo\Claude\waggle-dev; python -m pytest -q`
- Deploy a tested milestone: in the live checkout `git merge --ff-only overnight` then `.\reload-waggle.ps1`.
- Last completed task: Stage 1 audit + docs.
- Next exact task: policy.py + Claude Code hook + bridge + waggle MCP server + agent runner.
