"""Replay suite: does a code change make Waggle's real answers worse?  (dev only; Inspect AI runs in its own venv)

  python tools/replay/run.py --base REF --head REF [--cases 30] [--hard 20] [--golden full|quick|none]
                             [--claude [--claude-max 10]] [--json OUT] [--timeout-min 45]

REF is a git ref in this checkout, or a folder holding a Waggle tree. The same cases run on base and on head, each in
its own throwaway home (no Telegram, nothing sent, nothing written to the owner's files), then the per-category diff
is printed and the verdict decides: head is "worse" by the margin in compare.py.

What runs, honestly:
  - replay    the owner's real recent turns (dataset.py, redacted), answered by Waggle's engine from that tree with
              Claude OFF: the context build (notes, lessons, core facts) and the small local model answer, judged by
              the fixed local judge (judge.py) against the old accepted reply. It does NOT test what Claude would
              have said, nor tool use, nor a conversation over several turns.
  - golden    the deterministic waggle.evals cases (code and local-model tiers), from this checkout's golden file.
  - --claude  also one-shot Claude answers (no tools) for the newest few cases, only while evals.claude_gate allows
              (real 5-hour window under 60 %), capped. Default off; the proposals gate never uses it.
Cost: $0. Local Ollama only.

Exit code: 0 not worse (or skipped), 2 head is worse, 3 the replay could not run.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import compare  # noqa: E402
import dataset  # noqa: E402

PREFIX = "REPLAY_RESULT "
MARK = ".replay-throwaway"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VENV_PY = Path.home() / ".waggle-tools" / "inspect" / "Scripts" / "python.exe"
CONFIG_KEEP = ("ollama_url", "local_model", "embed_model", "memory_hybrid", "memory_fact_decide", "chat_model",
               "router_model", "voice_fast_path", "voice_fast_local", "voice_fast_model", "voice_fast_cap_s",
               "gates_enabled", "gates_local", "cascade_enabled", "cascade_local", "local_first", "home_location",
               "units", "mailwatch_local_model", "search_top_k", "quiet_hours")
CONFIG_OFF = ("vault_git", "google_gog", "wolfram_mcp", "github_watch", "feedwatch_enabled", "activitywatch_enabled",
              "calls_enabled", "ccusage_enabled", "session_records_enabled", "scout_enabled", "agent_enabled",
              "review_enabled", "upcoming_note", "dev_sources", "engine_media")
KEEP_RUNS = 6


class ReplayError(Exception):
    """The replay could not run (not a verdict)."""


class ReplayTimeout(ReplayError):
    pass


def inspect_python():
    p = os.environ.get("WAGGLE_INSPECT_PY")
    p = Path(p) if p else VENV_PY
    return p if p.exists() else None


# ---- the throwaway home -------------------------------------------------------------------------------------

def _copy_db(src, dst):
    s = sqlite3.connect("file:" + str(src).replace("\\", "/") + "?mode=ro", uri=True, timeout=10)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def prepare_home(root, real_home=None, with_notes=True):
    """Make <root>/home (WAGGLE_HOME), <root>/userhome (USERPROFILE) with a settings file that has everything that
    reaches outside switched off, and a copy of the owner's notes and memory (read once, deleted with the run).
    The traces database is NOT copied: the replay must not find the answer it is being compared with."""
    root = Path(root)
    real = Path(real_home) if real_home else dataset.real_home()
    home, user = root / "home", root / "userhome"
    for d in (home, user, root / "hermes", root / "jobs", root / "work"):
        d.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads((real / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    out = {k: cfg[k] for k in CONFIG_KEEP if k in cfg}
    out.update({k: False for k in CONFIG_OFF})
    out.update(vault=str(user / "WaggleVault"), jobs_dir=str(root / "jobs"), agent_workdir=str(root / "work"),
               backup_dir=str(root / "backup"), engine_chats=[], ntfy_topic="", deadman_url="", extra_write_roots=[],
               watch_roots=[], extra_repos=[], github_repos=[])
    (home / "config.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    vault = Path(cfg.get("vault") or Path.home() / "WaggleVault")
    if with_notes:
        if vault.is_dir():
            shutil.copytree(vault, user / "WaggleVault", ignore=shutil.ignore_patterns(".git", ".obsidian"),
                            dirs_exist_ok=True)
        for name in ("memory.db", "knowledge.db", "graph.db"):
            if (real / name).exists():
                try:
                    _copy_db(real / name, home / name)
                except sqlite3.Error:
                    pass
        if (real / "lessons").is_dir():
            shutil.copytree(real / "lessons", home / "lessons", dirs_exist_ok=True)
    (user / "WaggleVault").mkdir(exist_ok=True)
    (home / MARK).write_text("throwaway home made by tools/replay/run.py; safe to delete\n", encoding="utf-8")
    return root


# ---- the code trees -----------------------------------------------------------------------------------------

class Trees:
    """Git worktrees (detached) of the two refs, removed on exit. A ref that is a folder is used in place."""

    def __init__(self, repo, base, head, root):
        self.repo, self.refs, self.root, self.made = Path(repo), {"base": base, "head": head}, Path(root), []
        self.trees = {}

    def _git(self, *args):
        p = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        if p.returncode:
            raise ReplayError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()[:200]}")
        return p.stdout.strip()

    def __enter__(self):
        for label, ref in self.refs.items():
            if (Path(ref) / "waggle" / "__init__.py").exists():  # a folder holding a Waggle tree (not a branch name that is also a folder)
                self.trees[label] = str(Path(ref).resolve())
                continue
            sha = self._git("rev-parse", "--verify", f"{ref}^{{commit}}")
            dest = self.root / f"tree-{label}"
            self._git("worktree", "add", "--detach", str(dest), sha)
            self.made.append(dest)
            self.trees[label] = str(dest)
        return self

    def __exit__(self, *exc):
        for dest in self.made:
            try:
                self._git("worktree", "remove", "--force", str(dest))
            except (ReplayError, OSError, subprocess.SubprocessError):
                pass
            shutil.rmtree(dest, ignore_errors=True)
        return False


# ---- the real runner ----------------------------------------------------------------------------------------

def read_logs(log_dir):
    """{sample id: [pass|fail|skip per epoch]} from an Inspect JSON log directory."""
    out, reasons = {}, {}
    for f in sorted(Path(log_dir).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in data.get("samples") or []:
            scores = s.get("scores") or {}
            sc = next(iter(scores.values()), None) if scores else None
            value = (sc or {}).get("value")
            st = {"C": "pass", "I": "fail"}.get(value, "skip")
            out.setdefault(str(s["id"]), []).append(st)
            if sc and sc.get("explanation"):
                reasons[str(s["id"])] = sc["explanation"]
    return out, reasons


class Runner:
    """Does the real work: Inspect in the venv for the replay, a worker process for the golden cases."""

    def __init__(self, run_dir, deadline, python=None, inspect_py=None, claude=False, log_dir=None):
        self.run_dir, self.deadline = Path(run_dir), deadline
        self.python = python or sys.executable
        self.inspect_py = inspect_py or inspect_python()
        self.claude = claude
        self.log_dir = Path(log_dir or self.run_dir / "logs")
        self._homes, self._n = {}, 0

    def home(self, label):
        if label not in self._homes:
            self._homes[label] = prepare_home(self.run_dir / f"home-{label}")
        return self._homes[label]

    def _run(self, cmd, env, stdin=None, cap=None):
        left = self.deadline - time.time()
        if left <= 0:
            raise ReplayTimeout("the replay ran out of time")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", env=env, creationflags=NO_WINDOW)
        try:
            out, err = p.communicate(stdin, timeout=min(left, cap or left))
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
            p.communicate()
            raise ReplayTimeout("a replay step timed out")
        return p.returncode, out or "", err or ""

    def _env(self, label, tree):
        root = self.home(label)
        env = {k: v for k, v in os.environ.items() if k != "INSPECT_TELEMETRY"}
        env.update(REPLAY_TREE=str(tree), WAGGLE_HOME=str(root / "home"), USERPROFILE=str(root / "userhome"),
                   HOME=str(root / "userhome"), HERMES_HOME=str(root / "hermes"), PYTHONPATH=str(tree),
                   PYTHONIOENCODING="utf-8", PYTHONUTF8="1", WAGGLE_ORIGIN="bench", NO_COLOR="1",
                   REPLAY_REAL_HOME=str(dataset.real_home()))
        return env

    def replay(self, label, tree, dataset_path, ids=None, epochs=1, claude=False):
        """{id: [status...]}, {id: reason} for the dataset's cases (or `ids`)."""
        if not self.inspect_py:
            raise ReplayError("the Inspect venv is missing")
        self._n += 1
        logs = self.log_dir / f"{label}-{self._n}"
        cmd = [str(self.inspect_py), str(HERE / "task.py"), "--dataset", str(dataset_path), "--tree", str(tree),
               "--home", str(self.home(label)), "--log-dir", str(logs), "--python", self.python,
               "--epochs", str(epochs)]
        if ids:
            cmd += ["--ids", ",".join(ids)]
        if claude:
            cmd.append("--claude")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NO_COLOR="1",
                   REPLAY_REAL_HOME=str(dataset.real_home()))
        env.pop("INSPECT_TELEMETRY", None)
        if claude:
            env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
        rc, out, err = self._run(cmd, env)
        res = read_logs(logs)
        if not res[0] and rc:
            raise ReplayError(f"inspect failed (exit {rc}): {(err or out)[-240:]}")
        return res

    def golden(self, label, tree, golden_path, ids=None, quick=False):
        """[{id, category, status, detail, ms}] from waggle.evals in the tree under test."""
        req = json.dumps({"golden": str(golden_path), "ids": list(ids or []), "quick": bool(quick)})
        rc, out, err = self._run([self.python, str(HERE / "worker.py"), "golden"], self._env(label, tree), stdin=req)
        for line in reversed(out.splitlines()):
            if line.startswith(PREFIX):
                data = json.loads(line[len(PREFIX):])
                if data.get("error"):
                    raise ReplayError(data["error"])
                return data["results"]
        raise ReplayError(f"the golden worker gave no result (exit {rc}): {(err or out)[-200:]}")


# ---- the run ------------------------------------------------------------------------------------------------

def golden_tiers(golden_path):
    data = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    return {c["id"]: c["needs"] for c in data["cases"]}


def _stat(votes):
    return compare.majority(votes)


def run_replay(base, head, *, repo=None, cases=30, hard=20, golden="full", claude=False, claude_max=10,
               timeout_min=45, runner=None, trees=None, cases_list=None, golden_path=None, out_dir=None,
               claude_gate=None, log=print):
    """The whole comparison. Returns the report dict (see summary_lines / owner_text). Never raises for a replay that
    could not run: status is then "skipped" with the reason."""
    t0 = time.time()
    out_dir = Path(out_dir) if out_dir else dataset.replay_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = out_dir / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    report = {"status": "ok", "blocked": False, "why": "", "base": str(base), "head": str(head), "stamp": stamp,
              "claude": False, "notes": [], "judge_version": None}
    try:
        import judge
        report["judge_version"] = judge.JUDGE_VERSION
        golden_path = Path(golden_path or ROOT / "waggle" / "evals_golden.json")
        case_list = cases_list if cases_list is not None else dataset.build(n_ok=cases, n_hard=hard)
        ds_path = None
        if case_list:
            ds_path = dataset.save(case_list, out_dir / "datasets", stamp)
            dataset.prune(out_dir / "datasets")
        report["dataset"] = {"cases": len(case_list), "by_category": dataset.counts(case_list)}
        if not case_list:
            report["notes"].append("no replayable turns in the owner's history yet: only the fixed cases ran")
        use_claude = False
        if claude and case_list:
            ok, why = (claude_gate or _claude_gate)()
            use_claude = ok
            if not ok:
                report["notes"].append(f"Claude answers left out: {why}")
        report["claude"] = use_claude
        deadline = t0 + timeout_min * 60
        trees = trees or Trees(repo or ROOT, base, head, run_dir)
        with trees as tr:
            runner = runner or Runner(run_dir, deadline, claude=use_claude, log_dir=out_dir / "logs" / stamp)
            tiers = golden_tiers(golden_path) if golden != "none" else {}
            res = {"base": {}, "head": {}}
            meta = {}   # result id -> (category, tier)
            votes = {"base": {}, "head": {}}

            def add(side, rid, category, tier, status):
                meta[rid] = (category, tier)
                votes[side].setdefault(rid, []).append(status)

            def replay_side(side, ids=None, epochs=1, claude_pass=False):
                got, reasons = runner.replay(side, tr.trees[side], ds_path, ids=ids, epochs=epochs, claude=claude_pass)
                for cid, sts in got.items():
                    c = by_id[cid]
                    rid = ("c:" if claude_pass else "r:") + cid
                    cat = ("claude:" if claude_pass else "replay:") + c["category"]
                    for st in sts:
                        add(side, rid, cat, "model", st)
                    if reasons.get(cid):
                        report.setdefault("reasons", {}).setdefault(side, {})[rid] = reasons[cid]

            def golden_side(side, ids=None):
                for r in runner.golden(side, tr.trees[side], golden_path, ids=ids, quick=(golden == "quick" and not ids)):
                    tier = "code" if tiers.get(r["id"]) == "code" else "model"
                    add(side, "g:" + r["id"], "golden:" + r["category"], tier, r["status"])

            by_id = {c["id"]: c for c in case_list}
            if case_list:
                for side in ("base", "head"):
                    log(f"replaying {len(case_list)} turns on {side} ...")
                    replay_side(side)
            if use_claude and case_list:
                newest = [c["id"] for c in sorted(case_list, key=lambda c: -c["time"])[:claude_max]]
                for side in ("base", "head"):
                    log(f"one-shot Claude answers for {len(newest)} turns on {side} ...")
                    replay_side(side, ids=newest, claude_pass=True)
            if golden != "none":
                for side in ("base", "head"):
                    log(f"running the fixed cases on {side} ...")
                    golden_side(side)

            def results():
                return {side: [{"id": rid, "category": meta[rid][0], "tier": meta[rid][1], "status": _stat(v)}
                               for rid, v in votes[side].items()] for side in votes}

            first = results()
            flipped = compare.flips(first["base"], first["head"])
            report["flipped_first_pass"] = flipped
            if flipped:
                log(f"{len(flipped)} cases flipped; re-running them twice on both sides ...")
                for side in ("base", "head"):
                    r_ids = [f[2:] for f in flipped if f.startswith("r:")]
                    c_ids = [f[2:] for f in flipped if f.startswith("c:")]
                    g_ids = [f[2:] for f in flipped if f.startswith("g:")]
                    if r_ids:
                        replay_side(side, ids=r_ids, epochs=2)
                    if c_ids:
                        replay_side(side, ids=c_ids, epochs=2, claude_pass=True)
                    for _ in range(2 if g_ids else 0):
                        golden_side(side, ids=g_ids)
            final = results()
        cmp_ = compare.compare(final["base"], final["head"])
        report.update(compare=cmp_, table=compare.table(cmp_), blocked=cmp_["blocked"], why="; ".join(cmp_["reasons"]),
                      paired=cmp_["paired"], margin=cmp_["margin"], lost=cmp_["lost"], gained=cmp_["gained"])
        report["pass_rate"] = {side: _rate(final[side], final["base" if side == "head" else "head"]) for side in final}
        n_turns = len(case_list)
        scored = sum(1 for x, _ in compare.pair(final["base"], final["head"]) if x["id"].startswith("r:"))
        if n_turns and scored < n_turns:
            sample = next(iter((report.get("reasons", {}).get("head") or report.get("reasons", {}).get("base") or {}).values()), "")
            report["notes"].append(f"only {scored} of {n_turns} real turns could be scored on both sides"
                                   + (f" (e.g. {sample})" if "no answer" in sample else ""))
        if cmp_["paired"] == 0:
            report.update(status="skipped", blocked=False, why="no case could be scored on both sides"
                          + (f" ({report['notes'][-1]})" if report["notes"] else ""))
    except ReplayTimeout as e:
        report.update(status="skipped", blocked=False, why=f"{e}")
    except ReplayError as e:
        report.update(status="skipped", blocked=False, why=str(e))
    except Exception as e:  # noqa: BLE001 - the gate must never take the pipeline down
        import traceback
        report.update(status="skipped", blocked=False, why=f"{type(e).__name__}: {e}"[:240],
                      trace=traceback.format_exc()[-900:])
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)  # the throwaway homes hold copies of the owner's notes
        _prune_logs(out_dir / "logs")
    report["seconds"] = int(time.time() - t0)
    report["summary"] = one_line(report)
    try:
        rp = out_dir / "reports"
        rp.mkdir(parents=True, exist_ok=True)
        (rp / f"{stamp}.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        report["report_path"] = str(rp / f"{stamp}.json")
    except OSError:
        pass
    return report


def _rate(own, other):
    """(passed, scored) over the cases both sides scored."""
    ids = {r["id"] for r in other if r["status"] != "skip"}
    rows = [r for r in own if r["id"] in ids and r["status"] != "skip"]
    return [sum(r["status"] == "pass" for r in rows), len(rows)]


def _claude_gate():
    from waggle import evals
    return evals.claude_gate()


def _prune_logs(logs):
    try:
        dirs = sorted(d for d in Path(logs).iterdir() if d.is_dir())
    except OSError:
        return
    for d in dirs[:-KEEP_RUNS]:
        shutil.rmtree(d, ignore_errors=True)


def one_line(report):
    if report["status"] != "ok":
        return f"Replay skipped: {report['why']}"
    b, h = report["pass_rate"]["base"], report["pass_rate"]["head"]
    word = "WORSE" if report["blocked"] else "not worse"
    return (f"Replay of {report['paired']} scored cases ({report['dataset']['cases']} real turns + fixed cases): base "
            f"{b[0]}/{b[1]}, head {h[0]}/{h[1]}: head is {word} (margin {report['margin']}).")


def owner_text(report):
    """What the owner is told when head is worse: numbers, the cases, where the report is."""
    c = report["compare"]
    lines = ["Replay of real answers: " + one_line(report).split(": ", 1)[-1], "Why it is not offered: " + report["why"] + "."]
    if report["lost"]:
        lines.append("Cases that got worse: " + ", ".join(report["lost"][:8]) + ".")
    if report.get("report_path"):
        lines.append("Full report: " + report["report_path"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay real turns on two code versions and print the per-category diff.")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--cases", type=int, default=30)
    ap.add_argument("--hard", type=int, default=20)
    ap.add_argument("--golden", choices=("full", "quick", "none"), default="full")
    ap.add_argument("--claude", action="store_true")
    ap.add_argument("--claude-max", type=int, default=10)
    ap.add_argument("--timeout-min", type=int, default=45)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    rep = run_replay(a.base, a.head, repo=a.repo, cases=a.cases, hard=a.hard, golden=a.golden, claude=a.claude,
                     claude_max=min(a.claude_max, 20), timeout_min=a.timeout_min)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, default=str), encoding="utf-8")
    print()
    if rep["status"] == "ok":
        print("\n".join(rep["table"]))
        print()
    print(rep["summary"])
    for n in rep["notes"]:
        print("note:", n)
    if rep["status"] == "ok" and rep["blocked"]:
        print("Why:", rep["why"])
    if rep.get("report_path"):
        print("report:", rep["report_path"])
    return 3 if rep["status"] != "ok" else (2 if rep["blocked"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
