"""The Inspect AI task of the replay suite (dev only: it runs in the venv at %USERPROFILE%\\.waggle-tools\\inspect).

  inspect eval tools/replay/task.py -T dataset=PATH -T tree=PATH -T home=PATH
  python tools/replay/task.py --dataset PATH --tree PATH --home PATH --log-dir DIR [--ids a,b] [--epochs 1] [--claude]

dataset  a JSONL file from tools/replay/dataset.py (redacted real turns)
tree     the code under test (a worktree of base or head); worker.py imports Waggle from it
home     the folder prepare_home made: home/ (WAGGLE_HOME), userhome/ (USERPROFILE) and the marker file

The solver hands each request to worker.py, which runs Waggle's engine from `tree` in the throwaway home. The scorer
is the fixed local judge (judge.py). No Inspect model is ever called: the task runs under mockllm. Telemetry stays
off (INSPECT_TELEMETRY is never set).
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from inspect_ai import Task, eval as inspect_eval, task  # noqa: E402
from inspect_ai.dataset import MemoryDataset, Sample  # noqa: E402
from inspect_ai.model import ModelOutput  # noqa: E402
from inspect_ai.scorer import CORRECT, INCORRECT, NOANSWER, Score, accuracy, scorer, stderr  # noqa: E402
from inspect_ai.solver import Generate, TaskState, solver  # noqa: E402

import judge as judge_mod  # noqa: E402

PREFIX = "REPLAY_RESULT "
ANSWER_TIMEOUT_S = 420
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def samples_from(path, ids=None):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if ids and c["id"] not in ids:
            continue
        out.append(Sample(id=c["id"], input=c["request"], target=c.get("reference") or "",
                          metadata={k: c.get(k) for k in ("category", "kind", "correction", "reference", "request",
                                                          "tools")}))
    return out


def worker_env(tree, home, claude):
    root = Path(home)
    env = {k: v for k, v in os.environ.items() if k not in ("INSPECT_TELEMETRY",)}
    env.update(REPLAY_TREE=str(tree), WAGGLE_HOME=str(root / "home"), USERPROFILE=str(root / "userhome"),
               HOME=str(root / "userhome"), HERMES_HOME=str(root / "hermes"), PYTHONPATH=str(tree),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1", WAGGLE_ORIGIN="bench")
    env.setdefault("REPLAY_REAL_HOME", str(Path(os.environ.get("REPLAY_REAL_HOME") or Path.home() / ".waggle")))
    if claude:  # the CLI keeps its sign-in in the real Claude folder: leave it there
        env.setdefault("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    return env


def run_worker(python, tree, home, claude, text, chat):
    cmd = [python, str(HERE / "worker.py"), "answer"]
    req = json.dumps({"text": text, "chat": chat, "claude": bool(claude)})
    try:
        p = subprocess.run(cmd, input=req, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=ANSWER_TIMEOUT_S, env=worker_env(tree, home, claude), creationflags=NO_WINDOW,
                           cwd=str(home))
    except subprocess.TimeoutExpired:
        return {"error": "the worker timed out"}
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith(PREFIX):
            try:
                return json.loads(line[len(PREFIX):])
            except ValueError:
                break
    return {"error": f"the worker gave no result (exit {p.returncode}): {(p.stderr or '')[-200:]}"}


@solver
def engine_solver(python, tree, home, claude=False):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        res = await asyncio.to_thread(run_worker, python, tree, home, claude, state.input_text,
                                      f"replay:{state.sample_id}")
        state.metadata["worker"] = {k: res.get(k) for k in ("answered_by", "failure", "why", "ms", "error")}
        reply = "" if res.get("error") or res.get("failure") else (res.get("reply") or "")
        state.metadata["infra"] = bool(res.get("error") or res.get("failure"))
        state.output = ModelOutput.from_content(model="replay", content=reply)
        return state
    return solve


@scorer(metrics=[accuracy(), stderr()])
def local_judge(chat=None):
    async def score(state: TaskState, target) -> Score:
        if state.metadata.get("infra"):
            why = (state.metadata.get("worker") or {}).get("error") or (state.metadata.get("worker") or {}).get("why")
            return Score(value=NOANSWER, explanation=f"no answer from the engine: {why}"[:240])
        case = {k: state.metadata.get(k) for k in ("request", "reference", "correction")}
        verdict, reason = await asyncio.to_thread(judge_mod.judge, case, state.output.completion, chat)
        if verdict is None:
            return Score(value=NOANSWER, explanation=reason)
        return Score(value=CORRECT if verdict else INCORRECT, explanation=reason,
                     metadata={"judge_version": judge_mod.JUDGE_VERSION})
    return score


@task
def replay(dataset, tree, home, python=None, claude=False, ids=None, epochs=1):
    ids = set(ids.split(",")) if isinstance(ids, str) and ids else None
    claude = str(claude).lower() in ("1", "true", "yes")
    return Task(dataset=MemoryDataset(samples_from(dataset, ids), name="owner-turns"),
                solver=engine_solver(python or sys.executable, tree, home, claude), scorer=local_judge(),
                epochs=int(epochs), metadata={"judge_version": judge_mod.JUDGE_VERSION, "tree": str(tree)})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--ids", default="")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--claude", action="store_true")
    a = ap.parse_args(argv)
    os.environ.pop("INSPECT_TELEMETRY", None)
    t = replay(dataset=a.dataset, tree=a.tree, home=a.home, python=a.python, claude=a.claude, ids=a.ids,
               epochs=a.epochs)
    logs = inspect_eval(t, model="mockllm/model", log_dir=a.log_dir, log_format="json", max_samples=1,
                        display="none", score=True, fail_on_error=False)
    status = logs[0].status if logs else "error"
    print(f"inspect status: {status}")
    return 0 if status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
