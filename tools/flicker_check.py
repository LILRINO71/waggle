"""flicker_check: runs Waggle's always-on pieces in a temp home with tools/window_probe.py watching the screen.

One run = one fresh process (the Waggle home is bound at import). Scenarios, each marked in the probe output:
  core start            pythonw -m waggle.core run with the production plug-ins
  watchdog              the watchdog started the way a scheduler does (one pass, or the long-lived loop)
  scheduled task        a temporary Task Scheduler task whose name starts "Waggle probe" (deleted afterwards)
  chat turn             one turn through chat_engine.handle_inbound in the core (Haiku, a shell tool)
  jobs                  outreach.look, chat.reap, core.retention forced once each
  in-process child      a job whose handler starts a console program with no flags (what forgotten flags do)
  detached job          a detached jobrun worker, whose handler also starts an unflagged console program

  python tools/flicker_check.py --tag before-1 --out DIR [--task-minutes 3] [--no-turn]

Never touches the real Waggle home, tasks or checkout: the home is a temp folder, the port a free one."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(REPO))
import window_probe as wp  # noqa: E402

NO_WINDOW = 0x08000000
DETACHED = 0x00000008
NEW_GROUP = 0x00000200
TASK_PREFIX = "Waggle probe"
PROMPT = ("Use your shell tool to run exactly: git --version   Then tell me, in one short sentence, what it "
          "printed.")


def pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(step)
    return None


def ps(script, timeout=60):
    """Run PowerShell quietly (the probe would otherwise count this helper's own windows)."""
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                          text=True, timeout=timeout, creationflags=NO_WINDOW)


def leftover_tasks():
    r = ps(f"(Get-ScheduledTask -TaskName '{TASK_PREFIX}*' -ErrorAction SilentlyContinue | "
           "Select-Object -ExpandProperty TaskName) -join ','")
    return [t for t in r.stdout.strip().split(",") if t]


def drop_tasks():
    ps(f"Get-ScheduledTask -TaskName '{TASK_PREFIX}*' -ErrorAction SilentlyContinue | ForEach-Object {{ "
       "Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue; "
       "Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false }")


def register_task(name, args, *, limit_minutes, restart=False):
    """One task: Once at the next minute, repeating every minute, MultipleInstances IgnoreNew; started now too."""
    restart_part = "-RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)" if restart else ""
    limit = "([TimeSpan]::Zero)" if limit_minutes == 0 else f"(New-TimeSpan -Minutes {limit_minutes})"
    script = f"""
$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute '{pythonw()}' -Argument '{args}' -WorkingDirectory '{REPO}'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Minutes 30)
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit {limit} -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries {restart_part}
$p = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
Start-ScheduledTask -TaskName '{name}'
"""
    r = ps(script)
    if r.returncode != 0:
        raise RuntimeError(f"could not register {name}: {r.stderr[:300]}")


def py_cmd(home, *argv):
    """A pythonw -c command that sets the temp home and then runs `python -m waggle.core <argv>`, since a task
    cannot carry environment variables."""
    code = (f"import os,sys;os.environ['WAGGLE_HOME']=r'{home}';os.environ['WAGGLE_CORE_PORT']=r'{os.environ['WAGGLE_CORE_PORT']}';"
            f"os.environ['WAGGLE_CORE_PLUGINS']='flicker_plugin';"
            f"sys.path[:0]=[r'{HERE}',r'{REPO}'];from waggle.core import procs;procs.install_quiet_children();"
            f"from waggle.core.__main__ import main;sys.exit(main({list(argv)!r}))")
    return f'-c "{code}"'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--task-minutes", type=float, default=3.0, help="how long the scheduled-task scenario watches")
    ap.add_argument("--no-turn", action="store_true", help="skip the real Claude chat turn")
    ap.add_argument("--no-tasks", action="store_true", help="skip the scheduled-task scenario")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    home = Path(tempfile.mkdtemp(prefix="waggle-probe-"))
    port = free_port()
    env = dict(os.environ, WAGGLE_HOME=str(home), WAGGLE_CORE_PORT=str(port), WAGGLE_CORE_PLUGINS="flicker_plugin",
               PYTHONPATH=f"{HERE}{os.pathsep}{REPO}", PYTHONUTF8="1")
    os.environ.update(env)
    (home / "vault").mkdir()
    (home / "work").mkdir()
    (home / "config.json").write_text(json.dumps({
        "vault": str(home / "vault"), "jobs_dir": str(home / "jobs"), "agent_workdir": str(home / "work"),
        "chat_model": "haiku", "chat_idle_s": 1}), encoding="utf-8")
    from waggle import guard
    had_manifest = guard.manifest_path().exists()
    ok = subprocess.run([sys.executable, "-m", "waggle.guard", "approve", "--yes"], cwd=REPO, env=env,
                        capture_output=True, text=True, creationflags=NO_WINDOW)
    if ok.returncode != 0:
        print("guard approve failed:", ok.stdout[-300:], ok.stderr[-300:])
        shutil.rmtree(home, ignore_errors=True)
        return 2

    from waggle.core import control, inbound, jobs, store  # after WAGGLE_HOME is set
    probe = wp.Probe(root_pid=os.getpid(), names=["pythonw", "python", "schtasks", "taskkill", "conhost", "cmd",
                                                  "powershell", "claude", "git", "gh", "node", "bash", "sh"],
                     cmd_contains=[str(home)], poll_ms=20, out=str(out_dir / f"{args.tag}.jsonl"),
                     park_cursor=True)
    spawned = []
    notes = {}
    try:
        probe.start()
        probe.mark("0 idle (baseline, 4 s)")
        time.sleep(4)

        # 1. the core
        probe.mark("1 core start + settle (production plug-ins)")
        core = subprocess.Popen([pythonw(), "-m", "waggle.core", "run"], cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                stdout=open(home / "core.out", "ab"), stderr=subprocess.STDOUT,
                                creationflags=DETACHED | NEW_GROUP)
        spawned.append(core.pid)
        up = wait_for(lambda: (control.heartbeat() or {}).get("pid") == core.pid, 30)
        notes["core_up"] = bool(up)
        time.sleep(10)

        # 2. the watchdog, started like a scheduler starts it
        has_loop = hasattr(control, "watch_loop")
        if has_loop:
            probe.mark("2a watchdog loop start + 20 s")
            subprocess.Popen([pythonw(), "-m", "waggle.core", "watch", "--loop"], cwd=REPO, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=DETACHED | NEW_GROUP)
            time.sleep(20)
            probe.mark("2b backstop pass x3 (watch --once)")
            for _ in range(3):
                subprocess.run([pythonw(), "-m", "waggle.core", "watch", "--once"], cwd=REPO, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=DETACHED | NEW_GROUP, timeout=60)
                time.sleep(2)
        else:
            probe.mark("2 watchdog pass x3 (pythonw -m waggle.core watch)")
            for _ in range(3):
                subprocess.run([pythonw(), "-m", "waggle.core", "watch"], cwd=REPO, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=DETACHED | NEW_GROUP, timeout=60)
                time.sleep(2)

        # 3. a real chat turn inside the core
        if not args.no_turn:
            probe.mark("3 chat turn (Haiku, shell tool)")
            sub = inbound.submit("telegram:probe", PROMPT, origin="test", platform="telegram", address="1")
            done = wait_for(lambda: (inbound.get(sub["turn_id"]) or {}).get("state") in ("done", "failed", "cancelled"),
                            150, 0.5)
            row = inbound.get(sub["turn_id"]) or {}
            notes["turn_state"] = row.get("state")
            try:
                notes["turn"] = json.loads((home / "probe_turn.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                notes["turn"] = None
            time.sleep(2)

        # 4. the periodic jobs forced once each (reap after the chat went idle, so it stops the process)
        for kind in ("chat.reap", "outreach.look", "core.retention"):
            probe.mark(f"4 job {kind}")
            time.sleep(1.5)
            jid = jobs.enqueue(kind, {})
            state = wait_for(lambda: (jobs.get(jid) or {}).get("state") in ("done", "failed", "dead"), 90, 0.3)
            notes[f"job:{kind}"] = (jobs.get(jid) or {}).get("state")
            time.sleep(2)

        # 5. a job whose handler starts an unflagged console program, in the core and in a detached worker
        probe.mark("5a in-process job + unflagged child")
        jid = jobs.enqueue("probe.inproc", {})
        wait_for(lambda: (jobs.get(jid) or {}).get("state") in ("done", "failed", "dead"), 40, 0.3)
        notes["job:probe.inproc"] = (jobs.get(jid) or {}).get("state")
        time.sleep(2)
        probe.mark("5b detached job worker + unflagged child")
        jid = jobs.enqueue("probe.detached", {})
        wait_for(lambda: (jobs.get(jid) or {}).get("state") in ("done", "failed", "dead"), 60, 0.3)
        notes["job:probe.detached"] = (jobs.get(jid) or {}).get("state")
        time.sleep(2)

        # 6. Task Scheduler's own effect
        if not args.no_tasks:
            probe.mark(f"6 scheduled task(s), {args.task_minutes:g} min")
            try:
                if has_loop:
                    # stop the manually started loop first so the task's instance is the one running
                    (home / "watchdog.stop").write_text("1")
                    time.sleep(12)
                    try:
                        (home / "watchdog.stop").unlink()
                    except OSError:
                        pass
                    register_task(f"{TASK_PREFIX} watchdog", py_cmd(home, "watch", "--loop").replace("'", "''"),
                                  limit_minutes=0, restart=True)
                    register_task(f"{TASK_PREFIX} backstop", py_cmd(home, "watch", "--once").replace("'", "''"),
                                  limit_minutes=2)
                else:
                    register_task(f"{TASK_PREFIX} watch", py_cmd(home, "watch").replace("'", "''"), limit_minutes=2)
                time.sleep(args.task_minutes * 60)
            finally:
                if has_loop:
                    (home / "watchdog.stop").write_text("1")
                drop_tasks()
            time.sleep(3)
        probe.mark("7 idle after (4 s)")
        time.sleep(4)
    finally:
        summary = probe.stop()
        try:
            control.stop(timeout_s=15)
        except Exception as e:
            notes["stop_error"] = str(e)
        (home / "watchdog.stop").write_text("1")
        time.sleep(1)
        drop_tasks()
        if not had_manifest:  # the approval was only for this temp home; leave the checkout as it was
            try:
                guard.manifest_path().unlink()
            except OSError:
                pass

    # what is left behind: processes still alive that descend from this run, and temp tasks
    table = wp.ProcTable()
    table.refresh(time.time())
    mine = [p for p in table.live if p != os.getpid() and os.getpid() in table.chain(p)]
    alive = [f"{table.info[p]['name']}:{p}" for p in mine]
    notes["leftover_processes"] = alive
    notes["mouse_reparks"] = probe.reparks
    notes["leftover_tasks"] = leftover_tasks()
    claude_spawns = {}
    for e in probe.events:
        if e["ev"] == "proc" and e.get("name") == "claude":
            claude_spawns[e["label"]] = claude_spawns.get(e["label"], 0) + 1
    notes["claude_starts"] = claude_spawns
    proc_names = {}
    for e in probe.events:
        if e["ev"] == "proc":
            key = e["label"]
            proc_names.setdefault(key, {})
            proc_names[key][e.get("name")] = proc_names[key].get(e.get("name"), 0) + 1
    notes["process_starts"] = proc_names
    result = {"tag": args.tag, "home": str(home), "summary": summary, "notes": notes,
              "events": probe.related_events()}
    (out_dir / f"{args.tag}.summary.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(wp.format_summary(summary))
    print("notes:", json.dumps({k: v for k, v in notes.items() if k != "process_starts"}, default=str)[:1500])
    for e in result["events"]:
        who = e.get("chain") or e.get("spawns")
        print(f"  [{e['label'][:28]}] t={e['t']:.1f} {e['ev']} {e.get('class') or e.get('state')} "
              f"{(e.get('title') or '')[:30]!r} by={e.get('related_by')} proc={e.get('process')} "
              f"cmd={(e.get('cmd') or '')[:90]!r} chain={(who or [])[:4]}")
    store.close_all()
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
