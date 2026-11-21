"""Nightly local backup of Waggle's data home (~/.waggle) as a dated zip.

    python -m waggle.backup run                      # back up now
    python -m waggle.backup list                     # the backups there are
    python -m waggle.backup restore <zip> --dry-run  # check a backup and show what a restore would write
    python -m waggle.backup restore <zip> --to <dir> # unpack it somewhere (a drill, or to copy single files)
    python -m waggle.backup restore <zip> --in-place --yes   # put it back over the data home (stop Waggle first)

What goes in: every SQLite database, copied with SQLite's own backup API (so a database in the middle of a write is
still a consistent copy, and the -wal/-shm files are not needed), config, lessons, the proposals database and every
other small state file. What stays out: the Kokoro voice (a venv and models, re-creatable), saved media, screens and
voice caches, logs, lock/pid/temp files and other backups.

Where it goes: settings.backup_dir (default %USERPROFILE%\\WaggleBackups). Never a cloud-synced folder: the data
includes tokens (core.token, twilio.env), so a path inside OneDrive, Dropbox, Google Drive or iCloud is refused.
The newest settings.backup_keep (14) zips are kept; only files named like our own zips are ever deleted.

The core runs it at 03:30 (timer backup.nightly). No subprocesses, no network, no downloads.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from . import config
from .config import Settings

PREFIX = "waggle-backup-"
PRE_RESTORE_PREFIX = "waggle-prerestore-"  # a safety copy taken before an in-place restore; never rotated away
NAME_RE = re.compile(r"^waggle-backup-\d{8}-\d{6}\.zip$")
ROOT_IN_ZIP = "waggle/"
MANIFEST = "manifest.json"
KEEP_DEFAULT = 14

SKIP_DIRS = {"kokoro", "media", "screens", "voice_cache", "voice_in", "voice_out", "chat_mcp", "bench", "logs", "log",
             "cache", "caches", "__pycache__", "tmp", "temp", ".venv", "venv", "node_modules", "backups"}
SKIP_SUFFIXES = (".log", ".out", ".lock", ".pid", ".tmp", ".part", ".pyc", "-wal", "-shm", "-journal")
CLOUD_PARTS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync", "sync.com", "megasync")
SQLITE_MAGIC = b"SQLite format 3\x00"
JOB_KIND = "backup.nightly"
TIMER = "backup.nightly"
CRON = "30 3 * * *"
MAX_LATE_S = 6 * 3600  # a PC that was off at 03:30 still backs up when it starts, up to 6 h later


class BackupError(Exception):
    pass


# ---- where ---------------------------------------------------------------------------------------------------------

def backup_dir(settings=None):
    s = settings or Settings.load()
    raw = (getattr(s, "backup_dir", "") or "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else Path.home() / "WaggleBackups"


def cloud_folder(path):
    """The cloud service a path sits in (by its folder names), or None."""
    for part in Path(path).parts:
        low = part.lower()
        if any(low.startswith(c) for c in CLOUD_PARTS):
            return part
    return None


def keep_count(settings=None):
    s = settings or Settings.load()
    try:
        return max(1, int(getattr(s, "backup_keep", KEEP_DEFAULT)))
    except (TypeError, ValueError):
        return KEEP_DEFAULT


# ---- what ----------------------------------------------------------------------------------------------------------

def _skipped(rel_parts, name):
    if any(p.lower() in SKIP_DIRS for p in rel_parts[:-1]):
        return True
    low = name.lower()
    return low.endswith(SKIP_SUFFIXES) or low.startswith("~$")


def collect(home=None):
    """(files to back up, number skipped): [(absolute path, path inside the data home with forward slashes)]."""
    home = Path(home or config.HOME)
    keep, skipped = [], 0
    if not home.is_dir():
        return keep, skipped
    for root, dirs, names in os.walk(home):
        rel_root = Path(root).relative_to(home)
        kept_dirs = []
        for d in dirs:
            if d.lower() in SKIP_DIRS:
                skipped += 1
            else:
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for n in names:
            p = Path(root) / n
            rel = (rel_root / n).parts
            if _skipped(rel, n) or p.is_symlink():
                skipped += 1
                continue
            keep.append((p, "/".join(rel)))
    return keep, skipped


def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def copy_sqlite(src, dst):
    """A consistent copy of a SQLite database through the backup API. Opens the source read-only when it can, so
    nothing in the live data home is changed."""
    try:
        con = sqlite3.connect(f"{Path(src).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:  # a WAL database whose -shm cannot be opened read-only
        con = sqlite3.connect(str(src), timeout=30)
    out = sqlite3.connect(str(dst))
    try:
        con.backup(out)
    finally:
        out.close()
        con.close()


# ---- run -----------------------------------------------------------------------------------------------------------

def run(settings=None, *, now=None, home=None, dest=None):
    """Make one backup zip. Returns {"zip", "files", "databases", "bytes", "skipped", "removed"}; raises BackupError."""
    s = settings or Settings.load()
    now = time.time() if now is None else now
    home = Path(home or config.HOME)
    dest = Path(dest) if dest else backup_dir(s)
    where = cloud_folder(dest)
    if where:
        raise BackupError(f"{dest} is inside a cloud folder ({where}); the backup holds tokens, so it stays on this "
                          "PC. Set backup_dir to a local folder.")
    try:
        if home.resolve() == dest.resolve() or home.resolve() in dest.resolve().parents:
            raise BackupError("backup_dir must be outside the data home")
    except OSError:
        pass
    if not home.is_dir():
        raise BackupError(f"no data home at {home}")
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    final = dest / f"{PREFIX}{stamp}.zip"
    part = dest / f"{PREFIX}{stamp}.zip.part"
    files, skipped = collect(home)
    dbs, written, problems = [], [], []
    stage = Path(tempfile.mkdtemp(prefix="waggle-backup-"))
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for i, (path, rel) in enumerate(files):
                try:
                    if is_sqlite(path):
                        tmp = stage / f"{i}.db"
                        copy_sqlite(path, tmp)
                        z.write(tmp, ROOT_IN_ZIP + rel)
                        tmp.unlink(missing_ok=True)
                        dbs.append(rel)
                    else:
                        z.write(path, ROOT_IN_ZIP + rel)
                    written.append(rel)
                except (OSError, sqlite3.Error) as e:  # one unreadable file never ruins the night's backup
                    problems.append(f"{rel}: {type(e).__name__}")
            z.writestr(MANIFEST, json.dumps({
                "created": now, "created_iso": dt.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                "home": str(home), "files": written, "databases": dbs, "skipped": skipped, "problems": problems,
                "note": "SQLite files were copied with the backup API; see waggle/backup.py for what is left out"},
                indent=1))
        if not written:
            raise BackupError("nothing to back up")
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    removed = rotate(dest, keep_count(s))
    return {"zip": str(final), "files": len(written), "databases": len(dbs), "bytes": final.stat().st_size,
            "skipped": skipped, "problems": problems, "removed": removed}


def rotate(dest, keep):
    """Delete all but the newest `keep` of our own zips (by their dated names). Returns the names removed."""
    names = sorted((p.name for p in Path(dest).iterdir() if NAME_RE.match(p.name)), reverse=True)
    gone = []
    for n in names[keep:]:
        try:
            (Path(dest) / n).unlink()
            gone.append(n)
        except OSError:
            pass
    return gone


# ---- list ----------------------------------------------------------------------------------------------------------

def backups(settings=None, dest=None):
    """[{name, path, bytes, created, files, databases}] newest first (the manifest is read when it is there)."""
    dest = Path(dest) if dest else backup_dir(settings)
    rows = []
    if not dest.is_dir():
        return rows
    for p in sorted(dest.iterdir(), reverse=True):
        if not NAME_RE.match(p.name):
            continue
        row = {"name": p.name, "path": str(p), "bytes": p.stat().st_size, "created": p.stat().st_mtime,
               "files": None, "databases": None}
        try:
            with zipfile.ZipFile(p) as z:
                m = json.loads(z.read(MANIFEST).decode("utf-8"))
            row.update(created=m.get("created", row["created"]), files=len(m.get("files", [])),
                       databases=len(m.get("databases", [])))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            row["files"] = None
        rows.append(row)
    return rows


def latest_age_h(settings=None, now=None):
    """Hours since the newest backup, or None when there is none."""
    rows = backups(settings)
    if not rows:
        return None
    return max(0.0, ((time.time() if now is None else now) - rows[0]["created"]) / 3600)


# ---- restore -------------------------------------------------------------------------------------------------------

def _safe_target(base, member):
    """base / member, refusing anything that leaves `base` (zip-slip)."""
    target = (Path(base) / member).resolve()
    if Path(base).resolve() not in target.parents:
        raise BackupError(f"unsafe path in the zip: {member}")
    return target


def inspect(zip_path, *, check_databases=True):
    """Verify a backup without writing anything to the data home: the zip is intact, the manifest is there, and each
    database in it passes SQLite's integrity check (on a temporary copy). Returns a dict with `ok` and `problems`."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise BackupError(f"no such backup: {zip_path}")
    problems, members = [], []
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            if bad:
                problems.append(f"damaged member: {bad}")
            try:
                manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
            except (KeyError, ValueError):
                manifest = {}
                problems.append("manifest.json missing or unreadable")
            members = [n for n in z.namelist() if n.startswith(ROOT_IN_ZIP) and not n.endswith("/")]
            dbs = []
            if check_databases:
                tmp = Path(tempfile.mkdtemp(prefix="waggle-verify-"))
                try:
                    for n in members:
                        _safe_target(tmp, n)  # a member that would leave the folder is refused before anything is read
                    for rel in manifest.get("databases", []):
                        n = ROOT_IN_ZIP + rel
                        if n not in members:
                            problems.append(f"{rel}: listed but not in the zip")
                            continue
                        out = tmp / f"{len(dbs)}.db"
                        with z.open(n) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        try:
                            con = sqlite3.connect(f"{out.as_uri()}?mode=ro", uri=True)
                            try:
                                res = con.execute("PRAGMA integrity_check").fetchone()
                            finally:
                                con.close()
                            if not res or res[0] != "ok":
                                problems.append(f"{rel}: integrity check says {res[0] if res else 'nothing'}")
                        except sqlite3.Error as e:
                            problems.append(f"{rel}: {e}")
                        dbs.append(rel)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
    except zipfile.BadZipFile as e:
        raise BackupError(f"not a valid zip: {e}") from e
    return {"ok": not problems, "problems": problems, "files": len(members), "created": manifest.get("created_iso"),
            "members": [n[len(ROOT_IN_ZIP):] for n in members]}


def restore(zip_path, *, to=None, in_place=False, dry_run=False, yes=False, settings=None, now=None):
    """Unpack a backup. dry_run: verify it and list what would be written (and what it would overwrite), touching
    nothing. to=<dir>: unpack there. in_place: over the data home, only with yes=True, after a safety backup of the
    present state (waggle-prerestore-*.zip). Returns a report dict; raises BackupError."""
    info = inspect(zip_path)
    if not (to or in_place):
        if not dry_run:
            raise BackupError("say where: --to <dir> for a drill, or --in-place --yes to put it over the data home "
                              "(or --dry-run to only check it)")
    target = Path(config.HOME) if in_place else Path(to) if to else Path(config.HOME)
    would = []
    for rel in info["members"]:
        t = _safe_target(target, rel)
        would.append({"path": str(t), "exists": t.exists()})
    report = {"dry_run": bool(dry_run), "target": str(target), "ok": info["ok"], "problems": info["problems"],
              "files": len(would), "would_overwrite": sum(1 for w in would if w["exists"]), "backup_of_now": None}
    if dry_run:
        return report
    if not info["ok"]:
        raise BackupError("the backup failed its check: " + "; ".join(info["problems"][:3]))
    if in_place:
        if not yes:
            raise BackupError("an in-place restore overwrites Waggle's live data; stop Waggle first "
                              "(python -m waggle.core off) and add --yes")
        s = settings or Settings.load()
        pre = run(s, now=now, dest=backup_dir(s) / "prerestore", home=target)  # the present state, kept aside
        pre_zip = Path(pre["zip"])
        moved = pre_zip.with_name(pre_zip.name.replace(PREFIX, PRE_RESTORE_PREFIX, 1))
        os.replace(pre_zip, moved)
        report["backup_of_now"] = str(moved)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for rel in info["members"]:
            out = _safe_target(target, rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(ROOT_IN_ZIP + rel) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            for side in ("-wal", "-shm"):  # a stale journal beside a restored database would be replayed into it
                if rel.endswith(".db"):
                    Path(str(out) + side).unlink(missing_ok=True)
    report["restored"] = len(info["members"])
    return report


# ---- core job and timer --------------------------------------------------------------------------------------------

def nightly_job(payload=None, *, settings=None, now=None, home=None):
    """Job backup.nightly. A failed backup is raised (the core records it and the doctor warns when the newest
    backup is over 36 h old)."""
    res = run(settings, now=now, home=home)
    return {k: res[k] for k in ("zip", "files", "databases", "bytes", "removed")}


def register_core(now=None):
    from .core import jobs, timers
    jobs.register(JOB_KIND, nightly_job, max_attempts=2, lease_s=600, timeout_s=900)
    timers.add(TIMER, "cron", CRON, JOB_KIND, {}, catch_up="latest", max_late_s=MAX_LATE_S, origin="backup", now=now)
    return {"jobs": [JOB_KIND], "timers": [TIMER]}


def doctor_check(settings=None, now=None):
    """(status, detail, fix) for waggle.doctor: warn when the newest backup is over 36 hours old."""
    age = latest_age_h(settings, now)
    if age is None:
        return "warn", f"no backup yet in {backup_dir(settings)}", "Run: python -m waggle.backup run"
    if age > 36:
        return "warn", f"the newest backup is {age / 24:.1f} days old", "Run: python -m waggle.backup run"
    return "ok", f"newest backup {age:.0f} h old ({len(backups(settings))} kept)", ""


# ---- command line --------------------------------------------------------------------------------------------------

def _mb(n):
    return f"{n / (1024 * 1024):.1f} MB"


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m waggle.backup", description="Local backups of ~/.waggle.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="back up now")
    sub.add_parser("list", help="the backups there are")
    r = sub.add_parser("restore", help="check or unpack a backup zip")
    r.add_argument("zip")
    r.add_argument("--dry-run", action="store_true", help="verify and list what would be written; touch nothing")
    r.add_argument("--to", help="unpack into this folder")
    r.add_argument("--in-place", action="store_true", help="put it over the data home (needs --yes)")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            res = run()
            print(f"Backed up {res['files']} files ({res['databases']} databases) to {res['zip']} ({_mb(res['bytes'])}); "
                  f"{res['skipped']} left out; removed {len(res['removed'])} old.")
            for p in res["problems"]:
                print("  could not read:", p)
            return 0
        if a.cmd == "list":
            rows = backups()
            if not rows:
                print(f"No backups in {backup_dir()}")
            for b in rows:
                when = dt.datetime.fromtimestamp(b["created"]).strftime("%Y-%m-%d %H:%M")
                print(f"{b['name']}  {when}  {_mb(b['bytes'])}  {b['files']} files, {b['databases']} databases")
            return 0
        res = restore(a.zip, to=a.to, in_place=a.in_place, dry_run=a.dry_run, yes=a.yes)
        if res["dry_run"]:
            print(f"Dry run: {res['files']} files would go to {res['target']} ({res['would_overwrite']} overwritten). "
                  f"Backup check: {'passed' if res['ok'] else 'FAILED: ' + '; '.join(res['problems'])}")
            return 0 if res["ok"] else 1
        print(f"Restored {res['restored']} files to {res['target']}."
              + (f" The state before is saved in {res['backup_of_now']}." if res["backup_of_now"] else ""))
        return 0
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
