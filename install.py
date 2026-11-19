"""Install Waggle into Hermes: copy the plugin, point it at this repo, set the local model, create the vault.

    python install.py            (re-run any time after pulling changes)
"""
import os
import shutil
import subprocess
import sys
import time
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
    ("waggle-hq", "every 1m", "waggle_hq.py", "", "hq"),  # keeps the Obsidian HQ dashboard live
    ("waggle-brief", "30 7 * * *", "waggle_brief.py", "", "brief"),  # proactive morning brief -> Telegram
    ("waggle-learn", "0 19 * * 0", "waggle_learn.py", "", "learning"),
    ("waggle-routines", "every 5m", "waggle_routines.py", "", "routines"),  # user-defined agent routines  # weekly: traces -> skills, prompt tuning
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


UNUSED_TOOLSETS = ["image_gen", "todo", "memory", "session_search", "delegation", "connections"]

GRAPH_GROUPS = [  # Obsidian graph colours by folder (rgb ints)
    ("path:Projects", 0xF5B301), ("path:Me", 0x3ED2F0), ("path:Jobs", 0xA78BFA),
    ("path:Ideas", 0x3DDC84), ("path:Journal", 0x5F6676), ("file:\"Waggle HQ\"", 0xFF5D5D),
]


def _merge_json(path, update):
    import json
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            data = {}
    for k, v in update.items():
        if isinstance(v, list) and isinstance(data.get(k), list) and all(isinstance(x, str) for x in v + data[k]):
            data[k] = list(dict.fromkeys(data[k] + v))  # e.g. enabled snippets: add ours, keep the user's
        else:
            data[k] = v
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def setup_obsidian(s):
    """Waggle HQ look: dark theme, the waggle.css snippet, coloured graph groups, HQ as the open note."""
    obs = Path(s.vault) / ".obsidian"
    (obs / "snippets").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / "obsidian" / "snippets" / "waggle.css", obs / "snippets" / "waggle.css")
    _merge_json(obs / "appearance.json", {"enabledCssSnippets": ["waggle"], "theme": "obsidian",
                                          "accentColor": "#f5b301"})
    _merge_json(obs / "graph.json", {"colorGroups": [{"query": q, "color": {"a": 1, "rgb": c}}
                                                     for q, c in GRAPH_GROUPS],
                                     "showArrow": True, "lineSizeMultiplier": 1.4, "nodeSizeMultiplier": 1.3})
    from waggle import hq
    hq.write(s)
    print("obsidian: theme + HQ ready")


def main():
    s = Settings.load()
    if s.router_model == "sonnet":  # 2026-11-18: Haiku is the default brain (fast; Sonnet = "deep" route)
        s.router_model = "haiku"
    s.ollama_url = s.ollama_url.replace("//localhost:", "//127.0.0.1:")  # "localhost" adds ~2 s per call
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
    setup_obsidian(s)

    if not HERMES.exists():
        print("Hermes not found at", HERMES, "- install Hermes first.")
        return
    backup = backup_config()
    configure_hermes(s)
    if not config_ok():
        if backup:
            shutil.copyfile(backup, HERMES_HOME / "config.yaml")
        print("!! Hermes config failed validation after changes; restored", backup)
        return
    soul = HERMES_HOME / "SOUL.md"
    shutil.copyfile(REPO / "persona" / "SOUL.md", soul)
    print("persona:", soul)
    if "--cron" in sys.argv:
        install_cron(s)
    try:
        from waggle import skills
        print("skills:", skills.reindex())
    except Exception as e:
        print("skills index skipped:", e)


MOJIBAKE = ("Ã¢", "Ã©", "â€™", "â€œ", "Ã°Å¸", "ðŸ")


def backup_config():
    """Copy config.yaml (bytes, so encoding/BOM/line endings are untouched) before Waggle changes anything."""
    src = HERMES_HOME / "config.yaml"
    if not src.exists():
        return None
    dst_dir = HERMES_HOME / "backups"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"config.yaml.{time.strftime('%Y%m%d-%H%M%S')}.waggle.bak"
    dst.write_bytes(src.read_bytes())
    olds = sorted(dst_dir.glob("config.yaml.*.waggle.bak"))
    for old in olds[:-10]:  # keep the last 10
        old.unlink()
    return dst


def config_ok():
    """Hermes config still parses and has no double-encoded characters."""
    p = HERMES_HOME / "config.yaml"
    text = p.read_text(encoding="utf-8", errors="replace")
    if any(m in text for m in MOJIBAKE):
        return False
    try:
        import yaml
        return isinstance(yaml.safe_load(text), dict)
    except ImportError:
        return True
    except Exception:
        return False


def configure_hermes(s):
    hermes("config", "set", "model.provider", "custom")
    hermes("config", "set", "model.base_url", f"{s.ollama_url}/v1")
    hermes("config", "set", "model.default", s.local_model)
    # Hermes refuses windows under 64k (it checks Ollama's real window); 64k fits the GPU with the 8-bit KV cache.
    hermes("config", "set", "model.context_length", "65536")
    # Compress early: a shorter prompt is a faster reply.
    hermes("config", "set", "compression.threshold_tokens", "24000")
    # Small local models fumble Hermes's tool-search meta-tool; show every tool directly.
    hermes("config", "set", "tools.tool_search.enabled", "off")
    # Hermes's image tool + Telegram photos: the local vision model (the chat model is text-only).
    hermes("config", "set", "auxiliary.vision.provider", "main")
    hermes("config", "set", "auxiliary.vision.model", s.vision_model)
    hermes("config", "set", "auxiliary.vision.timeout", "120")
    # Every enabled toolset is re-read by the model on every turn; drop the ones Waggle covers or never uses.
    for platform in ("cli", "telegram"):
        hermes("tools", "disable", "--platform", platform, *UNUSED_TOOLSETS)
    hermes("plugins", "enable", "waggle")


if __name__ == "__main__":
    main()
