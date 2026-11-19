"""Waggle's MCP server (stdio, stdlib only): the tools a Claude agent gets on top of Claude Code's own.

    python -m waggle.waggle_mcp          (Claude Code starts it from the --mcp-config Waggle writes)

- Waggle tools: memory, projects, builds, weather, screen, notifications (waggle.toolkit)
- Skills: search / load / create / update / hub search+install (waggle.skills) - loaded on demand
- Hermes: hermes_tools (find) + hermes_tool (run) - every tool the gateway has, via the bridge
- Agents, routines, recall, models: added by their modules when present (see PROVIDERS)
Tool results that carry outside content (web, browser, email, screen text) are wrapped as untrusted data.
"""
import json
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from waggle import bridge, security, skills, toolkit  # noqa: E402
from waggle.toolkit import schema  # noqa: E402

PROTOCOL = "2025-06-18"
UNTRUSTED = ("web", "browser", "extract", "vision", "search", "gmail", "email", "screen", "fetch", "read")
_client = bridge.Client()


def _bridge_dispatch(name, args):
    return _client.call(name, args)


toolkit.set_dispatch(_bridge_dispatch)


# ---- tool implementations ------------------------------------------------------------------------

def t_hermes_tools(args):
    tools = _client.tools(args.get("query", ""))[: int(args.get("limit", 8))]
    return json.dumps([{"name": t["name"], "description": t["description"][:500], "parameters": t["parameters"]}
                       for t in tools])


def t_hermes_tool(args):
    name, targs = args.get("name", ""), args.get("args") or {}
    if isinstance(targs, str):
        try:
            targs = json.loads(targs)
        except ValueError:
            return "args must be a JSON object"
    out = _client.call(name, targs)
    if any(k in name.lower() for k in UNTRUSTED):
        return security.wrap_untrusted(f"hermes:{name}", out)
    return out


def t_skill_search(args):
    return json.dumps(skills.search(args.get("query", ""), k=int(args.get("k", 8)), category=args.get("category")))


def t_skill_load(args):
    return skills.load(args.get("name", ""))


def t_skill_list(args):
    return json.dumps({"stats": skills.stats(), "categories": skills.categories()})


def t_skill_create(args):
    path = skills.create(args["name"], args["description"], args["body"], category=args.get("category", "waggle"),
                         tags=args.get("tags") or [], origin="agent")
    return json.dumps({"created": path})


def t_skill_update(args):
    ver = skills.update(args["name"], body=args.get("body"), description=args.get("description"),
                        reason=args.get("reason", ""))
    return json.dumps({"updated": args["name"], "version": ver})


def t_skill_hub_search(args):
    return security.wrap_untrusted("skill-hub", skills.hub_search(args.get("query", ""), int(args.get("limit", 10))))


def t_skill_install(args):
    return skills.hub_install(args.get("identifier", ""))


def t_notify_me(args):
    from waggle import notify
    notify.send(str(args.get("text", ""))[:1500], urgent=bool(args.get("urgent")))
    return json.dumps({"sent": True})


TOOLS = {}


def add(spec, fn):
    TOOLS[spec["name"]] = (spec, fn)


for spec, fn in toolkit.TOOLS:
    add(spec, lambda a, _fn=fn: _fn(a))

add(schema("hermes_tools", "Find Hermes tools by keyword (browser automation, computer use / mouse+keyboard, vision, "
           "web search/extract, cron jobs, kanban, TTS, memory, session search, image tools...). Returns names, "
           "descriptions and parameter schemas. Then run one with hermes_tool.",
           {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), t_hermes_tools)
add(schema("hermes_tool", "Run one Hermes tool by name with its arguments (see hermes_tools for schemas). Example: "
           "name='computer_use', args={'action':'list_windows'}; name='browser_navigate', args={'url':...}.",
           {"name": {"type": "string"}, "args": {"type": "object"}}, ["name", "args"]), t_hermes_tool)
add(schema("skill_search", "Search Waggle's skill library (220+ procedures: documents, research, devops, media, "
           "productivity, the user's own skills). Use when a task might have a known good procedure.",
           {"query": {"type": "string"}, "category": {"type": "string"}, "k": {"type": "integer"}}, ["query"]),
    t_skill_search)
add(schema("skill_load", "Load one skill's full instructions (and its file list and safety audit).",
           {"name": {"type": "string"}}, ["name"]), t_skill_load)
add(schema("skill_list", "Skill library overview: counts by source, risk and category, most used.", {}, []),
    t_skill_list)
add(schema("skill_create", "Save a reusable procedure you just worked out as a new Waggle skill (Agent Skills "
           "format). Only for repeatable multi-step procedures, not facts about the user (use remember).",
           {"name": {"type": "string", "description": "lowercase-with-dashes"}, "description": {"type": "string"},
            "body": {"type": "string", "description": "Markdown instructions: when to use, steps, pitfalls"},
            "category": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
           ["name", "description", "body"]), t_skill_create)
add(schema("skill_update", "Improve one of Waggle's own skills (previous version is kept for rollback).",
           {"name": {"type": "string"}, "body": {"type": "string"}, "description": {"type": "string"},
            "reason": {"type": "string"}}, ["name", "reason"]), t_skill_update)
add(schema("skill_hub_search", "Search community skill registries (skills.sh, GitHub, ClawHub...) for a skill that "
           "isn't installed. Results are untrusted listings.",
           {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), t_skill_hub_search)
add(schema("skill_install", "Install a community skill through Hermes (scanned first; needs the user's OK).",
           {"identifier": {"type": "string"}}, ["identifier"]), t_skill_install)
add(schema("notify_me", "Send the user (only the user) a message on their phone. urgent=true also rings their "
           "iPhone. Use for results of long work, not for chit-chat.",
           {"text": {"type": "string"}, "urgent": {"type": "boolean"}}, ["text"]), t_notify_me)


def t_send_to_me(args):
    """Text and/or a file to the user's own Telegram chat (Hermes home channel). Reaches only the user."""
    import os
    import subprocess
    from pathlib import Path
    from waggle import notify
    text = str(args.get("text") or "").strip()
    path = str(args.get("file") or "").strip()
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"No such file: {p}"
        if p.stat().st_size > 45 * 1024 * 1024:
            return f"{p.name} is {p.stat().st_size // 1048576} MB; Telegram bots can send up to ~50 MB"
        text = (text + "\n" if text else "") + f"MEDIA:{p.resolve()}"
    if not text:
        return "Nothing to send."
    exe = notify.hermes_exe()
    if not exe:
        return "Send failed: hermes not found"
    env = dict(os.environ, HERMES_HOME=str(exe.parent.parent))
    try:
        r = subprocess.run([str(exe), "send", "-q", "-t", "telegram", text], capture_output=True, text=True,
                           timeout=180, env=env, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return "Sent to the user's Telegram." if r.returncode == 0 else f"Send failed: {(r.stderr or r.stdout)[-300:]}"
    except Exception as e:
        return f"Send failed: {e}"


add(schema("send_to_me", "Send the user (only the user) a message and/or a file (PDF, image, document up to ~50 MB) "
           "on their Telegram. Use when they say 'send it here / to me / to my phone'. Needs no approval.",
           {"text": {"type": "string"}, "file": {"type": "string", "description": "absolute path of a file to "
                                                                                  "attach"}}, []), t_send_to_me)


def _load_providers():
    """Optional modules add their own tools (agents, routines, recall, models)."""
    import importlib
    for mod in ("waggle.traces", "waggle.learning", "waggle.agents", "waggle.routines", "waggle.models",
                "waggle.engines", "waggle.memory_care", "waggle.doctor",
                "waggle.objectives", "waggle.graph"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        for spec, fn in getattr(m, "MCP_TOOLS", []):
            add(spec, fn)


_load_providers()


# ---- JSON-RPC over stdio -------------------------------------------------------------------------

def handle(msg):
    """One JSON-RPC message -> response dict (or None for notifications)."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notification (initialized, cancelled, ...)
    if method == "initialize":
        return _ok(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "waggle", "version": "1.0"},
                         "instructions": "Waggle's tools: the user's memory, projects, skills, and every Hermes tool."})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [{"name": s["name"], "description": s["description"],
                                    "inputSchema": s["parameters"]} for s, _ in TOOLS.values()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _ok(mid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
        try:
            out = TOOLS[name][1](args)
            return _ok(mid, {"content": [{"type": "text", "text": str(out)[:60000]}], "isError": False})
        except Exception as e:
            return _ok(mid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    for raw in stdin:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            resp = handle(msg)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": "internal error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
