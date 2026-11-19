"""`waggle doctor`: is every part of Waggle healthy, and if not, exactly what to do.

Each check returns (name, status, detail, fix) with status ok / warn / fail. Read-only: it never changes
anything (the fixes are instructions or commands to run).
"""
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

from .config import Settings, data_path

HERMES = Path.home() / ".hermes"
MOJIBAKE = ("Ã¢", "Ã©", "â€™", "â€œ", "Ã°Å¸", "ðŸ")
NEEDED_CRON = ["waggle-proactive", "waggle-digest", "waggle-calls", "waggle-hq", "waggle-brief", "waggle-learn",
               "waggle-routines"]


def _http_ok(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def check_hermes_config():
    p = HERMES / "config.yaml"
    if not p.exists():
        return "fail", "~/.hermes/config.yaml missing", "Run Hermes setup."
    text = p.read_text(encoding="utf-8", errors="replace")
    bad = [m for m in MOJIBAKE if m in text]
    try:
        import yaml
        yaml.safe_load(text)
        parsed = True
    except ImportError:
        parsed = None
    except Exception as e:
        return "fail", f"config.yaml does not parse: {e}", "Restore the newest backup in ~/.hermes/backups."
    if bad:
        return "fail", f"config.yaml has double-encoded characters ({', '.join(bad)})", \
               "Restore ~/.hermes/config.yaml.bak-mojibake or the newest file in ~/.hermes/backups."
    return "ok", "parses cleanly" if parsed else "present (PyYAML unavailable to parse)", ""


def check_gateway():
    from . import bridge
    if bridge.Client(timeout=2).health():
        return "ok", "running; Claude agents can use Hermes tools", ""
    pid = (HERMES / "gateway.pid")
    return "fail", "gateway/bridge not reachable" + (" (stale pid file)" if pid.exists() else ""), \
        "Run reload-waggle.ps1 (or log out and back in)."


def check_ollama(s):
    try:
        with urllib.request.urlopen(f"{s.ollama_url}/api/tags", timeout=2) as r:
            names = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return "fail", "Ollama not running", "Start it: Waggle Gateway.vbs starts Ollama with the right settings."
    if not any(n.startswith(s.local_model) for n in names):
        return "fail", f"{s.local_model} missing", "Run ollama\\tune-ollama.ps1 to create it."
    env_ok = os.environ.get("OLLAMA_FLASH_ATTENTION") == "1" or _user_env("OLLAMA_FLASH_ATTENTION") == "1"
    return ("ok" if env_ok else "warn"), f"{len(names)} models; {s.local_model} present" + \
        ("" if env_ok else "; flash attention not set (64k context may spill to CPU)"), \
        "" if env_ok else "Run ollama\\tune-ollama.ps1."


def _user_env(name):
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def check_voice():
    if _http_ok("http://127.0.0.1:8899/"):
        return "ok", "Kokoro voice server warm", ""
    return "warn", "Kokoro server not running (replies speak ~3 s slower)", "Waggle Gateway.vbs starts it at login."


def check_claude():
    from . import claude_cli, quota
    exe = claude_cli.find_claude()
    if not exe:
        return "fail", "Claude Code CLI not found", "Open the Claude app once, or set CLAUDE_BIN."
    rows = [r for r in quota.ledger() if r.get("headless")][-20:]
    recent_auth = [r for r in rows if r.get("error_kind") == "auth"]
    if recent_auth and recent_auth[-1] is rows[-1]:
        return "fail", "last Claude call failed to sign in", "Run: python -m waggle.cli login, then /login."
    st = quota.status()
    if st.get("blocked_until") and st["blocked_until"] > time.time():
        return "warn", "Claude limit reached; Waggle is using the local model until the reset", ""
    return "ok", f"found; window {st['window_pct']}% used", ""


def check_cron():
    try:
        jobs = json.loads((HERMES / "cron" / "jobs.json").read_text(encoding="utf-8")).get("jobs", [])
        names = {j.get("name") for j in (jobs if isinstance(jobs, list) else jobs.values())}
    except (OSError, ValueError):
        return "fail", "can't read Hermes cron jobs", "Run reload-waggle.ps1."
    missing = [n for n in NEEDED_CRON if n not in names]
    if missing:
        return "warn", f"missing: {', '.join(missing)}", "Run reload-waggle.ps1 (it installs Waggle's cron jobs)."
    return "ok", f"{len(NEEDED_CRON)} Waggle jobs scheduled", ""


def check_vault(s):
    from . import memory_care
    if not Path(s.vault).exists():
        return "fail", f"vault missing at {s.vault}", "Run: python -m waggle.cli init"
    st = memory_care.status(s)
    detail = f"{st['notes']} notes, {st['indexed_notes']} indexed"
    if st["indexed_notes"] < st["notes"] - 3:
        return "warn", detail + " (index behind)", "Run: python -m waggle.cli reindex"
    return "ok", detail, ""


def check_skills():
    from . import skills
    st = skills.stats()
    if not st["total"]:
        return "warn", "skill index empty", "It builds itself on first use; or run python -c \"from waggle import skills; skills.reindex()\""
    high = st["by_risk"].get("high", 0)
    return "ok", f"{st['total']} skills ({high} flagged high-risk: never auto-run)", ""


def check_jobs():
    from . import jobs
    stuck = [j for j in jobs.load().values() if j.get("status") in ("running", "queued")
             and not jobs._alive(j.get("runner_pid")) and time.time() - j.get("created", 0) > 600]
    if stuck:
        return "warn", f"{len(stuck)} build(s) stuck", "They recover automatically at the next gateway start."
    return "ok", "no stuck builds", ""


def check_startup():
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    need = ["Waggle Gateway.vbs", "Waggle Voice.lnk"]
    missing = [n for n in need if not (startup / n).exists()]
    if missing:
        return "warn", f"not starting at login: {', '.join(missing)}", "Run install-startup.ps1."
    return "ok", "gateway + voice start at login", ""


def check_disk():
    free = shutil.disk_usage(Path.home()).free / 1e9
    return ("ok" if free > 10 else "warn" if free > 3 else "fail"), f"{free:.0f} GB free", \
        "" if free > 10 else "Free some space (models in the catalogue that don't fit the GPU use the most)."


def check_phone():
    from . import call
    if call.configured():
        return "ok", "Twilio calls configured", ""
    return "warn", "phone calls not set up (Telegram + iPhone push still work)", \
        "Get a Twilio number + verify your cell, then run Setup Calls.bat."


def run_all(settings=None):
    s = settings or Settings.load()
    checks = [("Hermes config", check_hermes_config), ("Gateway + bridge", check_gateway),
              ("Local AI (Ollama)", lambda: check_ollama(s)), ("Voice", check_voice), ("Claude", check_claude),
              ("Scheduled jobs", check_cron), ("Vault + memory", lambda: check_vault(s)), ("Skills", check_skills),
              ("Builds", check_jobs), ("Startup", check_startup), ("Disk", check_disk), ("Phone calls", check_phone)]
    out = []
    for name, fn in checks:
        try:
            st, detail, fix = fn()
        except Exception as e:
            st, detail, fix = "warn", f"check failed: {type(e).__name__}: {e}", ""
        out.append({"check": name, "status": st, "detail": detail, "fix": fix})
    data_path("doctor.json").write_text(json.dumps({"time": time.time(), "checks": out}, indent=1), encoding="utf-8")
    return out


def report(rows):
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"[{icon[r['status']]}] {r['check']}: {r['detail']}" + (f"\n        fix: {r['fix']}" if r["fix"] else "")
             for r in rows]
    bad = sum(r["status"] == "fail" for r in rows)
    lines.append(f"\n{len(rows) - bad}/{len(rows)} checks passing" + (f", {bad} failing" if bad else ""))
    return "\n".join(lines)


MCP_TOOLS = [
    ({"name": "waggle_doctor", "description": "Health check of every part of Waggle (Hermes config, gateway, local "
      "AI, voice, Claude, scheduled jobs, vault, skills, builds, startup, disk, phone) with fixes.",
      "parameters": {"type": "object", "properties": {}, "required": []}}, lambda a: report(run_all())),
]


def main():
    print(report(run_all()))


if __name__ == "__main__":
    main()
