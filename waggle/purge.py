"""Purge: take what non-user turns left behind out of everything retrieval can reach, with an undo.

Development tests, canaries, benches and background agents once talked to the live Waggle, and what they said was
filed as if the user had said it: a line in a journal note, a "fact" in the profile, an entity in the graph, a chunk
in the memory index. Fixing the source does not remove the derived copies. This tool finds them:

  vault      lines (and the lines nested under them) that quote a turn that was not the user's own (five words in a
             row), or summarise one (almost all their distinctive words come from it), or contain a phrase you name
             with --phrase, or were filed from a fact that is purged. A line the user's own turns also state stays.
  facts      knowledge.db facts whose origin is not the user's, whose source turn is not the user's, or (facts from
             before sources were recorded) whose text matches a non-user turn; they get the status "purged".
  state      the digest's stored day summaries that carry such content (they are written again from the user's turns)
  graph      graph.db entities and relations sourced from such turns or matching them, and their aliases
  index      memory.db chunks of any note that is excluded from retrieval (Me/History, Archive, ...), of notes that
             no longer exist, or that no longer match their note's current text; the touched notes are re-embedded
             so no stale chunk remains.

A turn is the user's own by learn.is_user_turn: origin "user", or an old row without an origin that did not come from
the CLI or a background agent. Nothing is deleted without a copy: every change is written to an undo record under
the Waggle home (purge/<id>/manifest.json), never inside the vault, and `restore <id>` puts it back (a note the user
edited since is left alone and reported).

    python -m waggle.purge                         dry run: counts per store and a few examples
    python -m waggle.purge --apply                 do it
    python -m waggle.purge --phrase "some phrase"  also treat lines with this phrase as polluted (repeatable)
    python -m waggle.purge list | restore <id>
"""
import datetime as dt
import hashlib
import json
import re
import sys
import time

from . import kstore, learn, memory, vault
from .config import Settings, data_path

SHINGLE = 5  # words in a row that make a quote
MIN_TOKENS = 4  # distinctive words a line needs before it can be a summary
OVERLAP = 0.8  # share of those words a non-user turn must contain
_STOP = memory.STOP | {"user", "users", "asked", "found", "decided", "open", "turn", "turns", "wants", "have", "has",
                       "with", "that", "this", "from", "their", "they", "about", "like"}
_HEADER = re.compile(r"^\s*- \d{1,2}:\d{2} \(")  # a digest line "- 16:40 (telegram, 2 turns)" heading its turns
_CLI_HEADER = re.compile(r"^\s*- \d{1,2}:\d{2} \(cli\b", re.I)
_MARK = re.compile(r"<!--\s*waggle:[^>]*-->")
_SOURCE_REF = re.compile(r"chat (\S*) (\d{4}-\d\d-\d\d \d\d:\d\d)")
HISTORY = ("Me/History/", "Archive/", "Quarantine/", ".trash/", "_archive/")


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _grams(text, n=SHINGLE):
    w = _words(text)
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def _tokens(text):
    return {w for w in _words(text) if len(w) >= 4 and w not in _STOP and not w.isdigit()}


def _minute(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---- what counts as polluted ------------------------------------------------------------------------------

class Matcher:
    """Decides, for a piece of text, whether it quotes or summarises a turn that was not the user's own."""

    def __init__(self, rows, phrases=()):
        self.phrases = [p.strip().lower() for p in phrases if p and p.strip()]
        self.bad_ids, self.bad_keys, self.user_keys = set(), set(), set()
        self.bad_grams, self.user_grams, self.bad_sets, self.user_sets = set(), set(), [], []
        for r in rows:
            req, rep = r.get("request") or "", r.get("reply") or ""
            key = (r.get("session") or "", _minute(r["time"]))
            if learn.is_user_turn(r):
                self.user_keys.add(key)
                self.user_grams |= _grams(req) | _grams(rep)
                if len(req) <= 1500:
                    self.user_sets.append(_tokens(req))
                continue
            self.bad_ids.add(r["id"])
            self.bad_keys.add(key)
            self.bad_grams |= _grams(req) | _grams(rep)
            background = (r.get("session") or "").startswith(("objective-", "routine-"))
            if not background and len(req) <= 700:
                self.bad_sets.append(_tokens(req))
        self.bad_keys -= self.user_keys
        self.fact_texts = []  # texts of purged facts: their filed lines go too

    def match(self, text):
        """The reason this text is polluted, or None."""
        low = (text or "").lower()
        for p in self.phrases:
            if p in low:
                return f"contains {p!r}"
        for t in self.fact_texts:
            if t in low:
                return "filed from a purged fact"
        hit = _grams(text) & self.bad_grams
        if hit - self.user_grams:
            return "quotes a turn that was not the user's"
        toks = _tokens(text)
        if len(toks) >= MIN_TOKENS:
            if any(len(toks & b) / len(toks) >= OVERLAP for b in self.bad_sets if b):
                if not any(len(toks & u) / len(toks) >= OVERLAP for u in self.user_sets if u):
                    return "summarises a turn that was not the user's"
        return None

    def ref_is_bad(self, ref):
        m = _SOURCE_REF.search(ref or "")
        return bool(m) and (m.group(1), m.group(2)) in self.bad_keys


def _all_turns():
    from . import traces
    con = traces._db()
    con.row_factory = __import__("sqlite3").Row
    rows = [dict(r) for r in con.execute("SELECT id, time, session, platform, origin, request, reply FROM traces")]
    con.close()
    return rows


# ---- vault ------------------------------------------------------------------------------------------------

def _indent(line):
    return len(line) - len(line.lstrip(" \t"))


def flag_lines(text, matcher):
    """{line index: reason} for the lines of one note that are polluted."""
    lines = text.splitlines(keepends=True)
    start = 0
    if lines and lines[0].strip() == "---":  # frontmatter stays
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    flagged = {}
    for i in range(start, len(lines)):
        body = lines[i].strip()
        if not body or body.startswith("#") or _MARK.fullmatch(body):
            continue
        why = "a digest line for a CLI conversation" if _CLI_HEADER.match(lines[i]) else matcher.match(body)
        if why:
            flagged[i] = why
    for i in sorted(flagged):  # whatever hangs under a removed list item goes with it
        if not lines[i].lstrip().startswith(("-", "*")):
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].strip() and _indent(lines[j]) <= _indent(lines[i]):
                break
            if lines[j].strip():
                flagged.setdefault(j, "nested under a removed line")
    for i in range(start, len(lines)):  # a turn header left with no turns under it
        if i in flagged or not _HEADER.match(lines[i]):
            continue
        kids = []
        for j in range(i + 1, len(lines)):
            if lines[j].strip() and _indent(lines[j]) <= _indent(lines[i]):
                break
            if lines[j].strip():
                kids.append(j)
        if kids and all(k in flagged for k in kids):
            flagged[i] = "its turns were removed"
    return flagged


def _plan_vault(s, matcher):
    root = vault.root(s)
    plan = []
    for p in sorted(vault.notes(s)):
        rel = p.relative_to(root).as_posix()
        try:
            raw = p.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        text = raw
        flagged = flag_lines(text, matcher)
        if not flagged:
            continue
        lines = text.splitlines(keepends=True)
        new = "".join(l for i, l in enumerate(lines) if i not in flagged)
        plan.append({"path": rel, "lines": len(flagged), "before": raw, "after": new,
                     "examples": [lines[i].strip()[:90] for i in sorted(flagged)[:3]],
                     "reasons": sorted({flagged[i] for i in flagged})})
    return plan


# ---- facts, digest state, graph ---------------------------------------------------------------------------

def _plan_facts(matcher):
    con = kstore.db()
    rows = [dict(r) for r in con.execute("SELECT * FROM facts WHERE status!='purged'")]
    con.close()
    out = []
    for f in rows:
        origin = (f.get("origin") or "").lower()
        why = None
        if origin and origin not in ("user", "calendar", "mail"):
            why = f"origin {origin}"
        elif f.get("source_turn") is not None and f["source_turn"] in matcher.bad_ids:
            why = "its source turn was not the user's"
        elif f.get("source_turn") is None and origin in ("", "user") and f.get("source") not in ("calendar", "mail"):
            why = matcher.match(f["text"])
        if why:
            out.append({"id": f["id"], "status": f["status"], "note": f.get("note"), "text": f["text"], "why": why,
                        "kind": f["kind"]})
    return out


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _plan_state(matcher):
    days = kstore.get("digest.days", {})
    bad = [d for d, st in days.items() if st.get("distilled") and any(matcher.match(t) for t in
                                                                        _strings(st["distilled"]))]
    return {"days": bad, "old": {d: days[d] for d in bad}}


def _graph_con():
    import sqlite3
    con = sqlite3.connect(data_path("graph.db"), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _plan_graph(matcher):
    try:
        con = _graph_con()
        cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
        nodes = [dict(r) for r in con.execute("SELECT * FROM nodes")]
    except Exception:
        return {"nodes": [], "edges": [], "aliases": []}
    bad = []
    for n in nodes:
        texts = [n.get("name"), n.get("summary"), n.get("quote")] if "quote" in cols else [n.get("name"),
                                                                                           n.get("summary")]
        if matcher.ref_is_bad(n.get("source_ref")) or any(matcher.match(t) for t in texts if t):
            bad.append(n)
    keys = {n["key"] for n in bad}
    edges = [dict(r) for r in con.execute("SELECT * FROM edges")]
    edges = [e for e in edges if e["src"] in keys or e["dst"] in keys]
    aliases = [dict(r) for r in con.execute("SELECT * FROM aliases")]
    aliases = [a for a in aliases if a["key"] in keys]
    con.close()
    return {"nodes": bad, "edges": edges, "aliases": aliases}


# ---- memory index -----------------------------------------------------------------------------------------

def _plan_index(s, new_texts, matcher):
    """Rows of the index that must go: excluded or vanished notes, text that is no longer in the note (using the
    text it will have after the purge), text the matcher flags."""
    root = vault.root(s)
    con = memory._db()
    rows = con.execute("SELECT rowid, path, idx, text FROM chunks WHERE idx>=0").fetchall()
    markers = con.execute("SELECT path FROM chunks WHERE idx<0").fetchall()
    con.close()
    exists = {p.relative_to(root).as_posix(): p for p in vault.notes(s)}
    cache, stale = {}, {}

    def current(rel):
        if rel not in cache:
            text = new_texts.get(rel)
            if text is None:
                text = exists[rel].read_text(encoding="utf-8", errors="replace")
            cache[rel] = set(c for c in memory.chunk(f"[{rel}]\n{memory.clean(text)}") if memory._has_body(c))
        return cache[rel]

    for rowid, rel, idx, text in rows:
        if not memory.indexable(rel):
            why = "note is excluded from retrieval"
        elif rel not in exists:
            why = "note no longer exists"
        elif text not in current(rel):
            why = "no longer in its note"
        elif matcher.match(text):
            why = "matches a non-user turn"
        else:
            continue
        stale.setdefault(why, []).append(rowid)
    gone = [r[0] for r in markers if r[0] not in exists or not memory.indexable(r[0])]
    return {"rows": stale, "markers": gone}


def remaining(s, matcher):
    """What is still reachable after a purge, per store (all zero when it worked)."""
    plan = _plan_vault(s, matcher)
    index = _plan_index(s, {}, matcher)
    return {"vault_lines": sum(p["lines"] for p in plan), "facts": len(_plan_facts(matcher)),
            "graph_nodes": len(_plan_graph(matcher)["nodes"]), "state_days": len(_plan_state(matcher)["days"]),
            "index_chunks": sum(len(v) for v in index["rows"].values())}


# ---- the tool ---------------------------------------------------------------------------------------------

def purge_dir():
    d = data_path("purge")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def purge(settings=None, apply=False, phrases=(), embedder=None, now=None):
    """Dry run unless apply=True. Returns counts per store (and examples); with apply, also the undo id."""
    s = settings or Settings.load()
    now = now or time.time()
    matcher = Matcher(_all_turns(), phrases)
    facts = _plan_facts(matcher)
    matcher.fact_texts = [f["text"].lower() for f in facts if len(f["text"]) >= 12]
    vplan = _plan_vault(s, matcher)
    state = _plan_state(matcher)
    graph = _plan_graph(matcher)
    index = _plan_index(s, {p["path"]: p["after"] for p in vplan}, matcher)
    report = {"dry_run": not apply,
              "non_user_turns": len(matcher.bad_ids),
              "vault": {"notes": len(vplan), "lines": sum(p["lines"] for p in vplan),
                        "files": [{"path": p["path"], "lines": p["lines"], "reasons": p["reasons"],
                                   "examples": p["examples"]} for p in vplan]},
              "facts": {"count": len(facts), "examples": [f"{f['kind']}: {f['text'][:80]} ({f['why']})"
                                                         for f in facts[:5]]},
              "digest_state": {"days": len(state["days"])},
              "graph": {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
                        "aliases": len(graph["aliases"]),
                        "examples": [n["name"][:60] for n in graph["nodes"][:5]]},
              "index": {"stale_chunks": sum(len(v) for v in index["rows"].values()),
                        "by_reason": {k: len(v) for k, v in index["rows"].items()},
                        "notes_to_rebuild": len({p["path"] for p in vplan})}}
    if not apply:
        return report
    pid = "purge-" + dt.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    manifest = {"id": pid, "time": now, "phrases": list(phrases), "vault": [], "facts": facts, "state": state,
                "graph": graph}
    d = purge_dir() / pid
    d.mkdir(parents=True, exist_ok=True)
    for p in vplan:  # the copy first, then the change
        manifest["vault"].append({"path": p["path"], "before": p["before"], "after_sha": _sha(p["after"])})
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str),
                                     encoding="utf-8")
    for p in vplan:
        vault.safe_path(p["path"], s).write_bytes(p["after"].encode("utf-8"))
    con = kstore.db()
    for f in facts:
        con.execute("UPDATE facts SET status='purged', note=? WHERE id=?", (f"purged {pid}: {f['why']}", f["id"]))
    con.commit()
    con.close()
    if state["days"]:
        days = kstore.get("digest.days", {})
        for day in state["days"]:
            for k in ("distilled", "distilled_last", "filed", "written_last"):
                days[day].pop(k, None)  # rewritten from the user's turns on the next digest run
        kstore.put("digest.days", days)
    if graph["nodes"] or graph["edges"] or graph["aliases"]:
        con = _graph_con()
        for n in graph["nodes"]:
            con.execute("DELETE FROM nodes WHERE key=?", (n["key"],))
        for e in graph["edges"]:
            con.execute("DELETE FROM edges WHERE src=? AND rel=? AND dst=?", (e["src"], e["rel"], e["dst"]))
        for a in graph["aliases"]:
            con.execute("DELETE FROM aliases WHERE alias=?", (a["alias"],))
        con.commit()
        con.close()
    con = memory._db()
    for ids in index["rows"].values():
        con.executemany("DELETE FROM chunks WHERE rowid=?", [(i,) for i in ids])
    for path in index["markers"]:
        con.execute("DELETE FROM chunks WHERE path=?", (path,))
    for p in vplan:  # the touched notes are embedded again from their new text
        con.execute("DELETE FROM chunks WHERE path=?", (p["path"],))
    con.commit()
    con.close()
    report["index"]["notes_reembedded"] = memory.reindex(s, embedder=embedder)
    if any(f["kind"] in learn.EVENT_KINDS for f in facts):
        learn.render_upcoming(s)
    report["id"] = pid
    report["manifest"] = str(d / "manifest.json")
    report["remaining"] = remaining(s, matcher)
    learn.events.log("purge", id=pid, vault_lines=report["vault"]["lines"], facts=len(facts),
                     graph_nodes=len(graph["nodes"]), index_chunks=report["index"]["stale_chunks"])
    return report


def list_purges():
    out = []
    for m in sorted(purge_dir().glob("purge-*/manifest.json")):
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": data["id"], "time": data["time"], "notes": len(data.get("vault", [])),
                    "facts": len(data.get("facts", [])), "restored": bool(data.get("restored"))})
    return out


def restore(purge_id, settings=None, embedder=None):
    """Undo one purge. A note changed since the purge is left as it is and listed under `conflicts`."""
    s = settings or Settings.load()
    m = purge_dir() / purge_id / "manifest.json"
    if not m.exists():
        return {"error": f"no purge {purge_id!r}"}
    data = json.loads(m.read_text(encoding="utf-8"))
    if data.get("restored"):
        return {"error": "already restored"}
    out = {"id": purge_id, "notes": 0, "facts": 0, "graph_nodes": 0, "conflicts": []}
    for v in data["vault"]:
        p = vault.safe_path(v["path"], s)
        now = p.read_bytes().decode("utf-8", errors="replace") if p.exists() else ""
        if p.exists() and _sha(now) != v["after_sha"]:
            out["conflicts"].append(v["path"])
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(v["before"].encode("utf-8"))
        out["notes"] += 1
    con = kstore.db()
    for f in data["facts"]:
        con.execute("UPDATE facts SET status=?, note=? WHERE id=?", (f["status"], f.get("note"), f["id"]))
        out["facts"] += 1
    con.commit()
    con.close()
    if data["state"]["old"]:
        days = kstore.get("digest.days", {})
        days.update(data["state"]["old"])
        kstore.put("digest.days", days)
    g = data["graph"]
    if g["nodes"] or g["edges"] or g["aliases"]:
        con = _graph_con()
        for table, rows in (("nodes", g["nodes"]), ("edges", g["edges"]), ("aliases", g["aliases"])):
            for r in rows:
                cols = ",".join(r)
                con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({','.join('?' * len(r))})",
                            list(r.values()))
        con.commit()
        con.close()
        out["graph_nodes"] = len(g["nodes"])
    out["notes_reembedded"] = memory.reindex(s, embedder=embedder)
    if any(f["kind"] in learn.EVENT_KINDS for f in data["facts"]):
        learn.render_upcoming(s)
    data["restored"] = time.time()
    m.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args[:1] == ["list"]:
        print(json.dumps(list_purges(), indent=1))
        return 0
    if args[:1] == ["restore"] and len(args) > 1:
        print(json.dumps(restore(args[1]), indent=1))
        return 0
    phrases = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--phrase"]
    print(json.dumps(purge(apply="--apply" in args, phrases=phrases), indent=1, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
