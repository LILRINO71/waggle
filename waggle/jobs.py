"""Opus background builds: start detached, track, write results to the vault, notify, resume after limits."""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import claude_cli, events, notify, projects, quota, security, vault
from .config import Settings, data_path


def _jobs_file():
    return data_path("jobs.json")


def load():
    p = _jobs_file()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save(jobs):
    tmp = _jobs_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    os.replace(tmp, _jobs_file())


def update(job_id, **fields):
    jobs = load()
    jobs.setdefault(job_id, {}).update(fields)
    _save(jobs)
    return jobs[job_id]


def create(title, build_prompt, request, settings=None, project=None):
    s = settings or Settings.load()
    job_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{vault.slug(title, 30)}"
    workdir = Path(s.jobs_dir) / vault.slug(title, 40)
    workdir.mkdir(parents=True, exist_ok=True)
    update(job_id, id=job_id, title=title, request=request, prompt=build_prompt, workdir=str(workdir),
           status="queued", created=time.time(), model=s.builder_model, project=project)
    events.log("job", job=job_id, status="queued", project=project, title=title)
    if project:
        _project_log(project, f"Started Opus build **{title}** (job `{job_id}`)", s)
    return job_id


def _project_log(project, entry, settings=None):
    try:
        projects.add_log(project, entry, settings)
    except Exception:
        pass  # a project note problem must never break a build


REPO = str(Path(__file__).resolve().parents[1])


def _spawn(cmd, job_id):
    """Detached child that can import waggle even from Hermes's own Python."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    env = dict(os.environ, PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.Popen([sys.executable, "-m", "waggle.jobs", cmd, job_id], creationflags=flags, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def launch(job_id):
    """Run the job in its own detached Python process so Hermes stays responsive."""
    _spawn("run", job_id)


def parse_stream(log_text):
    """Pull session id, final result and error state out of stream-json output."""
    session_id, result, is_error, usage = None, "", False, {}
    for line in log_text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("session_id"):
            session_id = ev["session_id"]
        if ev.get("type") == "result":
            result = ev.get("result") or ev.get("terminal_reason") or ""
            is_error = bool(ev.get("is_error"))
            usage = ev.get("usage") or {}
    return session_id, result, is_error, usage


def run(job_id, resume=False):
    s = Settings.load()
    job = load()[job_id]
    log = Path(job["workdir"]) / f".waggle-{job_id}.log"
    prompt = job["prompt"] if not resume else "Continue where you left off and finish the task."
    update(job_id, status="running", started=time.time(), log=str(log), runner_pid=os.getpid(),
           attempts=job.get("attempts", 0) + 1)
    try:
        if resume and job.get("session_id"):
            claude = claude_cli.find_claude()
            cmd = [claude, "-p", "--resume", job["session_id"], "--model", claude_cli.MODEL_IDS[s.builder_model],
                   "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions"]
            with open(log, "a", encoding="utf-8") as fh:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT, text=True,
                                        env=claude_cli.clean_env(), cwd=job["workdir"], encoding="utf-8",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                proc.stdin.write(prompt)
                proc.stdin.close()
                proc.wait()
        else:
            proc = claude_cli.start_session(prompt, s.builder_model, job["workdir"], log,
                                            system_append=security.JOB_GUARDRAILS)
            proc.wait()
    except Exception as e:
        update(job_id, status="failed", error=str(e), finished=time.time())
        notify.send(f"Build '{job['title']}' could not start: {e}", urgent=True)
        return
    session_id, result, is_error, usage = parse_stream(log.read_text(encoding="utf-8", errors="replace"))
    # The session transcript already counts toward the Pro window; this row counts it toward Waggle's share.
    quota.record(claude_cli.MODEL_IDS.get(s.builder_model, s.builder_model), usage, f"build:{job_id}",
                 headless=False, ok=not is_error)
    if session_id:
        update(job_id, session_id=session_id)
    if is_error and "limit" in result.lower():
        quota.note_limit_hit(s, claude_cli.parse_reset_hours(result))
        update(job_id, status="paused_limit", note=result[:300])
        events.log("job", job=job_id, status="paused_limit", project=job.get("project"))
        notify.send(f"Build '{job['title']}' paused: Claude limit hit. It resumes automatically after the reset.")
        return
    status = "failed" if is_error else "done"
    update(job_id, status=status, result=result[:4000], finished=time.time())
    events.log("job", job=job_id, status=status, project=job.get("project"),
               duration_s=round(time.time() - job.get("created", time.time()), 1))
    if job.get("project"):
        first = (result.strip().splitlines() or [""])[0][:200]
        _project_log(job["project"], f"Build **{job['title']}** {status}: {first} [[Jobs/{job_id}]]", s)
    note = vault.write(f"Jobs/{job_id}", f"# {job['title']}\n\n- Status: **{status}**\n- Folder: `{job['workdir']}`\n"
                       f"- Asked: {job['request']}\n\n## Result\n\n{result}\n\n## Prompt Opus got\n\n{job['prompt']}\n")
    vault.journal(f"Build job [[Jobs/{job_id}|{job['title']}]] finished: {status}")
    notify.send(f"Build '{job['title']}' {status}. Summary in your vault: {note.name}", urgent=(status != "done"))


def resume_paused():
    """Restart limit-paused jobs once the budget allows. Called on every Waggle turn; cheap."""
    started = []
    for job_id, job in load().items():
        if job.get("status") != "paused_limit":
            continue
        ok, _ = quota.can_escalate("opus")
        if not ok:
            break
        update(job_id, status="queued")
        _spawn("resume", job_id)
        started.append(job_id)
    return started


def _alive(pid):
    """Is a process still running? (Windows-safe: os.kill(pid, 0) would send Ctrl+C there.)"""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def recover(max_attempts=2, spawn=None, alive=None):
    """After a restart or crash: jobs stuck as 'running'/'queued' whose process is gone get resumed (their
    Claude session continues) or relaunched, at most max_attempts times; then they're marked interrupted."""
    spawn, alive = spawn or _spawn, alive or _alive
    out = []
    now = time.time()
    for job_id, job in load().items():
        if job.get("status") not in ("running", "queued"):
            continue
        if alive(job.get("runner_pid")):
            continue
        if job.get("status") == "queued" and now - job.get("created", now) < 600:
            continue  # just queued; its process may still be starting
        if job.get("attempts", 0) >= max_attempts:
            update(job_id, status="interrupted", finished=now)
            notify.send(f"Build '{job.get('title')}' was interrupted twice and stopped. Say 'build status' to see it.")
            out.append((job_id, "gave up"))
            continue
        update(job_id, status="queued", recovered=now)
        spawn("resume" if job.get("session_id") else "run", job_id)
        events.log("job", job=job_id, status="recovered")
        out.append((job_id, "resumed" if job.get("session_id") else "relaunched"))
    return out


if __name__ == "__main__":
    cmd, jid = sys.argv[1], sys.argv[2]
    run(jid, resume=(cmd == "resume"))
