"""The Obsidian vault: Waggle's long-term memory, readable and editable by you.

Two places in it: the whole vault (root(): what Waggle READS) and Waggle's own folder (own(): settings.vault_own, e.g.
"12 - Waggle"; the vault root when empty), the only place it writes. read / write / append / journal / safe_path /
own_path take paths relative to the own folder; notes() and vault_path() cover the whole vault."""
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
    """The whole vault: what Waggle READS (memory search, graph, digest, profile)."""
    return Path((settings or Settings.load()).vault)


def own_rel(settings=None):
    """The folder (relative to the vault) where Waggle keeps its own notes: "" or e.g. "12 - Waggle"."""
    raw = str(getattr((settings or Settings.load()), "vault_own", "") or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return ""
    if ":" in raw or ".." in raw.split("/") or Path(raw).is_absolute():
        raise ValueError(f"vault_own must be a plain folder inside the vault, not {raw!r}")
    return raw


def own(settings=None):
    """Waggle's own folder inside the vault: the ONLY place Waggle writes. It is the vault root when vault_own is empty."""
    s = settings or Settings.load()
    rel = own_rel(s)
    return root(s) / rel if rel else root(s)


def _under(base, p):
    return base == p or base in p.parents


def _own_relative(rel, s):
    rel = str(rel).replace("\\", "/").lstrip("/")
    pre = own_rel(s)
    if pre and rel.lower().startswith(pre.lower() + "/"):  # a path that already names the folder is not doubled
        rel = rel[len(pre) + 1:]
    return rel


def own_path(rel, settings=None):
    """Resolve a path relative to Waggle's own folder and refuse anything that leaves it. A bare name gets .md."""
    s = settings or Settings.load()
    base = own(s).resolve()
    p = (base / _own_relative(rel, s)).resolve()
    if not _under(base, p):
        raise ValueError(f"path escapes Waggle's folder: {rel}")
    if p.suffix != ".md":
        p = p.with_suffix(".md")
    return p


safe_path = own_path  # the name the callers use


def vault_path(rel, settings=None):
    """Resolve a path relative to the whole vault (a note anywhere in it, for reading) and refuse anything outside it."""
    s = settings or Settings.load()
    base = root(s).resolve()
    p = (base / str(rel).replace("\\", "/").lstrip("/")).resolve()
    if not _under(base, p):
        raise ValueError(f"path escapes the vault: {rel}")
    if p.suffix != ".md":
        p = p.with_suffix(".md")
    return p


def in_own(path, settings=None):
    """True when the path is inside Waggle's own folder."""
    s = settings or Settings.load()
    try:
        return _under(own(s).resolve(), Path(path).resolve())
    except OSError:
        return False


def vault_rel(path, settings=None):
    """Vault-relative posix path of a note (what the index, search results and the graph call it)."""
    return Path(path).resolve().relative_to(root(settings).resolve()).as_posix()


def own_relpath(path, settings=None):
    """Path relative to Waggle's own folder (what vault.read / vault.write take), or None when it is outside it."""
    s = settings or Settings.load()
    try:
        return Path(path).resolve().relative_to(own(s).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def init(settings=None):
    s = settings or Settings.load()
    o = own(s)
    for f in FOLDERS:
        (o / f).mkdir(parents=True, exist_ok=True)
    if not own_rel(s):  # Waggle's own vault: the owner's vault already has its own .obsidian, which is never created here
        (o / ".obsidian").mkdir(exist_ok=True)
    for name, text in SEED.items():
        p = o / name
        if not p.exists():
            p.write_text(text, encoding="utf-8")
    return o


def read(rel, settings=None):
    """A note in Waggle's own folder ('' when it does not exist)."""
    p = own_path(rel, settings)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_any(rel, settings=None):
    """A note anywhere in the vault, by its vault-relative path ('' when it does not exist). Read only."""
    p = vault_path(rel, settings)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _record(p, settings):
    """Tell the vault history about a write that is about to happen (waggle/vault_git.py). Never raises."""
    try:
        from . import vault_git
        vault_git.note_write(p, settings)
    except Exception:  # noqa: BLE001  (history must never stop a note from being written)
        pass


def append(rel, text, settings=None):
    p = safe_path(rel, settings)
    _record(p, settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else f"# {p.stem}\n\n"
    if not existing.endswith("\n"):
        existing += "\n"
    p.write_text(existing + text.rstrip() + "\n", encoding="utf-8")
    return p


def write(rel, text, settings=None):
    p = safe_path(rel, settings)
    _record(p, settings)
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
    """Every note in the whole vault, the owner's and Waggle's. Dot folders (.obsidian, .git, .trash, .waggle), the
    Drop folder (files waiting to be filed) and Sources/_files (the originals that were dropped, some of them .md
    files) are not notes."""
    s = settings or Settings.load()
    r = root(s)
    o = own(s).resolve()
    drop = (o / "Drop").resolve()
    out = []
    for dp, dirs, files in os.walk(r):
        here = Path(dp)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "_files" and (here / d).resolve() != drop]
        out += [here / f for f in files if f.endswith(".md") and not f.startswith(".")]
    return out


def own_notes(settings=None):
    """The notes in Waggle's own folder (same exclusions as notes())."""
    s = settings or Settings.load()
    o = own(s).resolve()
    return [p for p in notes(s) if _under(o, p.resolve())]




# ---- the owner's profile notes ------------------------------------------------------------------------------

def profile_notes(settings=None):
    """The owner's own profile notes, in the order the setting `vault_profile_notes` lists them. Each entry is a
    vault-relative path or a glob (e.g. "01 - Identity/*"). Read only; nothing outside the vault is ever matched."""
    s = settings or Settings.load()
    base = root(s).resolve()
    out, seen = [], set()
    for pat in getattr(s, "vault_profile_notes", None) or []:
        pat = str(pat).replace("\\", "/").strip().lstrip("/")
        if not pat or ".." in pat.split("/"):
            continue
        if not any(c in pat for c in "*?[") and not pat.endswith(".md"):
            pat += ".md"
        for h in sorted(base.glob(pat)):
            h = h.resolve()
            if h.suffix == ".md" and h.is_file() and _under(base, h) and h not in seen:
                seen.add(h)
                out.append(h)
    return out


_FRONT = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n", re.S)
_TAG = re.compile(r"\s*#epi/[\w-]+")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_NAV = re.compile(r"#+\s*(map|update rule|related|links)\b", re.I)




def _compact(text):
    """One profile note as (title, bits): its bullet points (else the first column of its table, else its first
    lines of text), with no frontmatter, breadcrumbs, callouts, link lists or Obsidian tags. An index note (type:
    index, only links to other notes) gives no bits."""
    text = text.replace("\r\n", "\n")
    front = _FRONT.match(text)
    if front and re.search(r"^type:\s*index\s*$", front.group(0), re.M):
        return "", []
    text = _FRONT.sub("", text)
    title, bullets, cells, prose, skip = "", [], [], [], False
    for line in text.splitlines():
        l = line.strip()
        if l.startswith("# ") and not title:
            title = l[2:].strip()
            continue
        if l.startswith("#"):
            skip = bool(_NAV.match(l))
            continue
        if skip or not l or l.startswith((">", "---", "![[", "[[")) or re.match(r"related:", l, re.I):
            continue
        if l.startswith("|"):
            first = [c.strip() for c in l.strip("|").split("|")][:1]
            if first and first[0] and not set(first[0]) <= set("-: ") and len(cells) < 12:
                cells.append(first[0])
            continue
        l = _LINK.sub(lambda m: m.group(2) or m.group(1), _TAG.sub("", l)).replace("**", "").strip()
        is_bullet = l.startswith(("- ", "* ", "+ "))
        l = l.lstrip("-*+ ").strip().rstrip(".;")
        if not l or re.match(r"^(not recorded|not provided|none)\b", l, re.I):
            continue
        (bullets if is_bullet else prose).append(l)
    if not bullets and cells:  # a table: its first column (the first row is the header)
        return title, list(dict.fromkeys(_LINK.sub(lambda m: m.group(2) or m.group(1), c) for c in cells[1:]))
    return title, bullets or prose[:2]


def profile_digest(settings=None, cap=1600):
    """The owner's profile notes boiled down to at most about `cap` characters: one line per note (the first note,
    the central profile, gets three times the room), a short note kept in full, a long one cut to its share.
    '' when no profile notes are configured or found."""
    s = settings or Settings.load()
    items = []
    for p in profile_notes(s):
        try:
            title, bits = _compact(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if bits:
            items.append((title or p.stem, bits))
    if not items:
        return ""
    lines = [f"- {t}: " + "; ".join(b) for t, b in items]
    weight = [3.0 if i == 0 else 1.0 for i in range(len(lines))]
    budget, wleft, out = float(max(200, int(cap))), sum(weight), {}
    for i in sorted(range(len(lines)), key=lambda k: len(lines[k]) / weight[k]):  # smallest first: the rest goes to long ones
        share = max(60, int(budget * weight[i] / wleft))
        text = lines[i]
        if len(text) > share:
            text = text[:share].rsplit(" ", 1)[0].rstrip(";:, ") + "..."
        out[i] = text
        budget -= len(text) + 1
        wleft -= weight[i]
    return "\n".join(out[i] for i in range(len(lines)))
