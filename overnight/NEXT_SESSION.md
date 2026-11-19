# Next session: where to pick up

Read in order: MASTER_PLAN.md -> DECISIONS.md -> FEATURE_MATRIX.md (bottom section = phase 2) -> PROGRESS.md (last
section) -> BLOCKERS.md.

## Layout
- Dev worktree: `C:\Users\hiheo\Claude\waggle-dev` (branch `overnight`). Live: `C:\Users\hiheo\Claude\waggle` (main).
  Both are at the same commit after the 2026-11-18 deploy.
- Tests: `cd C:\Users\hiheo\Claude\waggle-dev; python -m pytest -q` (222 pass, ~9 s, fully offline).
- Deploy a tested milestone: in the live checkout `git merge --ff-only overnight`, then `.\reload-waggle.ps1`
  (exit code 255 from that script is PowerShell piping noise; read its lines).
- Health: `python -m waggle.cli doctor`. Dashboard: http://127.0.0.1:8897.
- Rollback: `git reset --keep 0e74e97` in the live checkout + `.\reload-waggle.ps1` (pre-phase-2 main).

## State
- Phase 1 (agent stack, learning, dashboard, reliability) and phase 2 (objectives, knowledge graph, continuity,
  verified skills, proactive objective watch) are all deployed and live-verified.
- Open blockers: B-04 (try voice once), B-05 (calendar may be a different Google account).

## Next tasks, in order
1. **Objective step budget per window**: objectives currently check `quota.can_escalate` per step; add a per-day
   cap on objective runs in config (`objective_daily_runs`) so a big goal can't eat the week's Claude budget.
2. **Graph quality**: review `~/.waggle/graph.db` after a few days of chats (`python -m waggle.graph` prints
   nothing; inspect `~/.waggle/graph_last.json` and `graph.stats()`); tune EXTRACT_SYSTEM if entities are noisy.
3. **Objective templates**: common goals (weekly robot report, study plan from a syllabus, research brief) as
   presets the planner starts from.
4. **Voice for objectives**: speak a one-line "started" / "finished" through Kokoro when the voice window is open.
5. **Learning loop on objectives**: feed objective step failures into `learning.analyze` so repeated failures
   become skill drafts.
