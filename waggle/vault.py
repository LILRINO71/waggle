"""The Obsidian vault: Waggle's long-term memory, readable and editable by you."""
import datetime as dt
import os
import re
import time
from pathlib import Path

from .config import Settings

FOLDERS = ["Me", "Projects", "Journal", "Ideas", "People", "Jobs", "Inbox"]

SEED = {
    "Me/Profile.md": (
        "# Profile\n\nWhat Waggle knows about you. Edit anything that is wrong; Waggle reads this every turn.\n\n"
        "## Basics\n- Student building an AI/ML portfolio for college applications (GitHub: LILRINO71)\n"
        "- FTC robotics (BIOBUZZ season), Onshape CAD, laser cutter + 3D printer shop\n\n"
        "## How I like answers\n- Plain language, real numbers, no fluff\n\n## Goals\n\n"
    ),
    "Me/Likes.md": "# Likes\n\n",
    "Me/Dislikes.md": "# Dislikes\n\n- Bland template projects\n",
    "Me/Learned.md": "# Learned\n\nFacts Waggle picked up from conversations. Weekly review cleans this up.\n\n",
    "Ideas/Ideas.md": "# Ideas\n\nThings Waggle thought of to bring up with you.\n\n",
    "Waggle.md": (
        "# Waggle vault\n\n- **Me/**: your profile, likes, dislikes, what Waggle learned\n"
        "- **Projects/**: one note per project\n- **Journal/**: daily log\n- **Ideas/**: ideas Waggle wants to pitch\n"
        "- **People/**: teammates, teachers\n- **Jobs/**: background Claude builds and their results\n"
        "- **Inbox/**: drop notes here for Waggle to file\n"
    ),
}


def root(settings=None):
    return Path((settings or Settings.load()).vault)


def init(settings=None):
    r = root(settings)
    for f in FOLDERS:
        (r / f).mkdir(parents=True, exist_ok=True)
    (r / ".obsidian").mkdir(exist_ok=True)
    for rel, text in SEED.items():
        p = r / rel
        if not p.exists():
            p.write_text(text, encoding="utf-8")
    return r


def safe_path(rel, settings=None):
    """Resolve a vault-relative path and refuse anything that escapes the vault."""
    r = root(settings).resolve()
    p = (r / rel).resolve()
    if r != p and r not in p.parents:
        raise ValueError(f"path escapes the vault: {rel}")
    if p.suffix != ".md":
        p = p.with_suffix(".md")
    return p


def read(rel, settings=None):
    p = safe_path(rel, settings)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def append(rel, text, settings=None):
    p = safe_path(rel, settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else f"# {p.stem}\n\n"
    if not existing.endswith("\n"):
        existing += "\n"
    p.write_text(existing + text.rstrip() + "\n", encoding="utf-8")
    return p


def write(rel, text, settings=None):
    p = safe_path(rel, settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write a temp file and swap it in: a crash mid-write leaves the old note, never a half-written one.
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(5):
        try:
            os.replace(tmp, p)
            break
        except PermissionError:  # Obsidian or a sync client holding the file for a moment (Windows)
            if i == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2)
    return p


def journal(entry, settings=None, when=None):
    when = when or dt.datetime.now()
    return append(f"Journal/{when:%Y-%m-%d}", f"- {when:%H:%M} {entry}", settings)


def slug(text, n=50):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:n] or "note"


def notes(settings=None):
    r = root(settings)
    return [p for p in r.rglob("*.md") if ".obsidian" not in p.parts and ".trash" not in p.parts]
