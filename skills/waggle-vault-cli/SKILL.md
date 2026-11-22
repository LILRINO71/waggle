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
- Waggle works in the owner's one vault, PersonalIntelligenceVault (numbered folders `00 - Home` ... `11 - System`).
  Waggle's own notes (Me, Projects, Journal, Ideas, People, Jobs, Inbox, Goals, Objectives, Routines, Sources,
  Knowledge, Archive, Drop, Waggle HQ) all live in ONE folder of it: `12 - Waggle`. Everything else is the owner's.
- Always name the vault first: `obsidian vault=PersonalIntelligenceVault <command> ...`. Without it the CLI uses
  whichever vault is open in the app.
- Obsidian must be running; if a command fails with no app, start it
  (`Start-Process obsidian://open?vault=PersonalIntelligenceVault`) and try once more.
- Examples: `obsidian vault=PersonalIntelligenceVault search query=BIOBUZZ limit=5`,
  `obsidian vault=PersonalIntelligenceVault open path="12 - Waggle/Waggle HQ.md"`, `obsidian help` for the full list.
- Read anything in the vault, but write only inside `12 - Waggle`. Never restyle or rewrite the owner's notes or their
  `.obsidian` settings; change one of their notes only when the owner asks for that note in chat.
- Writing notes still follows Waggle's own rules (changes inside `12 - Waggle` are git-committed and listed with
  `/waggle memory`); prefer Waggle's tools for writing and use the CLI for finding and opening.
