"""Move Waggle's own vault into the owner's vault: WaggleVault -> PersonalIntelligenceVault/12 - Waggle.

Dry run by default: prints the plan and changes nothing. `--apply` does it:

  1. copies every Waggle folder and note of the old vault into <new vault>/<own folder> (same relative layout),
     including its .git history (the history now lives inside that folder, see waggle/vault_git.py) and
     .waggle/facts.jsonl. NOT the old .obsidian (the owner's vault keeps its own theme and settings) and not .trash.
  2. never overwrites a file that already exists in the new vault: an identical file counts as done, a different
     one is a conflict, listed and left alone.
  3. sets "vault", "vault_own" and (when missing) "vault_profile_notes" in ~/.waggle/config.json, after copying the
     file to config.json.bak-vaultmove-<time>. Written with Python as UTF-8, atomically.
  4. points the "Feed Waggle" desktop shortcut at <new vault>/<own folder>/Drop.
  5. leaves the old vault exactly where it is (NOT renamed; --rename-old renames it to "<name> (moved)").

Safe to run twice: whatever is already in place is reported as done. Stop Waggle first (python -m waggle.core stop,
or tools\\deploy.ps1 does it) so nothing writes to the old vault while it is copied; afterwards run
`python -m waggle.guard approve` (the guard hashes the "vault" settings) and restart Waggle.

  python tools/move_vault.py                 dry run against the real vaults
  python tools/move_vault.py --apply         do it
  python tools/move_vault.py --report out.json   also write the plan as JSON
"""
import argparse
import datetime as dt
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SKIP_TOP = {".obsidian", ".trash"}  # never copied from the old vault
SKIP_FILES = {"waggle.lock", "index.lock"}  # lock files of a running Waggle / git
SHORTCUT = "Feed Waggle.lnk"
PROFILE_NOTES = [
    "00 - Home/Personal Intelligence Profile.md",
    "01 - Identity/*",
    "04 - Preferences/*",
    "06 - Lifestyle/Daily Rhythm.md",
    "03 - Goals and Ambitions/*",
]


def waggle_home():
    return Path(os.environ.get("WAGGLE_HOME") or Path.home() / ".waggle")


def desktop_dir():
    """The real Desktop folder (under OneDrive on many PCs)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        g = GUID(0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:  # noqa: BLE001
        pass
    return Path.home() / "Desktop"


def _ps(script, **env):
    """Run a short PowerShell script with its inputs in environment variables (no quoting problems)."""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW,
                       env={**os.environ, **env})
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def shortcut_target(lnk):
    if not lnk.exists():
        return None
    rc, out, _ = _ps("(New-Object -ComObject WScript.Shell).CreateShortcut($env:MV_LNK).TargetPath", MV_LNK=str(lnk))
    return out if rc == 0 else None


def set_shortcut(lnk, target):
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:MV_LNK);$s.TargetPath=$env:MV_TARGET;"
              "$s.WorkingDirectory=$env:MV_TARGET;$s.IconLocation=\"$env:SystemRoot\\System32\\shell32.dll,3\";"
              "$s.Description='Drop files here and Waggle files them in its vault';$s.Save()")
    lnk.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = _ps(script, MV_LNK=str(lnk), MV_TARGET=str(target))
    return rc == 0, err


def same_path(a, b):
    return os.path.normcase(os.path.normpath(str(a or ""))) == os.path.normcase(os.path.normpath(str(b or "")))


# ---- the copy plan --------------------------------------------------------------------------------------------

def plan_copy(src, dst):
    """Every file of the old vault as copy / same / conflict against the new place. Read only."""
    items = {"copy": [], "same": [], "conflict": [], "skipped": []}
    for dp, dirs, files in os.walk(src):
        here = Path(dp)
        rel_dir = here.relative_to(src)
        if rel_dir == Path("."):
            for d in list(dirs):
                if d in SKIP_TOP:
                    dirs.remove(d)
                    items["skipped"].append(d + "/ (not copied: the owner's vault keeps its own)")
        for f in files:
            rel = (rel_dir / f).as_posix()
            if f in SKIP_FILES:
                items["skipped"].append(rel)
                continue
            s, d = here / f, dst / rel
            if not d.exists():
                items["copy"].append(rel)
            elif filecmp.cmp(s, d, shallow=False):
                items["same"].append(rel)
            else:
                items["conflict"].append(rel)
    return items


def top_counts(rels):
    out = {}
    for r in rels:
        top = r.split("/", 1)[0] if "/" in r else "(root files)"
        out[top] = out.get(top, 0) + 1
    return dict(sorted(out.items()))


def do_copy(src, dst, rels):
    for rel in rels:
        s, d = src / rel, dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)  # never reached for a file that exists: plan_copy only lists the missing ones


def profile_hits(dest, patterns):
    out = {}
    for pat in patterns:
        out[pat] = len([p for p in dest.glob(pat) if p.suffix == ".md" and p.is_file()])
    return out


# ---- config ---------------------------------------------------------------------------------------------------

def plan_config(cfg_path, new_vault, own):
    data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    changes = {}
    if not same_path(data.get("vault"), new_vault):
        changes["vault"] = [data.get("vault"), str(new_vault)]
    if data.get("vault_own") != own:
        changes["vault_own"] = [data.get("vault_own"), own]
    if "vault_profile_notes" not in data:
        changes["vault_profile_notes"] = [None, PROFILE_NOTES]
    return data, changes


def write_config(cfg_path, data, changes):
    """Back the file up, then write it again with Python as UTF-8 (atomically). Returns the backup path."""
    backup = None
    if cfg_path.exists():
        backup = cfg_path.with_name(f"{cfg_path.name}.bak-vaultmove-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(cfg_path, backup)
    for k, (_, new) in changes.items():
        data[k] = new
    tmp = cfg_path.with_name(f"{cfg_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))  # it must read back
    os.replace(tmp, cfg_path)
    return backup


def config_mentions(data, old):
    """Config settings (other than the vault itself) that still name the old vault folder."""
    needle = str(old).lower().replace("\\", "/")
    hits = []
    for k, v in data.items():
        if k in ("vault", "vault_own"):
            continue
        if needle in json.dumps(v, ensure_ascii=False).lower().replace("\\\\", "/").replace("\\", "/"):
            hits.append(k)
    return hits


# ---- main -----------------------------------------------------------------------------------------------------

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="do it (the default is a dry run)")
    ap.add_argument("--src", default=str(Path.home() / "WaggleVault"), help="the old Waggle vault")
    ap.add_argument("--dest", default=str(Path.home() / "PersonalIntelligenceVault"), help="the owner's vault")
    ap.add_argument("--own", default="12 - Waggle", help="Waggle's folder inside the owner's vault")
    ap.add_argument("--config", default=str(waggle_home() / "config.json"))
    ap.add_argument("--desktop", default="", help="folder holding the Feed Waggle shortcut (default: the real Desktop)")
    ap.add_argument("--rename-old", action="store_true", help="rename the old vault to '<name> (moved)' at the end")
    ap.add_argument("--report", default="", help="also write the plan as JSON to this file")
    a = ap.parse_args(argv)

    src, new_vault, own = Path(a.src), Path(a.dest), a.own.strip("/\\")
    dst = new_vault / own
    cfg_path = Path(a.config)
    lnk = (Path(a.desktop) if a.desktop else desktop_dir()) / SHORTCUT
    new_drop = dst / "Drop"
    problems = []
    if not src.is_dir():
        problems.append(f"the old vault is not there: {src}")
    if not new_vault.is_dir():
        problems.append(f"the owner's vault is not there: {new_vault}")
    if not own or ".." in own.replace("\\", "/").split("/") or ":" in own:
        problems.append(f"bad --own: {a.own!r}")
    if problems:
        print("cannot go on:\n  " + "\n  ".join(problems))
        return 2

    items = plan_copy(src, dst)
    data, changes = plan_config(cfg_path, new_vault, own)
    old_target = shortcut_target(lnk)
    lnk_state = ("missing" if old_target is None else "already right" if same_path(old_target, new_drop) else "repoint")
    busy = [str(p) for p in (src / ".git" / "waggle.lock", src / ".git" / "index.lock") if p.exists()]
    mentions = config_mentions(data, src)
    profiles = profile_hits(new_vault, PROFILE_NOTES)

    plan = {"mode": "apply" if a.apply else "dry run", "from": str(src), "to": str(dst),
            "copy": len(items["copy"]), "copy_by_folder": top_counts(items["copy"]),
            "already_there": len(items["same"]), "conflicts": items["conflict"], "skipped": items["skipped"],
            "history_copied": any(r.startswith(".git/") for r in items["copy"]),
            "config_file": str(cfg_path), "config_changes": changes, "config_still_names_old_vault": mentions,
            "profile_notes_found": profiles, "shortcut": {"file": str(lnk), "now": old_target, "will_be": str(new_drop),
                                                         "state": lnk_state},
            "old_vault": "left in place, not renamed" if not a.rename_old else "renamed to '<name> (moved)'",
            "git_busy": busy}
    print(f"{plan['mode'].upper()}: {src}  ->  {dst}")
    print(f"  files to copy: {plan['copy']}   already there: {plan['already_there']}   conflicts: {len(items['conflict'])}"
          f"   skipped: {len(items['skipped'])}")
    for k, v in plan["copy_by_folder"].items():
        print(f"    {k}: {v}")
    for c in items["conflict"]:
        print(f"  CONFLICT (left alone): {c}")
    for sk in items["skipped"]:
        print(f"  skipped: {sk}")
    print("  config " + str(cfg_path) + ": " + (", ".join(f"{k}: {v[0]!r} -> {v[1]!r}" for k, v in changes.items()) or "already set"))
    if mentions:
        print(f"  note: these other settings still name the old vault (left as they are): {', '.join(mentions)}")
    print("  profile notes found: " + ", ".join(f"{k} = {v}" for k, v in profiles.items()))
    print(f"  shortcut {lnk}: {lnk_state}; target now {old_target!r}, will be {str(new_drop)!r}")
    print(f"  old vault: {plan['old_vault']}")
    if busy:
        print(f"  WARNING: Waggle or git is using the old history right now ({', '.join(busy)}); stop Waggle first.")
    if a.report:
        Path(a.report).write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    if not a.apply:
        print("Nothing was changed. Run again with --apply to do it.")
        return 0
    if busy:
        print("Not applying: stop Waggle first (the history is in use).")
        return 3

    do_copy(src, dst, items["copy"])
    new_drop.mkdir(parents=True, exist_ok=True)
    bad = [r for r in items["copy"] if not (dst / r).is_file() or (dst / r).stat().st_size != (src / r).stat().st_size]
    print(f"copied {len(items['copy']) - len(bad)} of {len(items['copy'])} files" + (f"; PROBLEM: {bad[:5]}" if bad else ""))
    if bad:
        return 1
    if changes:
        backup = write_config(cfg_path, data, changes)
        print(f"config written (backup: {backup})")
    if lnk_state != "already right":
        ok, err = set_shortcut(lnk, new_drop)
        print("shortcut " + ("repointed" if ok else f"NOT changed: {err[:150]}"))
    if a.rename_old:
        target = src.with_name(src.name + " (moved)")
        try:
            os.rename(src, target)
            print(f"old vault renamed to {target}")
        except OSError as e:
            print(f"old vault NOT renamed ({e}); it stays where it is")
    print("done. Next: python -m waggle.guard approve, then restart Waggle (tools\\deploy.ps1).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
