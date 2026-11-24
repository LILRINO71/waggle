"""Step 2 baseline: the 30 turns through TODAY's path (waggle.chat_session, lean `claude -p`) in a throwaway home,
and the launch settings the SDK trial reuses (prep.json), so both paths get the same model, tools, MCP servers,
policy hook and environment.

    py -3.14 tools/trials/sdk/cli_trial.py <throwaway_home> [model]
Needs `python -m waggle.guard approve --yes` with WAGGLE_HOME=<throwaway_home> first (the policy hook checks it)."""
import json
import os
import statistics
import sys
import time
from pathlib import Path

home = Path(sys.argv[1]).resolve()
model = sys.argv[2] if len(sys.argv) > 2 else "haiku"
os.environ["WAGGLE_HOME"] = str(home)
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompts import CODE_WORD, NOTES, PROMPTS, SYSTEM  # noqa: E402
from waggle import agent, chat_session, claude_cli  # noqa: E402
from waggle.config import Settings  # noqa: E402

s = Settings.load()
s.chat_model = model
s.vault = ""
s.wolfram_mcp = False  # the SDK trial gets exactly the same servers: Waggle's own only
s.windows_mcp = False
work = home / "chat_home"
work.mkdir(parents=True, exist_ok=True)
(work / "notes.txt").write_text(NOTES, encoding="utf-8")
(work / "readme.md").write_text("trial\n", encoding="utf-8")

# what the SDK trial reuses
cfg = agent.settings_json()
if not getattr(s, "chat_thinking", False):
    cfg = {**cfg, "alwaysThinkingEnabled": False}
prep = {"home": str(home), "cwd": str(work), "model": claude_cli.MODEL_IDS.get(model, model),
        "tools": chat_session.chat_tools(s), "settings": cfg, "mcp_config": chat_session.mcp_config("sdk-trial", s, "test"),
        "system_append": SYSTEM, "claude": claude_cli.find_claude(), "lean_env": chat_session.LEAN_ENV}
(home / "prep.json").write_text(json.dumps(prep, indent=1), encoding="utf-8")

sess = chat_session.ChatSession("cli-trial", settings=s)
sess.origin = "test"  # the SDK trial's hook sees origin "test" too
chat_session.system_prompt = lambda st=None: SYSTEM  # same short system text as the SDK trial
rows = []
for i, p in enumerate(PROMPTS, 1):
    if i == 16:
        sess.stop()  # the CLI path's "kill": the next turn resumes the stored session
    t0 = time.time()
    r = sess.turn(p)
    rows.append({"turn": i, "s": round(time.time() - t0, 2), "ok": r.ok, "ctx": r.context_tokens,
                 "tools": [st.get("tool") for st in r.steps if isinstance(st, dict)], "text": r.text[:120],
                 "resumed": r.resumed})
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
sess.stop(grace=3)
lat = [r["s"] for r in rows]
out = {"path": "cli", "model": model, "turns": len(rows), "ok": sum(r["ok"] for r in rows),
       "p50_s": round(statistics.median(lat), 2), "p90_s": round(sorted(lat)[int(0.9 * len(lat)) - 1], 2),
       "first_ctx": rows[0]["ctx"], "remembered_after_resume": CODE_WORD.lower() in rows[15]["text"].lower(),
       "tool_turns": sum(1 for r in rows if r["tools"])}
(home / "cli_result.json").write_text(json.dumps({"summary": out, "rows": rows}, indent=1), encoding="utf-8")
print(json.dumps(out))
