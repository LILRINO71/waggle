"""Structured project state: one note per project in Projects/, the center of Waggle's awareness.

"Where were we with X?" is answered from the note, not by rereading old chats. Notes are plain
Markdown so you can edit them in Obsidian; tasks are Obsidian checkboxes (- [ ] / - [x])."""
import datetime as dt
import difflib
import re

from . import events, vault
from .config import Settings

SECTIONS = ["Goal", "Current focus", "Open problems", "Next actions", "Decisions", "Resources", "Log"]


def _rel(name):
    return f"Projects/{name}"


def render(name, status="active", aliases=(), **sections):
    today = f"{dt.date.today():%Y-%m-%d}"
    head = (f"---\ntype: project\nstatus: {status}\nupdated: {today}\naliases: [{', '.join(aliases)}]\n---\n"
            f"# {name}\n\n")
    body = []
    for sec in SECTIONS:
        key = sec.lower().replace(" ", "_")
        val = sections.get(key, "")
        if isinstance(val, (list, tuple)):
            if sec == "Next actions":
                val = "\n".join(f"- [ ] {v}" for v in val)
            else:
                val = "\n".join(f"- {v}" for v in val)
        body.append(f"## {sec}\n{val}\n")
    return head + "\n".join(body)


def create(name, settings=None, overwrite=False, **fields):
    p = vault.safe_path(_rel(name), settings)
    if p.exists() and not overwrite:
        return p
    vault.write(_rel(name), render(name, **fields), settings)
    events.log("project_update", project=name, action="create")
    return p


def all_projects(settings=None):
    r = vault.root(settings) / "Projects"
    out = []
    for p in sorted(r.glob("*.md")) if r.exists() else []:
        text = p.read_text(encoding="utf-8")
        if "type: project" not in text[:200]:
            continue
        meta = parse_meta(text)
        out.append({"name": p.stem, "status": meta.get("status", "active"), "aliases": meta.get("aliases", []),
                    "updated": meta.get("updated", "")})
    return out


def parse_meta(text):
    meta = {}
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                meta[k.strip()] = v
    return meta


def sections(name, settings=None):
    text = vault.read(_rel(name), settings)
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"## (.+)", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _set_section(name, section, content, settings=None):
    text = vault.read(_rel(name), settings)
    pat = re.compile(rf"(## {re.escape(section)}\n)(.*?)(?=\n## |\Z)", re.S)
    if pat.search(text):
        text = pat.sub(lambda m: m.group(1) + content.strip() + "\n", text, count=1)
    else:
        text = text.rstrip() + f"\n\n## {section}\n{content.strip()}\n"
    text = re.sub(r"updated: .*", f"updated: {dt.date.today():%Y-%m-%d}", text, count=1)
    vault.write(_rel(name), text, settings)


def add_log(name, entry, settings=None):
    cur = sections(name, settings).get("Log", "")
    line = f"- {dt.datetime.now():%Y-%m-%d %H:%M} {entry.strip()}"
    _set_section(name, "Log", (cur + "\n" + line).strip(), settings)
    events.log("project_update", project=name, action="log", entry=entry[:200])


def tasks(name, settings=None):
    """[{'text', 'done'}] from the Next actions checkboxes."""
    out = []
    for line in sections(name, settings).get("Next actions", "").splitlines():
        m = re.match(r"- \[( |x|X)\] (.+)", line.strip())
        if m:
            out.append({"text": m.group(2).strip(), "done": m.group(1).lower() == "x"})
    return out


def add_task(name, text, settings=None):
    cur = sections(name, settings).get("Next actions", "")
    _set_section(name, "Next actions", (cur + f"\n- [ ] {text.strip()}").strip(), settings)
    events.log("task", project=name, action="add", text=text[:200])


def complete_task(name, text, settings=None):
    """Tick the open task that best matches `text`. Returns the task text or None."""
    open_tasks = [t["text"] for t in tasks(name, settings) if not t["done"]]
    match = difflib.get_close_matches(text, open_tasks, n=1, cutoff=0.4)
    if not match:
        return None
    cur = sections(name, settings).get("Next actions", "")
    cur = cur.replace(f"- [ ] {match[0]}", f"- [x] {match[0]}", 1)
    _set_section(name, "Next actions", cur, settings)
    events.log("task", project=name, action="done", text=match[0][:200])
    return match[0]


def find(text, settings=None):
    """Projects mentioned in a message, by name or alias (case-insensitive)."""
    low = (text or "").lower()
    hits = []
    for p in all_projects(settings):
        names = [p["name"]] + list(p["aliases"])
        if any(re.search(rf"\b{re.escape(n.lower())}\b", low) for n in names if len(n) >= 3):
            hits.append(p["name"])
    return hits


def context_for(text, settings=None, limit=2):
    """Full state of projects the message mentions, plus a one-line index of all active projects."""
    s = settings or Settings.load()
    active = [p for p in all_projects(s) if p["status"] == "active"]
    lines = ["<waggle_projects>", "Active projects: " + ", ".join(p["name"] for p in active)]
    for name in find(text, s)[:limit]:
        lines += ["", vault.read(_rel(name), s)[:4000]]
    lines.append("</waggle_projects>")
    return "\n".join(lines)
