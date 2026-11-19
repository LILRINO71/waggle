---
name: verified-computer-use
description: "Control a desktop app (click, type, open, fill) with an observe-act-verify loop so a step only counts when the screen proves it worked."
version: 1.0.0
author: Waggle
platforms: [windows]
metadata:
  hermes:
    category: waggle
    tags: [computer use, desktop, click, type, app, window, screen, automation, verify]
    related_skills: [verified-browser-task]
---

# Verified computer use

Use for anything done inside a desktop app on the user's PC: opening a program, clicking through a dialog,
typing into a field, changing an app setting the user asked for. For web pages use `verified-browser-task`.

## The loop (every step)

1. **Observe.** `computer_use` action=`list_windows` to find the target, then action=`capture` mode=`som`
   with `app` set to that window. Read the numbered elements; do not guess coordinates from memory.
2. **Act once.** Click by `element` index (not pixels) whenever an index exists. One state-changing action
   per step. Pass `capture_after=true` so the result shows the new state.
3. **Verify.** Compare the new capture with what the step should have changed (field shows the text, dialog
   closed, toggle is on, file appears in the list). Follow the result's `verdict`:
   - confirmed: move on; never repeat confirmed input.
   - unverifiable: re-capture before doing anything else; only retry if the change is truly absent.
   - escalate: next try pixel coordinates, then `delivery_mode="foreground"` (asks for approval).
4. **Stop after 2 failed attempts at the same step.** Report what you saw (attach the `screenshot_path`)
   instead of looping.

## Rules

- `capture`, `list_windows`, `list_apps` are free. Everything else may need approval: if a call comes back
  `NEEDS_APPROVAL`, tell the user exactly what you were about to do and stop.
- Never type passwords, card numbers or one-time codes. Never solve CAPTCHAs. Never change Windows security,
  antivirus, firewall or account settings.
- Never click Send, Submit, Delete, Buy or Confirm unless the user asked for that exact action in this task.
- Before typing into a field, verify it has focus in the latest capture (the element is highlighted or the
  caret is there). Prefer `set_value` for dropdowns and sliders.
- `look_at_screen` is a cheap way to answer "what's on my screen" questions without clicking anything.

## Done means

The final capture shows the requested end state. Say what changed in one or two sentences and include the
screenshot path. If you could not verify the end state, say so plainly; a successful tool call is not proof.
