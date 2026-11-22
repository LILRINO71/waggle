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


# Hermes cron entries Waggle used to have. Every one now runs from a core timer (waggle/tick.py,
# waggle/knowledge_core.py, waggle/duties_core.py) or was only a shim for one, so install removes them.
RETIRED_CRON = {  # Hermes job name -> (script it had in HERMES_HOME/scripts, the core timers that do its work now)
    "waggle-proactive": ("waggle_proactive.py", ("outreach.look",)),  # only ran tick.main()
    "waggle-digest": ("waggle_digest.py", ("outreach.look", "knowledge.digest")),  # only ran tick.main()
    "waggle-calls": ("waggle_calls.py", ("outreach.look",)),  # only ran tick.main(); calls are one-shot core timers
    "waggle-brief": ("waggle_brief.py", ("outreach.brief",)),  # only ran tick.main()
    "waggle-ingest": ("waggle_ingest.py", ("knowledge.intake",)),
    "waggle-hq": ("waggle_hq.py", ("duty.hq",)),
    "waggle-routines": ("waggle_routines.py", ("duty.routines",)),
    "waggle-graph": ("waggle_graph.py", ("duty.graph", "knowledge.intake")),
    "waggle-learn": ("waggle_learn.py", ("duty.learn",)),
    "waggle-consolidate": ("waggle_consolidate.py", ("duty.consolidate",)),
}


def _cron_entries(jobs_file):
    """[(name, id)] of the Hermes cron jobs in jobs.json; [] when the file is missing or unreadable."""
    import json
    try:
        data = json.loads(Path(jobs_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("jobs", data) if isinstance(data, dict) else data
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    return [(j.get("name"), j.get("id")) for j in rows if isinstance(j, dict)]


def retire_cron(home=None, run=None, now=None):
    """Remove Waggle's jobs from Hermes cron (they run from core timers now), so nothing runs twice.

    Idempotent: with none of them left it does nothing and writes no backup. Before the first removal jobs.json
    is copied to cron/jobs.json.bak-cron2core-<time>. Removal goes through `hermes cron remove`, never by editing
    jobs.json while the gateway may be writing it. Returns {"removed": [...], "failed": [...], "backup": path|None}."""
    home = Path(home or HERMES_HOME)
    run = run or hermes
    jobs_file = home / "cron" / "jobs.json"
    found = [(n, i) for n, i in _cron_entries(jobs_file) if n in RETIRED_CRON]
    out = {"removed": [], "failed": [], "backup": None}
    if not found:
        return out
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    backup = jobs_file.with_name(f"jobs.json.bak-cron2core-{stamp}")
    shutil.copy2(jobs_file, backup)
    out["backup"] = str(backup)
    for name, job_id in found:
        r = run("cron", "remove", str(job_id))
        if getattr(r, "returncode", 1):
            out["failed"].append(name)
    left = {n for n, _ in _cron_entries(jobs_file)}
    out["removed"] = [n for n, _ in found if n not in left]
    out["failed"] = [n for n, _ in found if n in left]
    for name in out["removed"]:  # the script the old job ran (ours: it imports waggle)
        script = home / "scripts" / RETIRED_CRON[name][0]
        try:
            if script.exists() and "from waggle." in script.read_text(encoding="utf-8"):
                script.unlink()
        except OSError:
            pass
    return out


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
    if vault.own_rel(s):  # the owner's vault: their theme, CSS, graph and settings are never touched
        snip = obs / "snippets" / "waggle-hq.css"
        if obs.is_dir() and not snip.exists():  # only a stylesheet of ours that nothing enables (Settings > Appearance)
            snip.parent.mkdir(exist_ok=True)
            shutil.copyfile(REPO / "obsidian" / "snippets" / "waggle.css", snip)
        from waggle import hq
        hq.write(s)
        print("obsidian: the owner's vault is left as it is; HQ written into", vault.own_rel(s))
        return
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
    gone = retire_cron()  # their work runs from core timers now; each deploy re-checks, so none comes back twice
    if gone["removed"] or gone["failed"]:
        print("cron: removed", ", ".join(gone["removed"]) or "none", "| still there:", ", ".join(gone["failed"]) or "none",
              "| backup:", gone["backup"])
    install_skills()
    try:
        install_hub_skills()
    except Exception as e:  # noqa: BLE001
        print("hub skills skipped:", e)
    try:
        from waggle import skills
        print("skills:", skills.reindex())
    except Exception as e:
        print("skills index skipped:", e)


def install_skills(dest=None):
    """Copy the repo's reviewed Waggle skills into Hermes (an existing different copy is kept in .versions)."""
    dest = Path(dest or HERMES_HOME / "skills" / "waggle")
    done = []
    for src in sorted((REPO / "skills").glob("*/SKILL.md")):
        target = dest / src.parent.name / "SKILL.md"
        new = src.read_bytes()
        if target.exists():
            old = target.read_bytes()
            if old == new:
                continue
            vers = target.parent / ".versions"
            vers.mkdir(exist_ok=True)
            (vers / f"{time.strftime('%Y%m%d-%H%M%S')}-SKILL.md").write_bytes(old)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new)
        done.append(src.parent.name)
    print("waggle skills installed:", ", ".join(done) or "up to date")
    return done


# Skills fetched through Hermes's own hub (it scans each one). (identifier, category or None, folder it lands in).
# rss-feeds is Hermes's official optional skill (standard library only). The Obsidian ones are obsidianmd/obsidian-skills
# (MIT, reviewed at commit b110b2f); Waggle's registry lists hub installs as "unreviewed". blogwatcher is not here: it
# needs a separate Go program (blogwatcher-cli) and Waggle's feedwatch keeps its own seen-list instead.
HUB_SKILLS = [
    ("official/research/rss-feeds", None, "research/rss-feeds"),
    ("skills-sh/obsidianmd/obsidian-skills/skills/obsidian-bases", "note-taking", "note-taking/obsidian-bases"),
    ("skills-sh/obsidianmd/obsidian-skills/skills/json-canvas", "note-taking", "note-taking/json-canvas"),
    ("skills-sh/obsidianmd/obsidian-skills/skills/obsidian-cli", "note-taking", "note-taking/obsidian-cli"),
]


def install_hub_skills(run=None, home=None, settings=None):
    """Install the HUB_SKILLS that are not there yet; returns the identifiers it asked Hermes for. Off: config hub_skills."""
    run = run or hermes
    home = Path(home or HERMES_HOME)
    if not getattr(settings or Settings.load(), "hub_skills", True):
        return []
    done = []
    for ident, category, rel in HUB_SKILLS:
        if (home / "skills" / rel / "SKILL.md").exists():
            continue
        run("skills", "install", ident, *(["--category", category] if category else []), "--yes")
        done.append(ident)
    print("hub skills installed:", ", ".join(done) or "up to date")
    return done


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
    hermes("config", "set", "auxiliary.vision.model", s.vision())
    hermes("config", "set", "auxiliary.vision.timeout", "120")
    # Every enabled toolset is re-read by the model on every turn; drop the ones Waggle covers or never uses.
    for platform in ("cli", "telegram"):
        hermes("tools", "disable", "--platform", platform, *UNUSED_TOOLSETS)
    hermes("plugins", "enable", "waggle")


if __name__ == "__main__":
    main()
