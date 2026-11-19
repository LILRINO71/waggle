"""Install Waggle into Hermes: copy the plugin, point it at this repo, set the local model, create the vault.

    python install.py            (re-run any time after pulling changes)
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES = HERMES_HOME / "bin" / ("hermes.exe" if os.name == "nt" else "hermes")

sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Hermes prints emoji; cp1252 consoles choke
from waggle import vault  # noqa: E402
from waggle.config import Settings  # noqa: E402


def hermes(*args):
    env = dict(os.environ, HERMES_HOME=str(HERMES_HOME))
    r = subprocess.run([str(HERMES), *args], capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace")
    print(f"$ hermes {' '.join(args)} -> {r.returncode}")
    if r.returncode:
        print((r.stdout + r.stderr)[-800:])
    return r


SCRIPT = '''import sys
sys.path.insert(0, r"{repo}")
sys.argv = [sys.argv[0]{extra}]
from waggle.{module} import main
main()
'''

CRON_JOBS = [  # (name, schedule, script, extra argv, module)
    ("waggle-proactive", "every 1h", "waggle_proactive.py", "", "proactive"),
    ("waggle-digest", "0 8,17 * * *", "waggle_digest.py", ', "digest"', "proactive"),
    ("waggle-consolidate", "0 18 * * 0", "waggle_consolidate.py", ', "consolidate"', "proactive"),
    ("waggle-calls", "every 1m", "waggle_calls.py", ', "due"', "call"),  # places "call me in N min" calls
]


def install_cron(s):
    """Script-only Hermes cron jobs: stdout goes to the phone, empty stdout = silent."""
    scripts = HERMES_HOME / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name, schedule, script, extra, module in CRON_JOBS:
        (scripts / script).write_text(SCRIPT.format(repo=REPO, extra=extra, module=module), encoding="utf-8")
    existing = hermes("cron", "list").stdout
    for name, schedule, script, extra, module in CRON_JOBS:
        if name in existing:
            print(f"cron {name}: already there")
            continue
        hermes("cron", "create", schedule, "--name", name, "--no-agent", "--script", script,
               "--deliver", s.phone_target, "--failure-deliver", "local", "--no-wrap-response",
               "--catch-up", "off")


def main():
    s = Settings.load()
    from waggle.claude_cli import find_claude
    s.claude_bin = find_claude() or s.claude_bin
    s.save()
    print("vault:", vault.init(s))
    print("claude:", s.claude_bin or "not found")

    dest = HERMES_HOME / "plugins" / "waggle"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REPO / "hermes_plugin" / "waggle", dest,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "repo_path.txt").write_text(str(REPO), encoding="utf-8")
    print("plugin:", dest)

    if not HERMES.exists():
        print("Hermes not found at", HERMES, "- install Hermes first.")
        return
    hermes("config", "set", "model.provider", "custom")
    hermes("config", "set", "model.base_url", f"{s.ollama_url}/v1")
    hermes("config", "set", "model.default", s.local_model)
    # Hermes refuses windows under 64k, but qwen3:8b really holds 40,960 tokens: compress well before that.
    hermes("config", "set", "model.context_length", "65536")
    hermes("config", "set", "compression.threshold_tokens", "30000")
    # Small local models fumble Hermes's tool-search meta-tool; show every tool directly.
    hermes("config", "set", "tools.tool_search.enabled", "off")
    hermes("plugins", "enable", "waggle")
    soul = HERMES_HOME / "SOUL.md"
    shutil.copyfile(REPO / "persona" / "SOUL.md", soul)
    print("persona:", soul)
    if "--cron" in sys.argv:
        install_cron(s)


if __name__ == "__main__":
    main()
