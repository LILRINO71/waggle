# Decisions

- **D-01 Keep Hermes as the runtime.** Waggle stays a Hermes plugin; Hermes keeps the gateway, voice, cron,
  tool implementations and skill hub. Waggle adds routing, Claude reasoning, memory, learning, projects.
- **D-02 Develop in a worktree, deploy tested milestones.** Cron scripts and the plugin import Waggle straight
  from the repo folder, so editing the live checkout would break the running assistant mid-edit. Work happens
  in `waggle-dev` on branch `overnight`; a milestone is deployed by fast-forwarding `main` only after the full
  suite passes, then `reload-waggle.ps1`, then a live check. Rollback = `git reset --keep <previous sha>` on
  main + reload (previous sha recorded in PROGRESS.md for each deploy).
- **D-03 User's autonomy choice (asked 2026-11-18 20:50): "Full auto in projects".** Inside
  `C:\Users\hiheo\Claude`, the vault and Waggle's own folders, file edits, scripts and installs run without
  asking. Always ask: money, passwords/secrets, messaging other people, deleting the user's data, security
  settings, anything that targets paths outside those roots for writing.
- **D-04 Messaging: Telegram only** (user's choice). Other adapters are documented, not enabled.
- **D-05 Give Claude Hermes's hands through a bridge, not mcp-serve.** The official `mcp-serve` plugin
  excludes terminal, files and computer use and runs tools in a second process. Waggle instead runs a tiny
  HTTP endpoint *inside* the gateway (127.0.0.1 only, random token in `~/.waggle/bridge.json`) that calls
  `registry.dispatch`, so Claude gets the same configured tools (cua-driver, browser, vision, cron) the gateway
  already uses, after Waggle's policy check. A stdio MCP server (`waggle_mcp.py`, stdlib only) exposes it to
  the Claude CLI.
- **D-06 Policy is enforced by a Claude Code PreToolUse hook, not by prompt text.** Deny rules in the
  prompt are advisory; the hook runs `policy.py` on every tool call and can deny it, so a prompt injection
  cannot talk its way past it. One-time approvals are stored by hash and consumed when used.
- **D-07 Skills are indexed, not all installed.** Installing all 141 optional skills into `~/.hermes/skills`
  would bloat the local model's prompt. Waggle indexes every first-party skill (bundled, optional, Claude
  user skills, Waggle-built) with source + version hash + risk audit and loads one on demand
  (`skill_search` / `skill_load` MCP tools). Third-party installs go through `hermes skills install`
  (which runs Hermes's own scanner) and are marked unreviewed until audited.
- **D-08 Learning is measured, versioned, and can't touch safety.** Prompt/routing changes are candidates
  that must match or beat the current version on both the tuning and held-out benchmark before deploy;
  every deployed version is kept for rollback. The safety sections (SOUL.md safety block, policy.py,
  security.py) are excluded from automatic optimisation and hash-checked.
- **D-09 No model downloads.** 16 local models are already present; the catalogue recommends from what is
  installed and hardware-checks before suggesting anything new.
- **D-10 Usage.** Development uses this one Claude session sequentially; Waggle's own tests mock Claude.
  Live checks make a handful of real Haiku calls only.
