"""Personal knowledge graph: how the user's projects, people, decisions, ideas and objectives connect.

Built two ways, both free:
1. From the vault's own structure (deterministic, every few hours): one node per note (type from its folder),
   an edge per [[wikilink]], and every line of a project's "Decisions" section as a dated decision node.
2. From conversations (local model, no Claude): recent user turns are read for people/projects/tools and for
   decisions the user made ("let's go with the ramp transfer"); a decision about a known project is also written
   into that project's Decisions section with the date, so decision history stays in the user's own notes.
Used for context: when a message mentions a known entity, its closest connections and latest decisions are
added to the turn (a few lines, not whole notes). Agents can query it with graph_query.
"""
import datetime as dt
import json
import re
import sqlite3
import time

from . import projects, vault
from .config import Settings, data_path

FOLDER_TYPE = {"Projects": "project", "People": "person", "Ideas": "idea", "Objectives": "objective",
               "Research": "research", "Me": "profile", "Jobs": "build", "Routines": "routine", "Agents": "agent"}
SKIP_FOLDERS = {"Journal", "Inbox", ".obsidian", "Waggle"}
TYPES = {"person", "project", "decision", "idea", "objective", "research", "profile", "build", "routine", "agent",
         "tool", "place", "org", "topic", "event", "note"}


def _db():
    con = sqlite3.connect(data_path("graph.db"), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS nodes (key TEXT PRIMARY KEY, name TEXT, type TEXT, summary TEXT, source TEXT,
        first_seen REAL, last_seen REAL, mentions INTEGER DEFAULT 0, date TEXT);
    CREATE TABLE IF NOT EXISTS edges (src TEXT, rel TEXT, dst TEXT, source TEXT, time REAL,
        PRIMARY KEY (src, rel, dst));
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    return con


def key(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())[:120]


def upsert_node(con, name, type_="note", summary="", source="", date=None):
    k = key(name)
    if not k:
        return None
    now = time.time()
    row = con.execute("SELECT type, summary FROM nodes WHERE key=?", (k,)).fetchone()
    if row:
        keep_type = row["type"] if row["type"] not in ("note", "topic") else type_
        con.execute("UPDATE nodes SET type=?, summary=COALESCE(NULLIF(?, ''), summary), last_seen=?, "
                    "mentions=mentions+1, date=COALESCE(?, date) WHERE key=?", (keep_type, summary[:400], now, date, k))
    else:
        con.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,1,?)", (k, name.strip()[:120], type_, summary[:400],
                                                                     source, now, now, date))
    return k


def add_edge(con, src, rel, dst, source=""):
    if src and dst and src != dst:
        con.execute("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?)", (src, rel[:40], dst, source, time.time()))


# ---- 1. from the vault ---------------------------------------------------------------------------

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})[:\s-]*(.*)$")


def build_from_vault(settings=None):
    """Rebuild vault-derived nodes/edges (conversation-derived ones are kept). Returns counts."""
    s = settings or Settings.load()
    root = vault.root(s)
    con = _db()
    con.execute("DELETE FROM edges WHERE source='vault'")
    n_notes = n_links = n_dec = 0
    for p in vault.notes(s):
        rel = p.relative_to(root)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        if folder in SKIP_FOLDERS or p.stem.startswith("Waggle HQ"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        t = FOLDER_TYPE.get(folder, "note")
        if folder == "Agents":
            continue
        first = next((l.strip("# ").strip() for l in text.splitlines() if l.strip() and not l.startswith("---")
                      and ":" not in l[:20]), "")
        src = upsert_node(con, p.stem, t, first[:200], "vault")
        n_notes += 1
        for target in set(WIKILINK.findall(text)):
            dst = upsert_node(con, target.strip().split("/")[-1], "note", "", "vault")
            add_edge(con, src, "mentions", dst, "vault")
            n_links += 1
        if t == "project":
            for line in projects.sections(p.stem, s).get("Decisions", "").splitlines():
                line = line.strip().lstrip("-* ").strip()
                if len(line) < 6:
                    continue
                m = DATED.match(line)
                date, txt = (m.group(1), m.group(2)) if m else (None, line)
                d = upsert_node(con, f"decision: {txt[:90]}", "decision", txt, "vault", date)
                add_edge(con, src, "decided", d, "vault")
                n_dec += 1
        if folder == "People" and p.stem == "People":
            for line in text.splitlines():
                m = re.match(r"\s*[-*]\s*\**([A-Z][\w.' -]{1,40}?)\**\s*[-:—]\s*(.+)", line)
                if m:
                    upsert_node(con, m.group(1), "person", m.group(2)[:200], "vault")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('vault_built', ?)", (str(time.time()),))
    con.commit()
    con.close()
    return {"notes": n_notes, "links": n_links, "decisions": n_dec}


# ---- 2. from conversations (local model) ---------------------------------------------------------

EXTRACT_SYSTEM = """Read messages a user sent to their personal assistant. Extract ONLY what the user clearly
stated. Return JSON: {"entities": [{"name": "...", "type": "person|project|tool|place|org|topic|event"}],
"relations": [{"src": "...", "rel": "short verb like works_on, uses, part_of, member_of, about", "dst": "..."}],
"decisions": [{"project": "project name or empty", "decision": "what the user decided, one sentence"}]}.
A decision is a choice the user made ("let's use X", "we're going with Y", "I'll do Z first"), not a question.
Use empty lists when there is nothing. No guesses."""


def extract_from_traces(since=None, settings=None, chat=None, max_turns=40):
    """Read recent user turns with the local model; add entities/relations; file decisions into project notes."""
    from . import traces
    s = settings or Settings.load()
    con = _db()
    last = con.execute("SELECT v FROM meta WHERE k='traces_until'").fetchone()
    since = since if since is not None else float(last["v"]) if last else time.time() - 86400
    rows = [r for r in traces.all_traces(since, limit=max_turns) if not str(r.get("session", "")).startswith(
        ("objective-", "routine-")) and len(r.get("request") or "") > 15][::-1]
    if not rows:
        con.close()
        return {"turns": 0}
    if chat is None:
        from . import ollama
        chat = lambda msgs: ollama.chat_json(msgs, settings=s, timeout=120)  # noqa: E731
    known = [p["name"] for p in projects.all_projects(s)]
    added, filed = 0, 0
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        msgs = "\n".join(f"- {r['request'][:400]}" for r in batch)
        try:
            out = chat([{"role": "system", "content": EXTRACT_SYSTEM + f"\nKnown projects: {', '.join(known)}"},
                        {"role": "user", "content": msgs}]) or {}
        except Exception:
            continue
        for e in out.get("entities") or []:
            if isinstance(e, dict) and e.get("name") and len(e["name"]) < 60:
                upsert_node(con, e["name"], e.get("type") if e.get("type") in TYPES else "topic", "", "chat")
                added += 1
        for r in out.get("relations") or []:
            if isinstance(r, dict) and r.get("src") and r.get("dst"):
                a = upsert_node(con, r["src"], "topic", "", "chat")
                b = upsert_node(con, r["dst"], "topic", "", "chat")
                add_edge(con, a, str(r.get("rel") or "related_to"), b, "chat")
        for d in out.get("decisions") or []:
            if not isinstance(d, dict) or not d.get("decision"):
                continue
            proj = _match_project(d.get("project", ""), known)
            date = dt.date.today().isoformat()
            node = upsert_node(con, f"decision: {d['decision'][:90]}", "decision", d["decision"], "chat", date)
            if proj:
                add_edge(con, upsert_node(con, proj, "project", "", "chat"), "decided", node, "chat")
                if record_decision(proj, d["decision"], s):
                    filed += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('traces_until', ?)", (str(rows[-1]["time"] + 0.001),))
    con.commit()
    con.close()
    return {"turns": len(rows), "entities": added, "decisions_filed": filed}


def _match_project(name, known):
    n = key(name)
    if not n:
        return None
    for k in known:
        if key(k) == n or n in key(k) or key(k) in n:
            return k
    return None


def record_decision(project, text, settings=None):
    """Append a dated decision to the project's Decisions section (skips near-duplicates)."""
    cur = projects.sections(project, settings).get("Decisions", "")
    words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    for line in cur.splitlines():
        lw = set(re.findall(r"[a-z0-9]{4,}", line.lower()))
        if words and len(words & lw) / max(1, len(words)) > 0.7:
            return False
    line = f"- {dt.date.today().isoformat()}: {text.strip()} _(from chat)_"
    projects._set_section(project, "Decisions", (cur + "\n" + line).strip(), settings)
    return True


# ---- queries -------------------------------------------------------------------------------------

def neighbors(name, limit=12):
    k = key(name)
    con = _db()
    node = con.execute("SELECT * FROM nodes WHERE key=?", (k,)).fetchone()
    if not node:
        hit = con.execute("SELECT * FROM nodes WHERE key LIKE ? ORDER BY mentions DESC LIMIT 1", (f"%{k}%",)).fetchone()
        node = hit
    if not node:
        con.close()
        return None
    k = node["key"]
    out = [dict(r) for r in con.execute(
        "SELECT e.rel, n.name, n.type, n.date, n.summary, 'out' AS dir FROM edges e JOIN nodes n ON n.key=e.dst "
        "WHERE e.src=? UNION ALL SELECT e.rel, n.name, n.type, n.date, n.summary, 'in' FROM edges e JOIN nodes n "
        "ON n.key=e.src WHERE e.dst=?", (k, k))]
    con.close()
    decisions = sorted([r for r in out if r["type"] == "decision"], key=lambda r: r["date"] or "", reverse=True)
    others = [r for r in out if r["type"] != "decision"]
    return {"name": node["name"], "type": node["type"], "summary": node["summary"],
            "decisions": decisions[:6], "related": others[:limit]}


def _names(con):
    return [(r["name"], r["key"]) for r in con.execute(
        "SELECT name, key FROM nodes WHERE type IN ('project','person','idea','objective','research','tool','org') "
        "AND length(key) >= 4")]


def context_for(message, max_entities=2):
    """A few lines about known entities the message mentions (their links and latest decisions)."""
    low = (message or "").lower()
    if not low:
        return ""
    con = _db()
    names = _names(con)
    con.close()
    hits = sorted({n for n, k in names if re.search(r"\b" + re.escape(k) + r"\b", low)}, key=len, reverse=True)
    if not hits:
        return ""
    lines = ["<knowledge_graph>", "How things the user mentioned connect (from their notes and past chats):"]
    for n in hits[:max_entities]:
        nb = neighbors(n)
        if not nb:
            continue
        rel = ", ".join(f"{r['rel']} {r['name']}" if r["dir"] == "out" else f"{r['name']} {r['rel']} it"
                        for r in nb["related"][:6])
        lines.append(f"- {nb['name']} ({nb['type']})" + (f": {rel}" if rel else ""))
        for d in nb["decisions"][:3]:
            lines.append(f"  decided{(' ' + d['date']) if d['date'] else ''}: {(d['summary'] or d['name'])[:160]}")
    lines.append("</knowledge_graph>")
    return "\n".join(lines) if len(lines) > 3 else ""


def stats():
    con = _db()
    by = {r["type"]: r["n"] for r in con.execute("SELECT type, COUNT(*) AS n FROM nodes GROUP BY type")}
    edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    con.close()
    return {"nodes": sum(by.values()), "by_type": by, "edges": edges}


def main():
    """Cron entry. Prints nothing: Hermes sends a script job's stdout to the phone."""
    out = {"time": time.time(), "vault": build_from_vault()}
    try:
        out["chat"] = extract_from_traces()
    except Exception as e:  # local model down: the vault part still counts
        out["chat"] = {"error": f"{type(e).__name__}: {e}"}
    out["stats"] = stats()
    data_path("graph_last.json").write_text(json.dumps(out), encoding="utf-8")


MCP_TOOLS = [
    ({"name": "graph_query", "description": "What the user's knowledge graph knows about a person, project, idea or "
      "topic: how it connects to other things and the decisions made about it (dated).",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps(neighbors(a.get("name", "")) or {"error": "not in the graph"}, default=str)),
    ({"name": "graph_note", "description": "Record a relationship (e.g. 'Arjun' member_of 'FTC 7831') or, with "
      "rel='decided', a decision about a project (also written into that project's Decisions section).",
      "parameters": {"type": "object", "properties": {"src": {"type": "string"}, "rel": {"type": "string"},
                     "dst": {"type": "string"}}, "required": ["src", "rel", "dst"]}},
     lambda a: json.dumps(_note(a["src"], a["rel"], a["dst"]))),
]


def _note(src, rel, dst):
    con = _db()
    if rel.lower() in ("decided", "decision"):
        known = [p["name"] for p in projects.all_projects()]
        proj = _match_project(src, known)
        node = upsert_node(con, f"decision: {dst[:90]}", "decision", dst, "agent", dt.date.today().isoformat())
        add_edge(con, upsert_node(con, proj or src, "project" if proj else "topic", "", "agent"), "decided", node,
                 "agent")
        con.commit()
        con.close()
        return {"recorded": True, "filed_in_project": bool(proj and record_decision(proj, dst))}
    a = upsert_node(con, src, "topic", "", "agent")
    b = upsert_node(con, dst, "topic", "", "agent")
    add_edge(con, a, rel, b, "agent")
    con.commit()
    con.close()
    return {"recorded": True}


if __name__ == "__main__":
    main()
