# Feature matrix

Status: **done** = implemented + tested here · **live** = also verified in the running install ·
**partial** · **planned** · **existing** (was already in Waggle before tonight) · **blocked** (see BLOCKERS.md) ·
**n/a** (deliberately not done, reason given).
Upstream: H = Hermes Agent, OJ = OpenJarvis, W = Waggle original.

## Tools

| Capability | Up | Waggle before | Gap | Approach | Tests | Status |
|---|---|---|---|---|---|---|
| Terminal / process | H | local model only | Claude had none | Claude Code Bash + PreToolUse policy hook | test_policy | **live** |
| File read / edit / search | H | local model only | Claude had none | Claude Code Read/Edit/Write/Glob/Grep, path policy | test_policy | **live** |
| Web search / extract | H | prefetch for "live" words | Claude couldn't search on demand | Claude Code WebSearch/WebFetch in agent runs | test_agent | **live** |
| Browser automation / CDP / sessions | H | local model only | Claude had none | bridge → Hermes browser tools | test_bridge | done |
| Computer use (mouse/keyboard) | H | local + pre_tool_call guard | Claude had none | bridge → computer_use, same guard | test_bridge | **live** |
| Screen reading | W/H | desktop.py fast path | — | keep; expose `look_at_screen` to Claude | existing | existing |
| Vision | H | gemma4 aux | — | bridge → vision tool | test_bridge | done |
| Image / video generation | H | disabled (no paid provider) | needs paid key | leave off ($0 rule) | — | n/a |
| TTS / speech | H | Kokoro + Hermes voice | — | keep | existing | existing |
| Code execution | H/OJ | via terminal | — | Claude Code Bash (python) under policy | test_policy | done |
| Memory ops | H/W | vault_search, remember | Claude couldn't call them | MCP tools | test_mcp | **live** |
| Session / conversation search | H | Hermes session_search disabled | no recall of old chats | `recall` MCP tool over traces + Hermes state.db FTS | test_traces | done |
| Task management | H | jobs.py | — | MCP `jobs` tools | test_mcp | done |
| Scheduled task management | H | cron jobs installed by install.py | user can't add routines | routines.py + MCP tools + `/routine` | test_routines | **live** |
| Delegation / subagents | H/OJ | none for Claude | — | `delegate` MCP tool → named agent run | test_agents | done |
| MCP integrations | H/OJ | claude.ai connectors (email) | only email path | agent runs get connectors + waggle MCP | test_agent | done |
| Gmail / Drive / Docs | claude.ai | read + drafts (connected route) | — | keep; agent also gets them | test_connected | existing |
| Google Calendar / Tasks | claude.ai | — | not connected | auto-detected when connected | test_connected | blocked (B-01) |

## Skills

| Capability | Up | Waggle before | Gap | Approach | Tests | Status |
|---|---|---|---|---|---|---|
| Registry + metadata + categories | H/OJ | none (Hermes lists its own) | Claude path had no skills | skills.py index (SQLite) over all sources | test_skills | **live** |
| Search / discovery | H/OJ | — | — | keyword + embedding search | test_skills | **live** |
| Lazy loading | H/OJ | — | — | `skill_search` / `skill_load` MCP tools | test_skills | **live** |
| Version tracking | H/OJ | — | — | content hash + mtime per skill | test_skills | **live** |
| Install / update / remove (hub) | H | `hermes skills` exists | not reachable from Waggle | wrap hub CLI; unreviewed tier | test_skills | done |
| Security audit of scripts | H/OJ | — | — | static scan (shell, network, secrets, eval) + trust tiers | test_skills | done |
| Creation / editing | H/OJ | selfbuild (Opus) | — | learning → skill drafts; `skill_create` tool | test_learning | done |
| Usage tracking + quality score | OJ | — | — | traces per skill load/outcome | test_skills | done |
| First-party import (bundled 44 + optional 141) | H | 44 installed | optional not reachable | indexed (D-07) | test_skills | done |
| Community registries (skills.sh, GitHub, ClawHub) | H/OJ | — | — | `hermes skills search` wrapper, review before install | test_skills | done |

## Intelligence / engines

| Capability | Up | Waggle before | Gap | Approach | Tests | Status |
|---|---|---|---|---|---|---|
| Model catalogue (local + cloud) | OJ | hard-coded names | — | models.py: Ollama /api/tags + /api/show, Claude table | test_models | done |
| Capabilities (ctx, vision, tools) | OJ | — | — | from /api/show capabilities + model_info | test_models | done |
| Hardware detection | OJ | — | — | nvidia-smi + WMI | test_models | done |
| Observed latency | OJ | events latency | not per model | from traces | test_models | done |
| Task-based selection + recommendation | OJ | router fixed models | — | catalogue.pick(task) | test_models | done |
| Engine adapters (Ollama, OpenAI-compat: llama.cpp/vLLM/SGLang/LM Studio, Claude CLI) | OJ | ollama.py only | — | engines.py with one chat() interface + health + fallback chain | test_engines | done |
| Routing: trivial local / Haiku / Sonnet / Opus | W | yes | — | keep + "act" route | test_router | existing |
| Fallback on outage / quota | W/H | local fallback | not engine-aware | engines fallback chain | test_engines | done |
| Quota accounting | W | quota.py | background share not split | add per-purpose share + weekly view | test_quota | done |

## Agents / automation

| Capability | Up | Waggle before | Gap | Approach | Tests | Status |
|---|---|---|---|---|---|---|
| Tool-using reasoning agent | H/OJ | none for Claude | core gap | agent.py: Claude Code -p with tools, MCP, hook | test_agent | **live** |
| Named specialists (code, research, robotics, planning, tutor, engineering) | H/W | 4 prompt specialists | no tools/models/memory per agent | agents.py registry | test_agents | done |
| Isolated specialist memory | H | Me/Specialists notes | — | Agents/<name>/ notes folder | test_agents | done |
| Deep research | OJ | — | — | research agent: Sonnet + web, report into vault | test_agents | done |
| Background builds | W | Opus jobs | recovery after restart | jobs recovery | test_jobs | done |
| Scheduled agent routines | H/OJ | fixed crons | user-defined | routines.py + cron tick | test_routines | **live** |
| Continuous monitoring | OJ | proactive.py hourly | — | keep + monitor routines | existing | existing |
| Daily brief / weekly review / nightly project review | W/OJ | daily brief | weekly + nightly | routines defaults | test_routines | done |
| Pause / resume | H | — | — | routine state | test_routines | done |

## Memory / learning

| Capability | Up | Waggle before | Gap | Approach | Tests | Status |
|---|---|---|---|---|---|---|
| Semantic vault retrieval | W | yes | — | keep | existing | existing |
| Fact learning + dedupe | W | learn.py | — | keep | existing | existing |
| Facts vs procedures split | H/OJ | — | — | procedures go to skills, facts to vault | test_learning | done |
| Trace collection | OJ | events.jsonl (partial) | no tool steps/outcomes | traces.py SQLite | test_traces | **live** |
| Corrections + feedback capture | OJ/W | 👍/👎 on findings | chat corrections ignored | correction detector | test_traces | done |
| Analyzer (routing mistakes, failures, slow paths) | OJ | — | — | learning.analyze | test_learning | done |
| Skill discovery from tool sequences | OJ | — | — | learning.discover_skills | test_learning | done |
| Prompt optimisation (DSPy/GEPA-style), bench-gated | OJ | — | — | candidate prompts evaluated on bench + holdout, versioned | test_learning | done |
| Rollback | OJ | — | — | prompt versions store | test_learning | done |

## Interfaces / reliability / security

| Capability | Up | Waggle before | Gap | Approach | Tests | Status |
|---|---|---|---|---|---|---|
| Voice, wake word, PTT | H/W | yes | — | keep | — | existing |
| Command center dashboard | W | Obsidian HQ | no quota/skills/learning/health views | local web UI + HQ sections | test_dashboard | **live** |
| Telegram | H | yes | — | keep | — | existing |
| Secret redaction | OJ | — | logs could hold secrets | policy.redact on traces/events/audit | test_policy | done |
| Injection scanning | OJ/W | basic | — | merge OJ patterns | test_policy | done |
| Tool permission policy + approvals | H/OJ | pre_tool_call guard | Claude path unguarded | policy.py + hook + approvals | test_policy | **live** |
| Audit log | OJ | — | — | ~/.waggle/audit.jsonl | test_policy | **live** |
| Restart recovery | H | — | orphaned jobs stay "running" | jobs.recover() at gateway start | test_jobs | **live** |
| Health check | H | `hermes doctor` | Waggle parts not checked | `waggle doctor` | test_doctor | **live** |
