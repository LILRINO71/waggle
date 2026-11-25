"""Reorganise the owner's vault into PARA folders (the owner asked on 2026-11-25), reversibly.

    py -3.14 tools/vault_para.py              show the plan (nothing changes)
    py -3.14 tools/vault_para.py --apply      back up the whole vault, move, update Waggle's settings and the graph colours
    py -3.14 tools/vault_para.py --undo FILE  put everything back from the manifest the apply wrote

Layout after --apply (every note keeps its name, so [[links]] keep working):
    Personal Intelligence Dashboard.md, Personal Intelligence Profile.md     at the top (from 00 - Home)
    00 - Current Projects/                                                   (07 - Projects)
    01 - Areas/Identity, Psychology and Behavior, Goals and Ambitions, Preferences, Learning and Skills, Lifestyle
    01 - Areas/Waggle/                                                       (12 - Waggle: Waggle's own folder, with its git)
    02 - Resources/AI Assistant Vision, Knowledge, System                    (08, 10, 11)
    98 - Archive/Questionnaire Archive                                       (09)
    99 - Unsorted/                                                           (new, empty)

Stop the Waggle core first (python -m waggle.core off --for 10m), so nothing writes while folders move. After --apply
it re-approves the guard (vault settings are guarded), rebuilds the memory index and refreshes the Feed Waggle
shortcut. Backup: %USERPROFILE%\\WaggleBackups\\vault-para-<stamp> (a full copy) plus the manifest JSON there.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = Path(os.environ.get("WAGGLE_HOME") or Path.home() / ".waggle") / "config.json"

MOVES = [  # (old vault-relative path, new vault-relative path); a folder moves whole
    ("00 - Home/Personal Intelligence Dashboard.md", "Personal Intelligence Dashboard.md"),
    ("00 - Home/Personal Intelligence Profile.md", "Personal Intelligence Profile.md"),
    ("07 - Projects", "00 - Current Projects"),
    ("01 - Identity", "01 - Areas/Identity"),
    ("02 - Psychology and Behavior", "01 - Areas/Psychology and Behavior"),
    ("03 - Goals and Ambitions", "01 - Areas/Goals and Ambitions"),
    ("04 - Preferences", "01 - Areas/Preferences"),
    ("05 - Learning and Skills", "01 - Areas/Learning and Skills"),
    ("06 - Lifestyle", "01 - Areas/Lifestyle"),
    ("12 - Waggle", "01 - Areas/Waggle"),
    ("08 - AI Assistant Vision", "02 - Resources/AI Assistant Vision"),
    ("10 - Knowledge", "02 - Resources/Knowledge"),
    ("11 - System", "02 - Resources/System"),
    ("09 - Questionnaire Archive", "98 - Archive/Questionnaire Archive"),
]
NEW_EMPTY = ["99 - Unsorted"]
NEW_OWN = "01 - Areas/Waggle"
NEW_PROFILE = ["Personal Intelligence Profile.md", "01 - Areas/Identity/*", "01 - Areas/Preferences/*",
               "01 - Areas/Lifestyle/Daily Rhythm.md", "01 - Areas/Goals and Ambitions/*"]


def rgb(hex_):
    return int(hex_.lstrip("#"), 16)


GRAPH_GROUPS = [  # Obsidian graph colour groups, most specific first
    ('path:"01 - Areas/Waggle"', "#46f2a4"),
    ('path:"00 - Current Projects"', "#ff3d1f"),
    ('path:"01 - Areas"', "#38d4ff"),
    ('path:"02 - Resources"', "#ffb020"),
    ('path:"98 - Archive"', "#8a8f98"),
    ('path:"99 - Unsorted"', "#c59bff"),
]


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_config(cfg):
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG)


def plan(vault):
    rows = []
    for old, new in MOVES:
        src, dst = vault / old, vault / new
        if not src.exists():
            rows.append((old, new, "missing (skipped)"))
        elif dst.exists():
            rows.append((old, new, "target exists (skipped)"))
        else:
            rows.append((old, new, "move"))
    return rows


def core_running():
    r = subprocess.run([sys.executable, "-m", "waggle.core", "status"], cwd=str(REPO), capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "Waggle core: healthy" in (r.stdout or "")


def graph_colours(vault, manifest):
    p = vault / ".obsidian" / "graph.json"
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        g = {}
    manifest["graph_json"] = g
    g["colorGroups"] = [{"query": q, "color": {"a": 1, "rgb": rgb(c)}} for q, c in GRAPH_GROUPS]
    g.setdefault("showOrphans", True)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(g, indent=2), encoding="utf-8")


def after(note):
    """Guard, memory index, Feed Waggle shortcut: each step reported, none stops the others."""
    steps = [("guard approve", [sys.executable, "-m", "waggle.guard", "approve", "--yes"]),
             ("guard verify", [sys.executable, "-m", "waggle.guard", "verify"]),
             ("memory reindex", [sys.executable, "-c", "from waggle import memory; print(memory.reindex())"]),
             ("Feed Waggle shortcut", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                       str(REPO / "tools" / "make-drop-shortcut.ps1")])]
    for name, cmd in steps:
        r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        note(f"{name}: {'ok' if r.returncode == 0 else 'FAILED'} {(r.stdout or r.stderr).strip()[-200:]}")


def apply(vault, force=False):
    if core_running() and not force:
        print("The Waggle core is running: stop it first (python -m waggle.core off --for 10m), or pass --force.")
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path.home() / "WaggleBackups" / f"vault-para-{stamp}"
    print(f"backing up the whole vault to {backup} ...")
    shutil.copytree(vault, backup / "vault")
    cfg = load_config()
    manifest = {"vault": str(vault), "stamp": stamp, "moves": [],
                "config": {k: cfg.get(k) for k in ("vault_own", "vault_profile_notes")}}
    for old, new, what in plan(vault):
        if what != "move":
            print(f"  skip  {old}  ({what})")
            continue
        (vault / new).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(vault / old), str(vault / new))
        manifest["moves"].append([old, new])
        print(f"  moved {old}  ->  {new}")
    for d in NEW_EMPTY:
        (vault / d).mkdir(exist_ok=True)
    try:
        (vault / "00 - Home").rmdir()   # emptied above
        manifest["removed_home"] = True
    except OSError:
        pass
    graph_colours(vault, manifest)
    if any(new == NEW_OWN for _, new in manifest["moves"]):
        cfg["vault_own"] = NEW_OWN
    cfg["vault_profile_notes"] = NEW_PROFILE
    save_config(cfg)
    mpath = backup / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"settings: vault_own = {cfg['vault_own']}; profile notes updated\nmanifest: {mpath}")
    after(print)
    print(f"undo: py -3.14 tools/vault_para.py --undo \"{mpath}\"")
    return 0


def undo(mpath, force=False):
    if core_running() and not force:
        print("The Waggle core is running: stop it first, or pass --force.")
        return 2
    m = json.loads(Path(mpath).read_text(encoding="utf-8"))
    vault = Path(m["vault"])
    if m.get("removed_home"):
        (vault / "00 - Home").mkdir(exist_ok=True)
    for old, new in reversed(m["moves"]):
        if (vault / new).exists() and not (vault / old).exists():
            (vault / old).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(vault / new), str(vault / old))
            print(f"  back  {new}  ->  {old}")
    for parent in ("01 - Areas", "02 - Resources", "98 - Archive", "99 - Unsorted"):
        try:
            (vault / parent).rmdir()
        except OSError:
            pass
    if "graph_json" in m:
        (vault / ".obsidian" / "graph.json").write_text(json.dumps(m["graph_json"], indent=2), encoding="utf-8")
    cfg = load_config()
    cfg.update({k: v for k, v in m["config"].items() if v is not None})
    save_config(cfg)
    after(print)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args.undo:
        return undo(args.undo, args.force)
    vault = Path(load_config()["vault"])
    if args.apply:
        return apply(vault, args.force)
    for old, new, what in plan(vault):
        print(f"{what:<24} {old}  ->  {new}")
    print(f"new: {', '.join(NEW_EMPTY)}; vault_own -> {NEW_OWN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
