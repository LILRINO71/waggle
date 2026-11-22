"""Personal knowledge graph: how the user's projects, people, decisions, ideas and objectives connect.

Built two ways, both free:
1. From the vault's own structure (deterministic, every few hours): one node per note (type from its folder),
   an edge per [[wikilink]], and every line of a project's "Decisions" section as a dated decision node.
2. From conversations (local model, no Claude): recent user turns are read for people/projects/tools and for
   decisions the user made ("let's go with the ramp transfer"); a decision about a known project is also written
   into that project's Decisions section with the date, so decision history stays in the user's own notes.
Used for context: when a message mentions a known entity, its closest connections and latest decisions are
added to the turn (a few lines, not whole notes). Agents can query it with graph_query.

Quality rules (mission 3):
- A decision is a commitment in the user's own words. "Maybe X" / "thinking about X" becomes an idea (status
  proposed) and is never written into a project's Decisions. Requests to the assistant are neither.
- Every chat-derived node keeps where it came from (source_ref: session + time) and the user's words (quote).
- A decision that changes an earlier one supersedes it: the old one is struck through in the note
  (~~old~~ _(changed <date>)_) and context shows only the current one, with what it replaced.
- The project note is the source of truth: a decision line the user deletes or strikes out in Obsidian is retracted
  or superseded in the graph on the next rebuild. graph_correct retracts, merges, confirms or rewrites by hand.
- Names resolve through aliases (project frontmatter `aliases:` and merges) and a punctuation/plural-insensitive
  form, so "Bio Buzz" and "BIOBUZZ" don't become two nodes. Chat entities must appear literally in what the user
  wrote; new free-floating topics, time words and file names are dropped; relations need both ends to be real.
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
SKIP_FOLDERS = {"Journal", "Inbox", ".obsidian", "Waggle", "Archive", "Quarantine"}  # archives are not knowledge
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
    CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, key TEXT, source TEXT);
    """)
    have = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
    for name, typ in (("status", "TEXT DEFAULT 'current'"), ("source_ref", "TEXT"), ("quote", "TEXT"),
                      ("superseded_by", "TEXT"), ("updated", "REAL")):
        if name not in have:
            try:
                con.execute(f"ALTER TABLE nodes ADD COLUMN {name} {typ}")
            except sqlite3.OperationalError:
                pass
    return con


def _norm(k):
    """Punctuation-, space- and plural-insensitive form used to spot duplicates ("Bio Buzz" == "biobuzz")."""
    last = re.split(r"[^a-z0-9]+", (k or "").strip())[-1]
    n = re.sub(r"[^a-z0-9]", "", k or "")
    # plural only on a real last word: "robots" == "robot", but "Arjun S" stays apart from "Arjun"
    return n[:-1] if len(last) >= 4 and last.endswith("s") and not last.endswith("ss") else n


def canonical(con, name):
    """The key an existing node already uses for this name (exact, alias, or same normalised form), else key()."""
    k = key(name)
    if not k:
        return k
    if con.execute("SELECT 1 FROM nodes WHERE key=?", (k,)).fetchone():
        return k
    row = con.execute("SELECT key FROM aliases WHERE alias=?", (k,)).fetchone()
    if row:
        return row["key"]
    n = _norm(k)
    if len(n) >= 4 and not k.startswith("decision: "):
        for r in con.execute("SELECT key FROM nodes WHERE type != 'decision'"):
            if _norm(r["key"]) == n:
                return r["key"]
    return k


def key(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())[:120]


def upsert_node(con, name, type_="note", summary="", source="", date=None, status=None, source_ref=None,
                quote=None):
    k = canonical(con, name)
    if not k:
        return None
    now = time.time()
    row = con.execute("SELECT type, summary FROM nodes WHERE key=?", (k,)).fetchone()
    if row:
        keep_type = row["type"] if row["type"] not in ("note", "topic") else type_
        con.execute("UPDATE nodes SET type=?, summary=COALESCE(NULLIF(?, ''), summary), last_seen=?, "
                    "mentions=mentions+1, date=COALESCE(?, date), status=COALESCE(?, status), "
                    "source_ref=COALESCE(source_ref, ?), quote=COALESCE(quote, ?) WHERE key=?",
                    (keep_type, summary[:400], now, date, status, source_ref, (quote or None) and quote[:400], k))
    else:
        con.execute("INSERT INTO nodes (key, name, type, summary, source, first_seen, last_seen, mentions, date, "
                    "status, source_ref, quote, updated) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)",
                    (k, name.strip()[:120], type_, summary[:400], source, now, now, date, status or "current",
                     source_ref, (quote or "")[:400] or None, now))
    return k


def set_status(con, k, status, superseded_by=None):
    con.execute("UPDATE nodes SET status=?, superseded_by=COALESCE(?, superseded_by), updated=? WHERE key=?",
                (status, superseded_by, time.time(), k))


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
    con.execute("DELETE FROM aliases WHERE source='vault'")
    n_notes = n_links = n_dec = 0
    seen_dec, scanned = set(), set()
    own = vault.own(s).resolve()
    for p in vault.notes(s):
        mine = vault.in_own(p, s)
        rel = p.resolve().relative_to(own if mine else root.resolve())  # Waggle's folders are named inside its own folder
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        if not mine:
            folder = "(vault) " + folder  # the owner's folders never count as Waggle's (Journal, Archive ...)
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
            scanned.add(src)
            fm = re.search(r"^aliases:\s*\[(.*?)\]", text[:600], re.M)
            for a in (fm.group(1).split(",") if fm else []):
                a = key(a.strip(" '\""))
                if len(a) >= 3 and a != src:
                    con.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?)", (a, src, "vault"))
            for line in projects.sections(p.stem, s).get("Decisions", "").splitlines():
                line = line.strip().lstrip("-* ").strip()
                if len(line) < 6:
                    continue
                struck = line.startswith("~~")
                not_decision = "not a decision" in line
                line = re.sub(r"_?\((?:from chat|changed [^)]*|not a decision[^)]*)\)_?", "",
                              line.replace("~~", "")).strip()
                m = DATED.match(line)
                date, txt = (m.group(1), m.group(2).strip()) if m else (None, line)
                d = upsert_node(con, f"decision: {txt[:90]}", "decision", txt, "vault", date,
                                source_ref=f"vault Projects/{p.stem}.md")
                if struck:
                    set_status(con, d, "retracted" if not_decision else "superseded")
                elif con.execute("SELECT status FROM nodes WHERE key=?", (d,)).fetchone()["status"] == "retracted":
                    set_status(con, d, "current")  # the user put it back in the note
                seen_dec.add(d)
                add_edge(con, src, "decided", d, "vault")
                n_dec += 1
        if folder == "People" and p.stem == "People":
            for line in text.splitlines():
                m = re.match(r"\s*[-*]\s*\**([A-Z][\w.' -]{1,40}?)\**\s*[-:—]\s*(.+)", line)
                if m:
                    upsert_node(con, m.group(1), "person", m.group(2)[:200], "vault")
    # The note is the source of truth: a decision no longer in its project's note was removed by the user.
    retracted = 0
    for r in con.execute("SELECT DISTINCT n.key, n.source, n.status FROM nodes n JOIN edges e ON e.dst=n.key "
                         "WHERE n.type='decision' AND e.rel='decided' AND e.src IN (%s)"
                         % ",".join("?" * len(scanned)), tuple(scanned)).fetchall() if scanned else []:
        if r["key"] not in seen_dec and r["status"] not in ("retracted", "superseded"):
            set_status(con, r["key"], "retracted")
            retracted += 1
    for r in con.execute("SELECT key FROM nodes WHERE type='decision' AND source='vault'").fetchall():
        if r["key"] not in seen_dec and not con.execute("SELECT 1 FROM edges WHERE src=? OR dst=?",
                                                         (r["key"], r["key"])).fetchone():
            con.execute("DELETE FROM nodes WHERE key=?", (r["key"],))  # derived only from a line that's gone
    con.execute("INSERT OR REPLACE INTO meta VALUES ('vault_built', ?)", (str(time.time()),))
    con.commit()
    con.close()
    return {"notes": n_notes, "links": n_links, "decisions": n_dec, "retracted": retracted}


# ---- 2. from conversations (local model) ---------------------------------------------------------

EXTRACT_SYSTEM = """Read messages a user sent to their personal assistant. Extract ONLY what the user clearly
stated. Return JSON: {"entities": [{"name": "...", "type": "person|project|tool|place|org|topic|event"}],
"relations": [{"src": "...", "rel": "short verb like works_on, uses, part_of, member_of, about", "dst": "..."}],
"decisions": [{"project": "project name or empty", "decision": "what the user decided, one sentence",
"status": "decided|considering", "replaces": "the earlier choice this changes, only if the user said so, else empty",
"quote": "the user's exact words"}]}.
"decided" = a clear commitment by the user about their own work ("we're going with Y", "decided on X", "switching
the shooter to Z"). "considering" = an idea, maybe, or question ("maybe X", "what if", "thinking about").
NOT decisions: requests or instructions to the assistant (write/remind/check/send/make a note), tests, small talk.
Entities: only real named people, projects, tools, organisations, places, events the user wrote; not dates, times,
files or generic words. Use empty lists when there is nothing. No guesses."""

_STR = {"type": "string"}
EXTRACT_SCHEMA = {"type": "object", "required": ["entities", "relations", "decisions"], "properties": {
    "entities": {"type": "array", "items": {"type": "object", "required": ["name", "type"], "properties": {
        "name": _STR, "type": _STR}}},
    "relations": {"type": "array", "items": {"type": "object", "required": ["src", "rel", "dst"], "properties": {
        "src": _STR, "rel": _STR, "dst": _STR}}},
    "decisions": {"type": "array", "items": {"type": "object", "required": ["decision"], "properties": {
        "project": _STR, "decision": _STR, "status": {"type": "string", "enum": ["decided", "considering"]},
        "replaces": _STR, "quote": _STR}}}}}

STOP_NAMES = {"today", "tomorrow", "tonight", "yesterday", "now", "morning", "evening", "week", "this week",
              "next week", "weekend", "it", "this", "that", "something", "everything", "anything", "me", "you",
              "user", "assistant", "the user", "notes", "note", "file", "files", "email", "emails", "message",
              # the vault's own folders are places in Waggle's notes, not things in the user's life
              "inbox", "journal", "projects", "ideas", "people", "jobs", "objectives", "research", "routines"}
# Requests to the assistant ("write a note...", "I'll have you check...") are not decisions about the user's work.
ASSISTANT_REQUEST = re.compile(r"^\s*(?:(?:i'?ll|i will|please|can you|could you|go)\s+)?(?:write|remind|send|check|"
                               r"find|list|tell|show|open|search|save|draft|read|look up|summari[sz]e|make a note|"
                               r"create a note|note down)\b", re.I)
REL_OK = {"works_on", "uses", "part_of", "member_of", "about", "owns", "mentors", "teammate_of", "built", "plans",
          "leads", "coaches", "related_to"}


def _words(text):
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


def _said(name, text):
    """Did the user literally write this name (case-insensitive, word boundary)?"""
    return bool(name) and re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", text) is not None


def _ref(batch, text):
    """Which chat turn something came from: 'chat <session> <YYYY-MM-DD HH:MM>' of the best-matching message."""
    want = _words(text) or {text.lower()}
    row = max(batch, key=lambda r: len(want & _words(r.get("request"))) + (text.lower() in (r.get("request") or "")
                                                                            .lower()))
    return f"chat {row.get('session', '')} {dt.datetime.fromtimestamp(row['time']):%Y-%m-%d %H:%M}"


def _strike_filed(project, text, settings=None):
    """Strike a line Waggle filed from chat ("_(from chat)_") in a project's Decisions; leaves the user's own lines."""
    try:
        cur = projects.sections(project, settings).get("Decisions", "")
    except Exception:
        return False
    want = _words(text)
    lines = cur.splitlines()
    for i, line in enumerate(lines):
        if "(from chat)" in line and "~~" not in line and want and len(want & _words(line)) / len(want) >= 0.8:
            body = line.strip().lstrip("-* ").strip()
            lines[i] = f"- ~~{body}~~ _(not a decision: removed {dt.date.today().isoformat()})_"
            projects._set_section(project, "Decisions", "\n".join(lines), settings)
            return True
    return False


def cleanup(con=None):
    """Apply today's quality rules to what older extraction runs left (once): drop noise entities and free-floating
    chat topics, retract chat 'decisions' that were really requests to the assistant. Such a line that Waggle itself
    filed into a project note (marked "(from chat)") is struck through there, not deleted, so the rebuild doesn't
    bring it back and the user can still see and undo it. Nothing the user wrote is touched."""
    own = con is None
    con = con or _db()
    removed = retracted = 0
    for r in con.execute("SELECT key, name, type, summary FROM nodes WHERE source='chat'").fetchall():
        if r["type"] == "decision":
            text = r["summary"] or r["name"]
            if ASSISTANT_REQUEST.match(text) or re.search(r"\w\.(md|txt|py|json)\b|[/\\]", text):
                set_status(con, r["key"], "retracted")
                retracted += 1
                for p in con.execute("SELECT n.name FROM edges e JOIN nodes n ON n.key=e.src WHERE e.dst=? AND "
                                     "e.rel='decided'", (r["key"],)).fetchall():
                    _strike_filed(p["name"], text)
            continue
        if r["type"] in ("idea", "project"):
            continue
        if not _good_entity(r["name"]) or r["type"] in ("topic", "note"):
            con.execute("DELETE FROM edges WHERE src=? OR dst=?", (r["key"], r["key"]))
            con.execute("DELETE FROM nodes WHERE key=?", (r["key"],))
            removed += 1
    con.execute("DELETE FROM edges WHERE source='chat' AND (src NOT IN (SELECT key FROM nodes) OR dst NOT IN "
                "(SELECT key FROM nodes))")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cleanup_v3', ?)", (str(time.time()),))
    if own:
        con.commit()
        con.close()
    return {"removed": removed, "retracted": retracted}


def _good_entity(name):
    n = (name or "").strip()
    low = n.lower()
    return (3 <= len(n) <= 50 and len(n.split()) <= 5 and low not in STOP_NAMES and "/" not in n and "\\" not in n
            and not re.search(r"\.[a-z0-9]{1,4}$", low) and not re.fullmatch(r"[\d\s:/.-]+", n))


def extract_from_traces(since=None, settings=None, chat=None, max_turns=40):
    """Read recent user turns with the local model; add entities/relations; file decisions into project notes."""
    from . import learn
    s = settings or Settings.load()
    con = _db()
    last = con.execute("SELECT v FROM meta WHERE k='traces_until'").fetchone()
    since = since if since is not None else float(last["v"]) if last else time.time() - 86400
    rows = [r for r in learn.user_turns(since, limit=max_turns) if not str(r.get("session", "")).startswith(
        ("objective-", "routine-")) and len(r.get("request") or "") > 15][::-1]
    if not rows:
        con.close()
        return {"turns": 0}
    if chat is None:
        from . import ollama
        chat = lambda msgs: ollama.chat_json(msgs, schema=EXTRACT_SCHEMA, settings=s, timeout=120)  # noqa: E731
    known = [p["name"] for p in projects.all_projects(s)]
    added, filed, dropped = 0, 0, 0
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        msgs = "\n".join(f"- {r['request'][:400]}" for r in batch)
        said = msgs.lower()
        try:
            out = chat([{"role": "system", "content": EXTRACT_SYSTEM + f"\nKnown projects: {', '.join(known)}"},
                        {"role": "user", "content": msgs}]) or {}
        except Exception:
            continue
        accepted = set()
        for e in out.get("entities") or []:
            if not (isinstance(e, dict) and _good_entity(e.get("name")) and _said(e["name"], said)):
                dropped += 1
                continue
            typ = e.get("type") if e.get("type") in TYPES else "topic"
            proj = _match_project(e["name"], known, con) if typ in ("project", "topic") else None
            k = canonical(con, proj or e["name"])
            exists = con.execute("SELECT type FROM nodes WHERE key=?", (k,)).fetchone()
            if typ == "topic" and not exists:
                dropped += 1  # no new free-floating topics from small talk
                continue
            upsert_node(con, proj or e["name"], "project" if proj else typ, "", "chat",
                        source_ref=_ref(batch, e["name"]))
            accepted.add(k)
            added += 1
        for r in out.get("relations") or []:
            if not (isinstance(r, dict) and r.get("src") and r.get("dst")):
                continue
            ends = [_match_project(x, known, con) or x for x in (r["src"], r["dst"])]
            keys = [canonical(con, x) for x in ends]
            rel = str(r.get("rel") or "related_to").lower().replace(" ", "_")
            real = all(x in accepted or _match_project(n, known, con) or (con.execute(
                "SELECT type FROM nodes WHERE key=?", (x,)).fetchone() or {"type": "topic"})["type"] not in
                ("topic", "note") for x, n in zip(keys, ends))
            if rel in REL_OK and real and _said(r["src"], said) and _said(r["dst"], said):
                a, b = (upsert_node(con, n, "project" if _match_project(n, known, con) else "topic", "", "chat")
                        for n in ends)
                add_edge(con, a, rel, b, "chat")
            else:
                dropped += 1
        for d in out.get("decisions") or []:
            if not isinstance(d, dict) or not d.get("decision"):
                continue
            text, quote = d["decision"].strip(), (d.get("quote") or "").strip()
            words = _words(text)
            grounded = words and len(words & _words(said)) / len(words) >= 0.6
            if (not grounded or ASSISTANT_REQUEST.match(quote or text) or ASSISTANT_REQUEST.match(text)
                    or re.search(r"\w\.(md|txt|py|json)\b|[/\\]", text)):
                dropped += 1  # not in the user's words, or a request to the assistant rather than a choice
                continue
            proj = _match_project(d.get("project", ""), known, con)
            date = dt.date.today().isoformat()
            if str(d.get("status", "decided")).lower() != "decided":  # an idea, not a commitment
                node = upsert_node(con, f"idea: {text[:90]}", "idea", text, "chat", date, status="proposed",
                                   source_ref=_ref(batch, quote or text), quote=quote)
                if proj:
                    add_edge(con, upsert_node(con, proj, "project", "", "chat"), "considering", node, "chat")
                continue
            node = upsert_node(con, f"decision: {text[:90]}", "decision", text, "chat", date, status="current",
                               source_ref=_ref(batch, quote or text), quote=quote)
            if proj:
                pk = upsert_node(con, proj, "project", "", "chat")
                add_edge(con, pk, "decided", node, "chat")
                if d.get("replaces"):
                    supersede(con, pk, node, d["replaces"], proj, s)
                if record_decision(proj, text, s):
                    filed += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('traces_until', ?)", (str(rows[-1]["time"] + 0.001),))
    con.commit()
    con.close()
    return {"turns": len(rows), "entities": added, "decisions_filed": filed, "dropped": dropped}


def supersede(con, project_key, new_key, replaces, project_name, settings=None):
    """Mark the project's earlier decision that best matches `replaces` as superseded by new_key, and strike it
    through in the note. Needs a real match (half its words): never guesses which decision changed."""
    want = _words(replaces)
    best, score = None, 0.0
    for r in con.execute("SELECT n.key, n.summary FROM nodes n JOIN edges e ON e.dst=n.key WHERE e.src=? AND "
                         "e.rel='decided' AND n.type='decision' AND n.key != ? AND COALESCE(n.status,'current')="
                         "'current'", (project_key, new_key)):
        have = _words(r["summary"])
        sc = len(want & have) / max(1, len(want))
        if sc > score:
            best, score = r, sc
    if not best or score < 0.5:
        return None
    set_status(con, best["key"], "superseded", new_key)
    add_edge(con, new_key, "replaces", best["key"], "chat")
    _strike(project_name, best["summary"], settings)
    return best["key"]


def _strike(project, text, settings=None):
    """~~old decision~~ _(changed <date>)_ in the project's Decisions section."""
    try:
        cur = projects.sections(project, settings).get("Decisions", "")
    except Exception:
        return False
    want = _words(text)
    lines = cur.splitlines()
    for i, line in enumerate(lines):
        if "~~" in line or not want:
            continue
        if len(want & _words(line)) / len(want) >= 0.8:
            body = line.strip().lstrip("-* ").strip()
            lines[i] = f"- ~~{body}~~ _(changed {dt.date.today().isoformat()})_"
            projects._set_section(project, "Decisions", "\n".join(lines), settings)
            return True
    return False


def correct(name, action, value="", settings=None):
    """Fix the graph by hand. action: retract (wrong decision/idea/link: hidden from context, struck in the note),
    confirm (an idea becomes a decision and is filed), merge (this node is a duplicate of `value`), rename/update
    (new summary text)."""
    con = _db()
    try:
        k = canonical(con, name)
        node = con.execute("SELECT * FROM nodes WHERE key=?", (k,)).fetchone()
        if not node:
            hit = con.execute("SELECT * FROM nodes WHERE key LIKE ? ORDER BY last_seen DESC LIMIT 1",
                              (f"%{key(name)}%",)).fetchone()
            node, k = hit, hit["key"] if hit else None
        if not node:
            return {"error": f"'{name}' is not in the graph"}
        proj = con.execute("SELECT n.name FROM edges e JOIN nodes n ON n.key=e.src WHERE e.dst=? AND e.rel IN "
                           "('decided','considering') AND n.type='project'", (k,)).fetchone()
        if action == "retract":
            set_status(con, k, "retracted")
            if node["type"] == "decision" and proj:
                _strike(proj["name"], node["summary"] or node["name"], settings)
            out = {"retracted": node["name"]}
        elif action == "confirm":
            text = node["summary"] or node["name"].split(": ", 1)[-1]
            new = upsert_node(con, f"decision: {text[:90]}", "decision", text, "user", dt.date.today().isoformat(),
                              status="current", source_ref=f"confirmed by the user {dt.datetime.now():%Y-%m-%d %H:%M}")
            set_status(con, k, "confirmed", new)
            filed = False
            if proj:
                add_edge(con, canonical(con, proj["name"]), "decided", new, "user")
                filed = record_decision(proj["name"], text, settings)
            out = {"confirmed": text, "filed_in_project": filed}
        elif action == "merge":
            into = canonical(con, value)
            if not con.execute("SELECT 1 FROM nodes WHERE key=?", (into,)).fetchone() or into == k:
                return {"error": f"merge target '{value}' is not in the graph"}
            for col in ("src", "dst"):
                for e in con.execute(f"SELECT * FROM edges WHERE {col}=?", (k,)).fetchall():
                    s2, d2 = (into, e["dst"]) if col == "src" else (e["src"], into)
                    add_edge(con, s2, e["rel"], d2, e["source"])
            con.execute("DELETE FROM edges WHERE src=? OR dst=?", (k, k))
            con.execute("DELETE FROM nodes WHERE key=?", (k,))
            con.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?)", (k, into, "user"))
            out = {"merged": node["name"], "into": value}
        elif action in ("rename", "update"):
            con.execute("UPDATE nodes SET summary=?, updated=? WHERE key=?", (value[:400], time.time(), k))
            out = {"updated": node["name"]}
        else:
            return {"error": f"unknown action {action}"}
        con.commit()
        return out
    finally:
        con.close()


def _match_project(name, known, con=None):
    """A known project this name really refers to: same name, same spelling variant, the project's full name inside
    it ("the BIOBUZZ Robot project"), or one of its aliases. Never a fragment: "Claude" is not "Kalshi Claude Desk"."""
    n = key(name)
    if not n:
        return None
    for k in known:
        kk = key(k)
        if kk == n or _norm(kk) == _norm(n) or re.search(r"(?<![a-z0-9])" + re.escape(kk) + r"(?![a-z0-9])", n):
            return k
    if con is not None:
        row = con.execute("SELECT key FROM aliases WHERE alias=?", (n,)).fetchone()
        if row:
            return next((k for k in known if key(k) == row["key"]), None)
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
        "SELECT e.rel, n.key, n.name, n.type, n.date, n.summary, COALESCE(n.status,'current') AS status, n.source_ref, "
        "n.quote, 'out' AS dir FROM edges e JOIN nodes n ON n.key=e.dst WHERE e.src=? UNION ALL SELECT e.rel, n.key, "
        "n.name, n.type, n.date, n.summary, COALESCE(n.status,'current'), n.source_ref, n.quote, 'in' FROM edges e "
        "JOIN nodes n ON n.key=e.src WHERE e.dst=?", (k, k))]
    for r in out:
        if r["type"] == "decision":
            old = con.execute("SELECT n.summary FROM edges e JOIN nodes n ON n.key=e.dst WHERE e.src=? AND "
                              "e.rel='replaces'", (r["key"],)).fetchone()
            r["replaces"] = old["summary"] if old else None
    con.close()
    live = [r for r in out if r["status"] not in ("retracted", "superseded", "confirmed")]
    decisions = sorted([r for r in live if r["type"] == "decision"], key=lambda r: r["date"] or "", reverse=True)
    ideas = [r for r in live if r["type"] == "idea" and r["status"] == "proposed"]
    others = [r for r in live if r["type"] not in ("decision", "idea") or r["status"] != "proposed"
              and r["type"] != "decision"]
    return {"name": node["name"], "type": node["type"], "summary": node["summary"],
            "decisions": decisions[:6], "ideas": ideas[:4], "related": others[:limit],
            "changed": [r for r in out if r["type"] == "decision" and r["status"] == "superseded"][:4]}


def _names(con):
    rows = [(r["name"], r["key"]) for r in con.execute(
        "SELECT name, key FROM nodes WHERE type IN ('project','person','idea','objective','research','tool','org') "
        "AND length(key) >= 4 AND COALESCE(status,'current') NOT IN ('retracted','superseded')")]
    names = {k: n for n, k in rows}
    rows += [(names[r["key"]], r["alias"]) for r in con.execute("SELECT alias, key FROM aliases")
             if r["key"] in names and len(r["alias"]) >= 4]
    return rows


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
            lines.append(f"  decided{(' ' + d['date']) if d['date'] else ''}: {(d['summary'] or d['name'])[:160]}"
                         + (f" (changed from: {d['replaces'][:80]})" if d.get("replaces") else ""))
        for i in nb.get("ideas", [])[:2]:
            lines.append(f"  considering (not decided): {(i['summary'] or i['name'])[:140]}")
    lines.append("</knowledge_graph>")
    return "\n".join(lines) if len(lines) > 3 else ""


def stats():
    con = _db()
    by = {r["type"]: r["n"] for r in con.execute("SELECT type, COUNT(*) AS n FROM nodes GROUP BY type")}
    edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    con.close()
    return {"nodes": sum(by.values()), "by_type": by, "edges": edges}


def refresh(settings=None, vault_too=True):
    """The six-hourly graph pass (core job duty.graph): one-time cleanup, the vault part (skipped when the intake
    already did it), chat extraction with the local model, stats. Writes graph_last.json and returns it."""
    con = _db()
    if not con.execute("SELECT 1 FROM meta WHERE k='cleanup_v3'").fetchone():
        cleanup(con)
        con.commit()
    con.close()
    out = {"time": time.time(), "vault": build_from_vault(settings) if vault_too else "built by the knowledge intake"}
    try:
        out["chat"] = extract_from_traces(settings=settings)
    except Exception as e:  # local model down: the vault part still counts
        out["chat"] = {"error": f"{type(e).__name__}: {e}"}
    out["stats"] = stats()
    data_path("graph_last.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def main():
    """Command line: one full refresh. Prints nothing."""
    refresh()


MCP_TOOLS = [
    ({"name": "graph_query", "description": "What the user's knowledge graph knows about a person, project, idea or "
      "topic: how it connects to other things and the decisions made about it (dated).",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps(neighbors(a.get("name", "")) or {"error": "not in the graph"}, default=str)),
    ({"name": "graph_correct", "description": "Fix the user's knowledge graph when something in it is wrong: "
      "action retract (a wrong decision, idea or fact; also struck in the project note), confirm (an idea the user "
      "has now decided), merge (name is a duplicate of value), update (new description in value).",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "action": {"type": "string",
                     "enum": ["retract", "confirm", "merge", "update"]}, "value": {"type": "string"}},
                     "required": ["name", "action"]}},
     lambda a: json.dumps(correct(a["name"], a["action"], a.get("value", "")))),
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
        proj = _match_project(src, known, con)
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
