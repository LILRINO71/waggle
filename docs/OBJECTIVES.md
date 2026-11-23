# Night-shift objectives

The owner approves these; Waggle works only on approved objectives (decision document, section 6.2). Each has a
definition of done that code can check, limits, and how the morning report shows it. Changed only by the owner,
in the weekly planning conversation.

Night shift: 23:00-07:00, at most 35 % of the weekly Claude allowance. Overnight code work is pushed as `waggle/*`
branches with **draft** pull requests; Waggle never merges, never pushes to `main`, never sends messages to other
people.

## O1. FTC SimBench Pro: bring the three stale draft pull requests back to life

Approved 2026-11-21. Repository `LILRINO71/ftc-simbench-pro` (public; other contributors: read `AGENTS.md` first).

- **Why:** pull requests #12, #13 and #15 have been drafts since October. `main` has moved on (CI green on
  2026-11-20). Each holds real work: Onshape for school accounts and bug fixes (#12), the UI redesign (#13), a
  smoother CAD view (#15).
- **Tasks:** for each pull request, one at a time, oldest first:
  1. On a new branch `waggle/pr-<n>-refresh` cut from the pull request's branch, merge current `main` and resolve
     conflicts.
  2. Run the full test suite locally; fix only what the merge broke.
  3. Push the branch and open a draft pull request against `main` that links the original.
  4. Write a review note: what the pull request changes, what the merge changed, test results, open risks, and a
     one-line verdict (ready to merge / needs the owner / not worth it).
- **Done when (checked by code):** for each of the three: the branch exists on GitHub; GitHub Actions `tests` is
  green on it; the draft pull request exists; the review note exists. The original branches are untouched.
- **Never:** push to `main` or to the original pull-request branches; force-push; close or merge anything; change
  `AGENTS.md` or CI files.
- **Morning report:** per pull request: CI result, files changed, the verdict line, link. Owner answers accept,
  reject or redo.

## O2. Waggle itself: cut idle churn without losing a duty

Approved 2026-11-21. Repository `LILRINO71/waggle`, worked in its own worktree on a `waggle/*` branch.

- **Why:** about 980 background jobs a day run when nothing is happening (1,961 in the 48 h to 2026-11-23); the
  Step 0 target is under 300. Most are timers that poll for nothing (drop folder every 2 minutes, HQ every
  minute, routines every 5 minutes, outreach look every 5 minutes).
- **Tasks:**
  1. From `runtime.db`, list every timer with runs per day and the share of runs that did anything.
  2. For each timer that mostly does nothing, propose and implement one change: a longer interval, an event
     trigger instead of polling, or a cheap "anything to do?" check before the job is queued.
  3. Prove in a throwaway data home that every duty still works: a reminder fires on time, a mail check runs every
     5 minutes in waking hours, the brief and evening note are queued, a dropped file is picked up within
     5 minutes.
- **Done when (checked by code):** the full test suite passes; a replay of one day's timer schedule in a throwaway
  home creates under 300 jobs; the duty checks above pass; a draft pull request lists each timer's before and
  after.
- **Never:** deploy (the owner or the builder deploys after review); touch safety-protected files; change duties'
  timing in waking hours beyond the limits above.
- **Morning report:** jobs per day before and after, the duty checks, link.

## Not approved (proposals only)

Waggle may propose new objectives in the morning report. It does not start them.
