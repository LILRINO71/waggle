# Waggle overnight transformation: master plan

Started 2026-11-18 ~20:30 local. Branch `overnight`, worktree `C:\Users\hiheo\Claude\waggle-dev`.
The live runtime keeps running from `C:\Users\hiheo\Claude\waggle` (branch `main`) and is only
switched to tested milestones (see DECISIONS.md D-02).

## What the audit found (verified, not taken from the README)

| Area | Verified state |
|---|---|
| Repo | 25 commits on main, clean, 107 tests passing, 3.9k lines in `waggle/` + plugin |
| Hermes | v0.23.2 source at `~/.hermes/hermes-agent`, gateway running (Telegram), 6 Waggle cron jobs |
| Hermes tools | Big registry (terminal, files, web, browser + CDP, computer_use/cua-driver, vision, TTS, image/video gen, cron, kanban, delegation, memory, session_search, MCP). **Only the local 4B model could call them.** Claude replies (Haiku/Sonnet through the CLI) ran with every tool stripped. |
| Hermes skills | 44 bundled installed, 141 official optional skills in the source tree, a full hub (`hermes skills search/install/audit/update`) over skills.sh, GitHub, ClawHub. Plus 27 Claude Code user skills. |
| Hermes extras | `mcp-serve` plugin (Hermes as MCP server, but terminal/file/computer-use excluded), curator (skill upkeep), journey (memory graph), fallback providers, MoA, kanban, profiles, checkpoints, `hermes insights`. |
| OpenJarvis | Cloned read-only (c3ed63f). Five primitives: intelligence (model_catalog), engine (ollama, openai-compat, vLLM, SGLang, llama.cpp, cloud, multi), agents (simple, orchestrator, native_react, deep_research, monitor_operative, proactive, morning_digest, claude_code), tools + memory (retrieval, knowledge search, storage), learning (traces -> analyzer -> skill_discovery / optimizers (DSPy, GEPA, ACE, LLM) -> eval-gated acceptance). Security: injection scanner, credential stripper, taint, file policy, skill trust tiers. |
| Hardware | RTX 5070 12 GB, 16 Ollama models incl. waggle-4b, gemma4 (vision), nomic-embed-text, qwen3:8b, 30B coders (too big for full GPU) |
| Connectors | claude.ai Gmail, Drive, Docs, Sheets, Slides connected in the Claude CLI; Calendar being connected by the user |

## The core gap

Waggle has two brains that don't share hands: Claude (smart, on the Max plan, $0 extra) answers
most messages but has **no tools**; the local model has **all Hermes tools** but is weak. The
transformation fixes that first, then adds the OpenJarvis-style learning loop on top.

## Architecture (one system, 14 interfaces)

```
 Telegram / voice / CLI ──► Hermes gateway ──► Waggle plugin (pre_llm_call)
                                                   │
                       ┌───────────── router (intelligence) ─────────────┐
                       │ trivial→local   answer→Haiku   deep→Sonnet       │
                       │ connected→Haiku+Gmail/Drive   act→Claude agent   │
                       │ build→Opus job   (fallback: engines registry)    │
                       └──────────────────────────────────────────────────┘
 Claude agent (claude -p, tools on) ──► built-in tools (files, shell, web)
        │  PreToolUse hook ──► policy.py (permissions, approvals, audit)
        └─ MCP: waggle_mcp.py ──► Waggle tools (memory, projects, skills, agents, routines)
                               └► bridge (in-gateway, 127.0.0.1 + token) ──► every Hermes tool
 traces.db ◄── every turn / tool call / outcome / feedback
 learning.py ──► analysis, skill discovery, prompt variants, bench-gated deploy + rollback
```

1. Intelligence: `models.py` catalogue (discovered, hardware-aware) + `router.py`
2. Engines: `engines.py` (Ollama, OpenAI-compatible: llama.cpp / vLLM / SGLang / LM Studio, Claude CLI)
3. Agents: `agents.py` registry + `agent.py` runner (Claude Code with tools) + routines
4. Tools: Hermes registry via `bridge.py`, Claude Code built-ins, Waggle MCP tools
5. Skills: `skills.py` registry over Hermes + Claude skills, lazy loading via MCP, audit, hub wrapper
6. Memory: `memory.py` (embeddings) + vault + conversation recall
7. Learning: `traces.py` + `learning.py` + bench
8. Projects: `projects.py`
9. Scheduling: Hermes cron + `routines.py`
10. Messaging: Hermes gateway (Telegram only, by the user's choice)
11. Voice: Hermes voice + Kokoro
12. Desktop: cua-driver via bridge, `desktop.py`, `screen.py`
13. Security: `policy.py` + `security.py`
14. Observability: events, traces, audit log, command center

## Order
Stage 1 audit (done) → bridge + agent + policy → skills → traces + learning → models/engines →
agents/routines → memory → command center → reliability → deploy → final audit.
