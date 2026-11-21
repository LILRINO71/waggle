---
name: waggle-vault-cli
description: "Drive the owner's Obsidian vaults with the Obsidian CLI."
version: 1.0.0
author: Waggle
platforms: [windows]
metadata:
  hermes:
    category: waggle
    tags: [obsidian, vault, notes, search, daily note, cli]
    related_skills: [obsidian-cli, obsidian-bases, json-canvas]
---

# Waggle's vault through the Obsidian CLI

Use when a vault task is easier through Obsidian itself than through files: search with Obsidian's index, open a
note in the app for the owner, the daily note, properties, tags, links and backlinks.

- The command is `Obsidian.com` in `%LOCALAPPDATA%\Programs\Obsidian` (on PATH as `obsidian` for new shells).
- Always name the vault first: `obsidian vault=WaggleVault <command> ...`. Without it the CLI uses whichever
  vault is open in the app, which is often PersonalIntelligenceVault (the owner's other vault). Use
  `vault=PersonalIntelligenceVault` only when the owner asks about that one.
- Obsidian must be running; if a command fails with no app, start it (`Start-Process obsidian://open?vault=WaggleVault`)
  and try once more.
- Examples: `obsidian vault=WaggleVault search query=BIOBUZZ limit=5`, `obsidian vault=WaggleVault daily`,
  `obsidian help` for the full list.
- Writing notes still follows Waggle's own rules (vault changes are git-committed and listed with
  `/waggle memory`); prefer Waggle's tools for writing and use the CLI for finding and opening.
