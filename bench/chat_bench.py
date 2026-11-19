"""Chat benchmark: the same conversation through Waggle (the real gateway) and straight to Claude models.

    python bench/chat_bench.py waggle            # real end-to-end: hermes chat -q / --resume (plugin, router, agents)
    python bench/chat_bench.py direct sonnet     # the bare model, no Waggle context (follow-ups get the history)
    python bench/chat_bench.py direct opus --only reasoning,technical

Results (full replies, routes, latency) go to ~/.waggle/bench/, not the repo: answers can contain personal notes.
Read the replies; the numbers are only latency and route, not a quality score.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waggle import claude_cli  # noqa: E402
from waggle.config import data_path  # noqa: E402

CASES = json.loads((Path(__file__).with_name("chat_cases.json")).read_text(encoding="utf-8"))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HERMES = Path.home() / ".hermes" / "bin" / "hermes.exe"


def hermes_turn(text, session=None):
    cmd = [str(HERMES), "chat", "-Q", "-q", text] + (["--resume", session] if session else [])
    env = dict(os.environ, HERMES_HOME=str(HERMES.parent.parent))
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=600)
    dt = time.time() - t0
    out = ANSI.sub("", p.stdout + p.stderr)
    m = re.search(r"Session:\s+(\S+)", out) or re.search(r"--resume (\S+)", out)
    body = out
    if "☤ Hermes" in out:
        body = out.split("☤ Hermes", 1)[1]
        body = re.split(r"\n─{10,}", body, 1)[0]
    return body.strip(), (m.group(1) if m else session), dt


def last_trace():
    con = sqlite3.connect(data_path("traces.db"))
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT route, model, agent, latency_s FROM traces ORDER BY time DESC LIMIT 1").fetchone()
    con.close()
    return dict(r) if r else {}


def run_waggle(cases):
    out, session = [], None
    for c in cases:
        if not c.get("follow_up"):
            session = None
        reply, session, dt = hermes_turn(c["prompt"], session)
        out.append(dict(c, reply=reply, wall_s=round(dt, 1), trace=last_trace()))
        print(f"{c['id']:<14} {dt:6.1f}s  {out[-1]['trace'].get('route')}/{out[-1]['trace'].get('model')}")
    return out


def run_direct(cases, model):
    out, history = [], []
    for c in cases:
        if not c.get("follow_up"):
            history = []
        prompt = "\n".join(f"{who}: {t}" for who, t in history) + (f"\nUser: {c['prompt']}" if history else c["prompt"])
        t0 = time.time()
        try:
            reply = claude_cli.ask(prompt, model=model, purpose="bench", effort="low")["text"].strip()
        except claude_cli.ClaudeError as e:
            reply = f"ERROR {e}"
        dt = time.time() - t0
        history += [("User", c["prompt"]), ("Assistant", reply[:2500])]
        out.append(dict(c, reply=reply, wall_s=round(dt, 1), trace={"route": "direct", "model": model}))
        print(f"{c['id']:<14} {dt:6.1f}s  direct/{model}")
    return out


def main():
    args = sys.argv[1:]
    only = None
    if "--only" in args:
        only = set(args[args.index("--only") + 1].split(","))
    cases = [c for c in CASES if not only or c["kind"] in only or c["id"] in only]
    mode = args[0] if args else "waggle"
    res = run_waggle(cases) if mode == "waggle" else run_direct(cases, args[1] if len(args) > 1 else "sonnet")
    d = data_path("bench")
    d.mkdir(exist_ok=True)
    tag = mode if mode == "waggle" else f"direct-{args[1] if len(args) > 1 else 'sonnet'}"
    path = d / f"chat-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.json"
    path.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved", path)


if __name__ == "__main__":
    main()
