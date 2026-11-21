# Waggle architecture

One system, fourteen interfaces. Hermes keeps doing what it does well (gateway, voice, cron, tool
implementations, skill hub); Waggle adds the intelligence, personalisation, cost control, project awareness,
learning and orchestration.

| # | Interface | Module(s) | Notes |
|---|---|---|---|
| 1 | Intelligence + model selection | `router.py`, `models.py`, `prompts.py` | Haiku routes (answer / act / connected / deep / opus / local); catalogue picks models per task from real hardware |
| 2 | Inference engines | `engines.py`, `ollama.py`, `claude_cli.py` | Ollama, OpenAI-compatible (llama.cpp, vLLM, SGLang, LM Studio, hermes proxy), Claude CLI; fallback chain |
| 3 | Agent orchestration | `agent.py`, `agents.py`, `jobs.py` | Claude Code headless with tools; named specialists; delegation (depth 2); Opus builds with crash recovery |
| 4 | Tool registry | `waggle_mcp.py`, `bridge.py`, `toolkit.py` | MCP server for Claude; bridge into the live Hermes gateway's registry (`registry.dispatch`) |
| 5 | Skill registry | `skills.py` | 224 skills indexed, lazy load, audit, versions, hub search/install through Hermes |
| 6 | Memory + retrieval | `memory.py`, `vault.py`, `learn.py`, `memory_care.py`, `traces.py` | Embeddings over the vault, facts, recall of past conversations, dedupe |
| 7 | Learning + evaluation | `learning.py`, `bench/`, `prompts.py` | Analysis, skill discovery, bench-gated prompt optimisation, rollback |
| 8 | Project awareness | `projects.py`, `proactive.py` | Structured project notes, audit, watchers |
| 9 | Scheduling | Core timers (`waggle/core/timers.py`) + `routines.py` | No Waggle jobs left in Hermes cron (`install.py` removes them); user routines with recovery and quiet monitors |
| 10 | Messaging | Hermes gateway (Telegram), `notify.py` | Tiered notifications, iPhone push via ntfy, quiet hours |
| 11 | Voice + multimodal | Hermes voice, `voice/kokoro_say.py`, `voice_ack.py`, `screen.py` | Wake word, Kokoro, fillers, local vision |
| 12 | Desktop + computer control | `desktop.py`, cua-driver via bridge | App targeting, fast screen read, protected windows |
| 13 | Security + permissions | `policy.py`, `agent_hook.py`, `security.py` | PreToolUse hook, approvals, redaction, injection scan, audit |
| 14 | Observability | `dashboard.py`, `hq.py`, `doctor.py`, `events.py` | Command center, Obsidian HQ, health check |

## Why a bridge, not Hermes's `mcp-serve`

The official `mcp-serve` plugin exposes Hermes to MCP clients but deliberately excludes terminal, file and
computer-use tools and runs tools in a second process. Waggle's tool agents need computer use with the gateway's
own configuration (cua-driver approvals, vision model, browser sessions), so the plugin starts a 127.0.0.1-only
HTTP endpoint inside the gateway, authenticated with a random token stored in `~/.waggle/bridge.json`, that calls
`tools.registry.registry.dispatch`. Hermes tools that Claude Code already has better versions of (terminal,
read/write file, patch) are hidden, so all file and shell access goes through the policy hook.

## Why the policy is a hook

Rules in a system prompt are advice. A `PreToolUse` hook is a separate process that sees the exact tool call and
returns allow / deny before it runs. "Ask" cannot open a dialog in a headless run, so it becomes a one-time
approval: the call is denied with an id, Waggle tells the user what it wants to do, and "yes" grants exactly that
call (matched by a hash of the tool name and arguments, valid for 30 minutes, used once).

## Learning guard rails

- Learns only from the user's own messages, Waggle's own tool steps and the user's feedback (never from web
  pages, emails or tool output).
- Prompt changes need a measured win on the tuning set *and* no loss on the held-out set; every version is kept;
  `prompts.rollback("router")` undoes the last one.
- `policy.py`, `security.py`, the agent safety rules and `SOUL.md` are not inputs to optimisation, and route names
  are protected phrases a candidate prompt must keep.
