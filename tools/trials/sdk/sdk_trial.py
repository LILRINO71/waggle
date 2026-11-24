"""Step 2 Agent SDK trial (decision doc 1.1), run with the trial venv's Python (claude-agent-sdk pinned 0.2.188).

    pythonw sdk_trial.py run <throwaway_home>     the same 30 turns as cli_trial.py through ClaudeSDKClient, with the
                                                   same model, tools, MCP servers, Waggle policy hook and environment
                                                   (prep.json, written by cli_trial.py); the claude process is killed
                                                   after turn 15 and the session resumed. -> <home>/sdk_result.json
    pythonw sdk_trial.py hooks <throwaway_home>   in-process PreToolUse hooks that raise or hang 60 s, with the
                                                   default on_failure and with on_failure="block": does the tool run?
                                                   -> <home>/sdk_hooks.json
Uses the existing Claude login (no API key: ANTHROPIC_* and CLAUDE_* variables are removed first)."""
import asyncio
import dataclasses
import json
import os
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

for k in list(os.environ):  # same as waggle.claude_cli.clean_env (the SDK merges os.environ into the child's)
    if (k.startswith("CLAUDE_") or k.startswith("ANTHROPIC_") or k == "CLAUDECODE") and k not in ("CLAUDE_CONFIG_DIR",):
        os.environ.pop(k)

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,  # noqa: E402
                              RateLimitEvent, ResultMessage)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import CODE_WORD, PROMPTS  # noqa: E402

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ctx_tokens(usage):
    if not isinstance(usage, dict):
        return 0
    its = usage.get("iterations")
    u = its[-1] if isinstance(its, list) and its and isinstance(its[-1], dict) else usage
    return int(sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens", "output_tokens")))


def plain(x):
    if dataclasses.is_dataclass(x):
        return {k: plain(v) for k, v in dataclasses.asdict(x).items()}
    return x if isinstance(x, (str, int, float, bool, type(None), list, dict)) else str(x)


def options(prep, resume=None):
    env = dict(prep["lean_env"], WAGGLE_HOME=prep["home"], WAGGLE_SESSION="sdk-trial", WAGGLE_CHAT="sdk-trial",
               WAGGLE_AGENT="chat", WAGGLE_TURN_ORIGIN="test", PYTHONIOENCODING="utf-8")
    return ClaudeAgentOptions(
        cli_path=prep["claude"], cwd=prep["cwd"], model=prep["model"], effort="low", tools=prep["tools"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": prep["system_append"]},
        mcp_servers=prep["mcp_config"], strict_mcp_config=True, setting_sources=[],
        settings=json.dumps(prep["settings"]), env=env, resume=resume,
        extra_args={"disable-slash-commands": None}, stderr=lambda line: None)


async def one_turn(client, prompt):
    text, tools, res, limits = [], [], None, []
    await client.query(prompt)
    async for m in client.receive_response():
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if getattr(b, "text", None):
                    text.append(b.text)
                if getattr(b, "name", None) and hasattr(b, "input"):
                    tools.append(b.name)
        elif isinstance(m, RateLimitEvent):
            limits.append(plain(m))
        elif isinstance(m, ResultMessage):
            res = m
    return "".join(text), tools, res, limits


def kill_tree(client):
    pid = client._transport._process.pid  # noqa: SLF001  (the trial needs the real claude process)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)
    return pid


async def run(home):
    prep = json.loads((home / "prep.json").read_text(encoding="utf-8"))
    rows, limits, events = [], [], []
    client = ClaudeSDKClient(options(prep))
    t_conn = time.time()
    await client.connect()
    session = ""
    for i, p in enumerate(PROMPTS, 1):
        t0 = t_conn if i == 1 else time.time()
        if i == 16:
            pid = kill_tree(client)
            events.append(f"killed claude pid {pid} after turn 15")
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001  (the process is gone)
                events.append(f"disconnect after kill: {type(e).__name__}")
            t0 = time.time()
            client = ClaudeSDKClient(options(prep, resume=session))
            await client.connect()
            events.append(f"resumed session {session}")
        try:
            text, tools, res, lim = await asyncio.wait_for(one_turn(client, p), 180)
            err = ""
        except Exception as e:  # noqa: BLE001
            text, tools, res, lim, err = "", [], None, [], f"{type(e).__name__}: {e}"
        limits += lim
        if res is not None and res.session_id:
            session = res.session_id
        rows.append({"turn": i, "s": round(time.time() - t0, 2), "ok": bool(res and not res.is_error and not err),
                     "ctx": ctx_tokens(res.usage if res else None), "tools": tools, "text": text[:120],
                     "session": session, "err": err})
        (home / "sdk_progress.jsonl").open("a", encoding="utf-8").write(json.dumps(rows[-1]) + "\n")
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    lat = [r["s"] for r in rows]
    out = {"path": "sdk", "turns": len(rows), "ok": sum(r["ok"] for r in rows),
           "p50_s": round(statistics.median(lat), 2), "p90_s": round(sorted(lat)[int(0.9 * len(lat)) - 1], 2),
           "first_ctx": rows[0]["ctx"], "remembered_after_resume": CODE_WORD.lower() in rows[15]["text"].lower(),
           "same_session_after_resume": rows[14]["session"] == rows[15]["session"],
           "tool_turns": sum(1 for r in rows if r["tools"]),
           "rate_limit_types": sorted({str((e.get("rate_limit_info") or {}).get("rate_limit_type")) for e in limits}),
           "rate_limit_events": len(limits)}
    (home / "sdk_result.json").write_text(json.dumps({"summary": out, "events": events, "rows": rows,
                                                      "rate_limits": limits[:6]}, indent=1, default=str),
                                          encoding="utf-8")


async def hooks(home):
    prep = json.loads((home / "prep.json").read_text(encoding="utf-8"))
    work = home / "hooktest"
    work.mkdir(exist_ok=True)

    async def passes(inp, tid, ctx):
        return {}

    async def raises(inp, tid, ctx):
        raise RuntimeError("policy hook crashed")

    async def hangs(inp, tid, ctx):
        await asyncio.sleep(60)
        return {}

    cases = [("control-pass", passes, None), ("raise-default", raises, None), ("raise-block", raises, "block"),
             ("hang-default", hangs, None), ("hang-block", hangs, "block")]
    out = []
    for name, fn, on_failure in cases:
        marker = work / f"{name}.txt"
        marker.unlink(missing_ok=True)
        m = HookMatcher(matcher="*", hooks=[fn], timeout=10, on_failure=on_failure)
        opts = ClaudeAgentOptions(cli_path=prep["claude"], cwd=str(work), model=prep["model"], effort="low",
                                  tools=["Bash"], permission_mode="bypassPermissions", strict_mcp_config=True,
                                  setting_sources=[], hooks={"PreToolUse": [m]}, env=dict(prep["lean_env"]),
                                  system_prompt={"type": "preset", "preset": "claude_code",
                                                 "append": "Run exactly the command you are given, once."},
                                  stderr=lambda line: None)
        t0 = time.time()
        rec = {"case": name, "on_failure": on_failure}
        try:
            async with ClaudeSDKClient(opts) as c:
                text, tools, res, _ = await asyncio.wait_for(
                    one_turn(c, f"Use Bash to run exactly: echo ran > {name}.txt"), 240)
            rec.update(tools=tools, text=text[:200], is_error=bool(res and res.is_error))
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        rec.update(s=round(time.time() - t0, 1), tool_ran=marker.exists())
        out.append(rec)
        (work / "hooks_progress.jsonl").open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
    (home / "sdk_hooks.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


def hide_windows():
    """The SDK starts claude.exe with anyio.open_process and no creationflags: from pythonw (no console) Windows then
    opens a new console for it, a Windows Terminal window that takes focus (measured 2026-11-23, 2 windows and 2
    focus changes in 30 turns). --no-window passes CREATE_NO_WINDOW, as Waggle's own launches do."""
    from claude_agent_sdk._internal.transport import subprocess_cli as sc
    real = sc.anyio.open_process

    async def open_hidden(*a, **kw):
        kw.setdefault("creationflags", NOWIN)
        return await real(*a, **kw)

    sc.anyio.open_process = open_hidden


def main():
    global PROMPTS
    if "--no-window" in sys.argv:
        hide_windows()
    for a in sys.argv:
        if a.startswith("--turns="):
            PROMPTS = PROMPTS[:int(a.split("=", 1)[1])]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode, home = args[0], Path(args[1]).resolve()
    try:
        asyncio.run(run(home) if mode == "run" else hooks(home))
    except Exception:  # noqa: BLE001  (pythonw has no console: keep the error)
        (home / f"sdk_{mode}_error.txt").write_text(traceback.format_exc(), encoding="utf-8")


if __name__ == "__main__":
    main()
