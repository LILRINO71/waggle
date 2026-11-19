---
name: objective-planning
description: "Choose objective, routine, delegate or answer; set the goal."
version: 1.0.1
author: Waggle
platforms: [windows, macos, linux]
metadata:
  hermes:
    category: waggle
    tags: [objective, goal, project, plan, background, long task, routine, schedule, delegate, autonomous]
    related_skills: [research-brief]
---

# Objective planning

Waggle has four ways to get work done. Pick the smallest that fits:

| Request looks like | Use |
| --- | --- |
| A question or a quick action (< 2 min) | Answer / do it now |
| One focused job for a specialist (code change, research, plan) | `delegate` |
| Multi-step work that takes a while, needs checking, or should survive a restart | `objective_create` |
| Something that should happen again on a schedule | `routine_add` |

## Writing a good objective

`objective_create` takes a goal and (optionally) a project. Waggle plans it into up to 8 steps, runs them
in the background with the right specialist agents, verifies each step, retries once, and reports.

A goal plans well when it says:
- **The deliverable**: a file path, a vault note, a list, a working script. ("Write `Research/Drive motors.md`
  comparing three motors", not "look at motors".)
- **Done criteria** the verifier can check: counts, tests passing, a file existing with certain sections.
- **Limits**: what not to touch, budget ("no purchases"), which folder or repo.
- **The project** name when it belongs to one, so progress and the report land in that project note.

## While it runs

- `objective_status` shows steps and their state. `objective_control` pauses, resumes or cancels.
- A step that needs approval waits (up to 24 h) and the user is told; the user says "yes" to continue.
- A step that fails twice blocks the objective and notifies the user with the reason; resume after fixing.

## Rules

- Never create an objective whose steps would spend money, send messages to other people, or delete user
  data; those always need the user in the loop.
- One objective per goal. Check `objective_status` before creating a duplicate.
