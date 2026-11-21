"""Activity model: what the owner is working on right now, built by rule from what is already on this machine.

Inputs, all local and cheap:
  repositories   the list the knowledge intake keeps (kstore "ingest.repos": path, project note) plus the repos
                 under watch_roots and extra_repos. Commit times come from each repository's HEAD log
                 (.git/logs/HEAD, and .git/worktrees/*/logs/HEAD): a file read, done only when its size or mtime
                 moved since the last pass. No git or GitHub process is started here.
  Claude Code    the session logs under ~/.claude/projects/, read locally and incrementally (byte offset per file).
                 Only derived facts are kept, in autonomy.db: session id, time span, the owner's turn times with up
                 to eight keywords each, the number of distinct files touched per workstream (paths are hashed),
                 normalised command shapes ("git commit", "python -m pytest") and the session's one-line title.
                 Message text, tool results and paths are never stored. Sessions started by `claude -p`
                 (entrypoint sdk-cli: Waggle's own model calls and test runs) are machine work and are skipped.
  chat turns     the owner's own Waggle turns (learn.user_turns: origin 'user' only; tests, canaries, benches and
                 agents count for nothing), matched to a workstream by its name.
  upcoming       dated items from the knowledge store (learn.facts, kinds event/deadline/reply).

Ranking, no model:  score(ws) = sum over events of w_kind * exp(-age / tau_kind)  +  sum of 2 / (days_to_due + 1)
  owner turn in Claude Code 1.0 (tau 2 d), owner chat turn 1.0 (2 d), file touched 0.2 (2 d, at most 30 per session),
  file touched by a sub-agent 0.1 (2 d), commit 0.5 (3 d). Events older than WINDOW_DAYS are ignored.

refresh() writes the ranked list to runtime.db meta "activity.workstreams" (and returns it); the core job
autonomy.activity calls it every 30 minutes with catch-up "skip". workstreams() reads the last snapshot.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from . import autonomy_store as store_
from .config import Settings

WINDOW_DAYS = 14
TOP_N = 12
MIN_SCORE = 0.05
MAX_EDITS_PER_SESSION = 30
WEIGHTS = {"cc_turn": (1.0, 2.0), "chat_turn": (1.0, 2.0), "edit": (0.2, 2.0), "agent_edit": (0.1, 2.0),
           "commit": (0.5, 3.0)}
DEADLINE_WEIGHT = 2.0
DEADLINE_DAYS = 14
META_KEY = "activity.workstreams"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SKIP_DIRS = ("appdata/local/temp", "/.claude/", "/.waggle", "/.hermes", "/tmp/", "site-packages")
STOP = set("""this that with have from what when your will would could should there their they them then than into
about just like make made also some more need want here were been being does done dont didn cant its
please thanks thank okay yeah sure going really still each every only other after before because which while where
these those over under again very much many most such same both well onto upon youre yours mine ours
lets give take tell know think look looks keep kept used using file files code""".split())


# ---- helpers ---------------------------------------------------------------------------------------------------

def _norm(p):
    return str(p).replace("\\", "/").rstrip("/").lower()


def _hash(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def _ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def keywords(text, n=8):
    """Up to n content words of an owner's message (a derived fact for grouping; the text itself is not kept)."""
    words = [w for w in re.findall(r"[a-z][a-z0-9]{3,}", (text or "").lower()) if w not in STOP]
    counts = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    out = []
    for w in sorted(counts, key=lambda w: -counts[w]):
        out.append(w)
        if len(out) >= n:
            break
    return out


READ_ONLY = {"ls", "cat", "head", "tail", "grep", "rg", "find", "echo", "pwd", "wc", "stat", "sed", "awk", "sort",
             "uniq", "dir", "type", "which", "where", "du", "date", "sleep", "cd", "true", "test", "printf",
             "git status", "git log", "git diff", "git show", "git branch", "git rev-parse", "git remote",
             "git ls-files", "get-content", "get-childitem", "select-string", "test-path", "write-output",
             "select-object", "measure-object", "get-item", "get-date", "resolve-path", "python -c", "set-location",
             "cut", "tr", "xargs", "jq", "basename", "dirname", "realpath", "diff", "cmp", "md5sum", "sha256sum",
             "file", "less", "more", "tree", "nl", "column", "paste", "join", "comm", "xxd", "od", "env", "id",
             "whoami", "hostname", "uname", "ps", "tasklist", "netstat", "findstr", "get-process", "get-service",
             "get-command", "get-scheduledtask", "format-list", "format-table", "out-string", "sort-object",
             "where-object", "foreach-object", "convertfrom-json"}
WRAPPERS = ("timeout", "time", "sudo", "&", "env", "nohup")
TOOLS_WITH_SUBCOMMANDS = ("git", "gh", "npm", "npx", "pip", "node", "cargo", "dotnet", "docker", "hermes", "claude",
                          "uv")
OPTS_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "-R", "--repo", "--prefix")


QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.S)
SHELL_WORDS = {"for", "do", "done", "while", "until", "if", "then", "else", "elif", "fi", "case", "esac",
               "function", "{", "}", "[", "[[", "export", "set", "source", ".", "read", "local", "return", "exit",
               "break", "continue", "true", "false", ":", "wait", "trap", "shift", "eval", "foreach", "%", "_"}


def _segments(cmd):
    cmd = (cmd or "").split("<<", 1)[0]
    # a quoted message, path or inline script is one token, whatever lines or separators it holds
    cmd = QUOTED.sub(lambda m: re.sub(r"[\s;&|]+", "_", m.group(0)[1:-1]) or "_", cmd)
    parts = re.split(r"&&|\|\||;|\||\n", cmd)
    return [p.strip() for p in parts if p.strip()]


def _segment_shape(seg):
    toks = seg.replace("(", " ").replace(")", " ").split()
    while toks and toks[0] in ("do", "then", "else", "if", "while", "until", "!", "{", "elif"):
        toks = toks[1:]  # 'do git push' runs git push; 'for x in ...' itself is dropped below
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in WRAPPERS
                    or re.fullmatch(r"\d+[smh]?", toks[0])):
        toks = toks[1:]
    if not toks:
        return ""
    prog = toks[0].strip("'\"&").replace("\\", "/").rsplit("/", 1)[-1].lower()
    prog = re.sub(r"\.(exe|cmd|bat)$", "", prog)
    if prog in SHELL_WORDS or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", prog):
        return ""  # shell syntax or not a program at all
    if re.fullmatch(r"(python3?|py)(\.?\d+)*", prog):
        prog = "python"
    rest = toks[1:]
    if prog == "python":
        if "-m" in rest:
            i = rest.index("-m")
            return f"python -m {rest[i + 1].strip(chr(39) + chr(34))}" if i + 1 < len(rest) else "python -m"
        if "-c" in rest:
            return "python -c"
        script = next((t for t in rest if t.endswith(".py")), "")
        return f"python {script.replace(chr(92), '/').rsplit('/', 1)[-1]}" if script else "python"
    if prog in TOOLS_WITH_SUBCOMMANDS:
        subs, skip_next = [], False
        for t in rest:
            if skip_next:
                skip_next = False
                continue
            if t in OPTS_WITH_VALUE:
                skip_next = True
                continue
            if (t.startswith("-") or "/" in t or "\\" in t or "=" in t or t.startswith(("'", '"'))
                    or re.fullmatch(r"\d*[<>].*|\d+", t)):  # options, paths, literals, redirections
                continue
            subs.append(t.lower())
            if len(subs) >= (2 if prog == "gh" else 1):
                break
        return " ".join([prog, *subs])
    return prog


def command_shape(cmd):
    """A shell command reduced to its programs and subcommands: 'cd x && git add -A && git commit -m "..."'
    -> 'git add > git commit'. Paths, messages and other literals are dropped."""
    shapes = []
    for seg in _segments(cmd)[:6]:
        sh = _segment_shape(seg)
        if sh and sh not in ("cd", "set-location") and (not shapes or shapes[-1] != sh):
            shapes.append(sh)
    return " > ".join(shapes[:4])


def is_read_only(shape):
    return all(part in READ_ONLY for part in shape.split(" > ")) if shape else True


# ---- workstream identity ----------------------------------------------------------------------------------------

class Resolver:
    """Path -> workstream key (a repository's folder name). Worktrees resolve to their main repository; paths in
    temp folders and Waggle's or Claude's own homes resolve to nothing."""

    def __init__(self, settings=None, repos=None):
        s = settings or Settings.load()
        self.roots = [_norm(r) for r in s.watch_roots]
        self.root_names = {r.rsplit("/", 1)[-1] for r in self.roots}
        self.repos = {}
        self.names = {}
        for path, note in (repos if repos is not None else _known_repos(s)).items():
            key = Path(path).name.lower()
            self.repos[_norm(path)] = key
            if note or key not in self.names:
                self.names[key] = note or key
        self._cache = {}

    @staticmethod
    def _git_owner(d):
        """The repository key for a folder that holds .git (folder or worktree file), or None."""
        g = Path(d) / ".git"
        try:
            if g.is_dir():
                return Path(d).name.lower()
            if g.is_file():
                m = re.search(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace"))
                if m:
                    gd = _norm(m.group(1).strip())
                    if "/.git/worktrees/" in gd:
                        return gd.split("/.git/worktrees/")[0].rsplit("/", 1)[-1]
                return Path(d).name.lower()
        except OSError:
            return None
        return None

    def __call__(self, path):
        if not path:
            return None
        p = _norm(path)
        if not re.match(r"^([a-z]:/|/)", p):
            return None
        if p in self._cache:
            return self._cache[p]
        best = None
        for rp, key in self.repos.items():
            if (p == rp or p.startswith(rp + "/")) and (best is None or len(rp) > len(best[0])):
                best = (rp, key)
        out = best[1] if best else None
        inside_root = any(p.startswith(r + "/") for r in self.roots)
        if out is None and not inside_root and any(x in p + "/" for x in SKIP_DIRS):
            self._cache[p] = None  # temp folders, Waggle's and Claude's own homes: not the owner's projects
            return None
        if out is None:
            cur = Path(path)
            for _ in range(8):
                if _norm(cur) in self.roots or cur.parent == cur:
                    break
                if cur.is_dir():
                    out = self._git_owner(cur)
                    if out:
                        break
                cur = cur.parent
        if out is None:
            for r in self.roots:
                if p.startswith(r + "/"):
                    rest = p[len(r) + 1:]
                    out = rest.split("/", 1)[0] if "/" in rest else None  # a loose file in the root says nothing
                    break
        if out in self.root_names:
            out = None
        self._cache[p] = out
        return out

    def is_generic(self, cwd):
        """A working folder that holds many projects (a watch root, the home folder): says nothing by itself."""
        p = _norm(cwd or "")
        return not p or p in self.roots or p == _norm(Path.home())


def _known_repos(s):
    """{repo path: project note name or ''} from the knowledge intake's state, plus the repos on disk."""
    out = {}
    try:
        from . import kstore
        for path, info in (kstore.get("ingest.repos", {}) or {}).items():
            out[path] = (info or {}).get("note") or ""
    except Exception:  # noqa: BLE001 - no knowledge store yet
        pass
    try:
        from . import ingest
        for p in ingest.discover_repos(s):
            out.setdefault(str(p), "")
    except Exception:  # noqa: BLE001
        pass
    return out


# ---- Claude Code session logs -----------------------------------------------------------------------------------

def claude_projects_dir():
    return Path(os.environ.get("WAGGLE_CLAUDE_PROJECTS") or Path.home() / ".claude" / "projects")


def _texts(content):
    if isinstance(content, str):
        return [content]
    out = []
    for part in content or []:
        if isinstance(part, dict):
            if part.get("type") == "tool_result":
                return []
            if part.get("type") == "text":
                out.append(part.get("text") or "")
    return out


def owner_text(d):
    """The owner's typed message in a log line, or None (tool results, sub-agents, injected notices, -p runs)."""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("entrypoint") == "sdk-cli":
        return None
    if d.get("userType") not in (None, "external"):
        return None
    text = "\n".join(t for t in _texts((d.get("message") or {}).get("content")) if t).strip()
    if not text or text.startswith(("<", "Caveat:", "[Request interrupted")):
        return None
    return text


def _new_data(agent):
    return {"session": "", "agent": agent, "machine": False, "title": "", "custom": False, "first": None,
            "last": None, "turns": [], "edits": [], "files": {}, "cmds": []}


def _absorb(data, d, resolve, counted):
    """Fold one parsed log line into the file's derived facts."""
    typ = d.get("type")
    if typ == "custom-title" and d.get("customTitle"):
        data["title"], data["custom"] = str(d["customTitle"])[:80], True
        return
    if typ == "ai-title" and d.get("aiTitle") and not data["custom"]:
        data["title"] = str(d["aiTitle"])[:80]
        return
    if typ not in ("user", "assistant"):
        return
    if d.get("entrypoint") == "sdk-cli":
        data["machine"] = True
        return
    ts = _ts(d.get("timestamp"))
    if ts is None:
        return
    data["session"] = data["session"] or d.get("sessionId") or ""
    data["first"] = ts if data["first"] is None else min(data["first"], ts)
    data["last"] = ts if data["last"] is None else max(data["last"], ts)
    cwd = d.get("cwd") or ""
    if typ == "user":
        text = owner_text(d)
        if text is not None:
            ws = None if resolve.is_generic(cwd) else resolve(cwd)
            data["turns"].append([ts, ws, keywords(text) if len(text) <= 600 else [], len(text)])
        return
    for part in (d.get("message") or {}).get("content") or []:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        name, inp = part.get("name"), part.get("input") or {}
        if not isinstance(inp, dict):
            continue
        if name in EDIT_TOOLS:
            fp = inp.get("file_path") or inp.get("notebook_path") or ""
            ws = resolve(fp)
            if ws:
                data["edits"].append([ts, ws])
                h = _hash(_norm(fp))
                if h not in counted.setdefault(ws, set()):
                    counted[ws].add(h)
                    data["files"].setdefault(ws, []).append(h)
        elif name in SHELL_TOOLS:
            cmd = str(inp.get("command") or "")
            shape = command_shape(cmd)
            if not shape:
                continue
            ws = None
            for p in re.findall(r"[A-Za-z]:[\\/][^\s\"'&|;]+|/[a-z]/[^\s\"'&|;]+", cmd)[:3]:
                ws = resolve(re.sub(r"^/([a-z])/", r"\1:/", p))
                if ws:
                    break
            if not ws and not resolve.is_generic(cwd):
                ws = resolve(cwd)
            data["cmds"].append([ts, shape, ws])


QUICK = re.compile(r'"type":\s?"(user|assistant|ai-title|custom-title)"')
TOOL_RESULT = re.compile(r'"type":\s?"tool_result"')
TEXT_PART = re.compile(r'"type":\s?"text"')
TOOL_USE = re.compile(r'"type":\s?"tool_use"')


def read_session_file(path, cached=None, resolve=None):
    """Derived facts of one session log, continuing from the cached byte offset when the file only grew.
    Returns (data, offset, stat)."""
    path = Path(path)
    st = path.stat()
    resolve = resolve or Resolver()
    data, offset = None, 0
    if cached and cached["size"] <= st.st_size and cached["offset"] <= st.st_size:
        data, offset = store_.loads(cached["data"]), cached["offset"]
    if data is None:
        data, offset = _new_data("subagents" in path.parts), 0
    if offset == st.st_size:
        return data, offset, st
    counted = {ws: set(hs) for ws, hs in data["files"].items()}
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # a line still being written: read it next time
            offset += len(raw)
            line = raw.decode("utf-8", "replace")
            m = QUICK.search(line)
            if not m:
                continue
            if m.group(1) == "user" and TOOL_RESULT.search(line) and not TEXT_PART.search(line):
                continue  # a tool result: never the owner's words, and the biggest lines in the log
            if m.group(1) == "assistant" and not TOOL_USE.search(line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                _absorb(data, d, resolve, counted)
    return data, offset, st


def scan_sessions(settings=None, root=None, resolve=None, now=None):
    """Read the new lines of every session log touched within the window; returns each file's derived facts."""
    s = settings or Settings.load()
    now = store_.now_or(now)
    root = Path(root or claude_projects_dir())
    resolve = resolve or Resolver(s)
    since = now - WINDOW_DAYS * 86400
    out = []
    if not root.is_dir():
        return out
    con = store_.db()
    try:
        cache = {r["path"]: dict(r) for r in con.execute("SELECT * FROM cc_files")}
        seen = set()
        for f in root.rglob("*.jsonl"):
            key = str(f)
            seen.add(key)
            try:
                st = f.stat()
            except OSError:
                continue
            c = cache.get(key)
            if st.st_mtime < since and not c:
                continue
            if c and c["mtime"] == st.st_mtime and c["size"] == st.st_size:
                data = store_.loads(c["data"])
            else:
                try:
                    data, offset, st = read_session_file(f, c, resolve)
                except OSError:
                    continue
                con.execute("INSERT OR REPLACE INTO cc_files(path, size, mtime, offset, data, updated) "
                            "VALUES(?,?,?,?,?,?)", (key, st.st_size, st.st_mtime, offset, store_.dumps(data), now))
            if data and not data.get("machine") and (data.get("last") or 0) >= since:
                out.append(data)
        for gone in set(cache) - seen:
            con.execute("DELETE FROM cc_files WHERE path=?", (gone,))
        con.commit()
    finally:
        con.close()
    return out


# ---- repositories: commit times from the HEAD log -----------------------------------------------------------

def _head_logs(repo):
    g = Path(repo) / ".git"
    logs = [g / "logs" / "HEAD"]
    wt = g / "worktrees"
    if wt.is_dir():
        logs += [p / "logs" / "HEAD" for p in wt.iterdir()]
    return [p for p in logs if p.is_file()]


def commit_times(repos, now=None):
    """{repo key: [commit epoch times in the window]} from HEAD logs; a log is re-read only when it moved."""
    now = store_.now_or(now)
    since = now - WINDOW_DAYS * 86400
    out = {}
    con = store_.db()
    try:
        for path, key in repos.items():
            times = []
            for log in _head_logs(path):
                st = log.stat()
                row = con.execute("SELECT * FROM reflog WHERE path=?", (str(log),)).fetchone()
                if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                    got = store_.loads(row["data"], [])
                else:
                    got = []
                    with open(log, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            head, _, msg = line.partition("\t")
                            m = re.search(r"\s(\d{9,11})\s[+-]\d{4}$", head.strip())
                            if m and msg.startswith("commit"):
                                got.append(float(m.group(1)))
                    got = [t for t in got if t >= now - 30 * 86400][-500:]
                    con.execute("INSERT OR REPLACE INTO reflog(path, mtime, size, data, updated) VALUES(?,?,?,?,?)",
                                (str(log), st.st_mtime, st.st_size, store_.dumps(got), now))
                times += [t for t in got if t >= since]
            if times:
                out.setdefault(key, []).extend(times)
        con.commit()
    finally:
        con.close()
    return out


# ---- the model --------------------------------------------------------------------------------------------------

def _decay(kind, age_s):
    w, tau_d = WEIGHTS[kind]
    return w * math.exp(-max(age_s, 0) / (tau_d * 86400))


def match_ws(text, labels):
    """The workstream whose name (or project note name) appears in the text as a word, or None."""
    low = (text or "").lower()
    for key, names in labels.items():
        for lab in names:
            if lab and len(lab) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(lab) + r"(?![a-z0-9])", low):
                return key
    return None


def build(sessions, commits=None, chat_turns=None, upcoming=None, names=None, now=None):
    """Rank workstreams from derived facts (pure, for tests). sessions: read_session_file outputs; commits:
    {ws: [times]}; chat_turns: the owner's turn rows ({time, request}); upcoming: [{date, text}]; names: {ws: note}."""
    now = store_.now_or(now)
    since = now - WINDOW_DAYS * 86400
    week = now - 7 * 86400
    ws = {}

    def entry(key):
        return ws.setdefault(key, {"key": key, "name": (names or {}).get(key) or key, "score": 0.0, "last": 0.0,
                                   "first": None, "evidence": {"cc_turns_7d": 0, "sessions_7d": set(),
                                                               "files_touched_7d": 0, "commits_7d": 0,
                                                               "chat_turns_7d": 0, "deadlines_14d": 0},
                                   "topic": "", "topic_ts": 0.0, "next_due": None})

    def hit(key, kind, t, session=None):
        if not key or t is None or t < since or t > now + 300:
            return False
        e = entry(key)
        e["score"] += _decay(kind, now - t)
        e["last"] = max(e["last"], t)
        e["first"] = t if e["first"] is None else min(e["first"], t)
        if session and t >= week:
            e["evidence"]["sessions_7d"].add(session)
        return True

    by_session = {}
    for d in sessions:
        by_session.setdefault(d.get("session") or str(id(d)), []).append(d)
    for sid, files in by_session.items():
        edits = sorted((e for d in files for e in d.get("edits", [])), key=lambda e: e[0])
        turns = sorted((t for d in files if not d.get("agent") for t in d.get("turns", [])), key=lambda t: t[0])
        for d in files:
            per_ws = {}
            kind = "agent_edit" if d.get("agent") else "edit"
            for t, key in d.get("edits", []):
                per_ws[key] = per_ws.get(key, 0) + 1
                if per_ws[key] <= MAX_EDITS_PER_SESSION:
                    hit(key, kind, t, sid)
            if (d.get("last") or 0) >= week:
                for key, hs in d.get("files", {}).items():
                    if key in ws:
                        ws[key]["evidence"]["files_touched_7d"] += len(hs)
        for i, (t, key, _kw, _n) in enumerate(turns):
            if not key:  # a turn typed in a folder of many projects belongs to what was touched after it
                end = turns[i + 1][0] if i + 1 < len(turns) else t + 2 * 3600
                votes = {}
                for et, ek in edits:
                    if t <= et < end:
                        votes[ek] = votes.get(ek, 0) + 1
                key = max(votes, key=votes.get) if votes else None
            if hit(key, "cc_turn", t, sid) and t >= week:
                ws[key]["evidence"]["cc_turns_7d"] += 1
        touched = {k for d in files for _, k in d.get("edits", [])} | {t[1] for t in turns if t[1]}
        for d in files:
            if d.get("title") and not d.get("agent"):
                for key in touched:
                    if key in ws and (d.get("last") or 0) > ws[key]["topic_ts"]:
                        ws[key]["topic"], ws[key]["topic_ts"] = d["title"], d.get("last") or 0
    for key, times in (commits or {}).items():
        for t in times:
            if hit(key, "commit", t) and t >= week:
                ws[key]["evidence"]["commits_7d"] += 1
    labels = {k: {k, k.replace("-", " "), k.replace("_", " "), str(v.get("name", "")).lower()}
              for k, v in ws.items()}
    for k, note in (names or {}).items():
        labels.setdefault(k, {k, k.replace("-", " ")}).add(str(note).lower())
    for row in chat_turns or []:
        key = match_ws(row.get("request"), labels)
        t = row.get("time")
        if hit(key, "chat_turn", t) and t >= week:
            ws[key]["evidence"]["chat_turns_7d"] += 1
    today = dt.date.fromtimestamp(now)
    for item in upcoming or []:
        try:
            due = dt.date.fromisoformat(str(item.get("date"))[:10])
        except ValueError:
            continue
        days = (due - today).days
        if days < 0 or days > DEADLINE_DAYS:
            continue
        e = entry(match_ws(item.get("text"), labels) or "deadlines")
        e["score"] += DEADLINE_WEIGHT / (days + 1)
        e["evidence"]["deadlines_14d"] += 1
        if e["next_due"] is None or due.isoformat() < e["next_due"]:
            e["next_due"] = due.isoformat()
    out = []
    for e in ws.values():
        e["evidence"]["sessions_7d"] = len(e["evidence"]["sessions_7d"])
        e.pop("topic_ts", None)
        if e["score"] < MIN_SCORE:
            continue
        e["score"] = round(e["score"], 3)
        out.append(e)
    out.sort(key=lambda e: (-e["score"], -e["last"]))
    return out[:TOP_N]


def _upcoming():
    try:
        from . import learn
        return [{"date": f.get("date"), "text": f.get("text")} for f in learn.facts("active", learn.EVENT_KINDS)]
    except Exception:  # noqa: BLE001 - no knowledge store
        return []


def chat_turns(since):
    """The owner's own Waggle turns since `since` (origin 'user' only)."""
    try:
        from . import learn
        return [{"time": r["time"], "request": r.get("request") or ""} for r in learn.user_turns(since)]
    except Exception:  # noqa: BLE001 - no traces yet
        return []


def refresh(settings=None, now=None, claude_dir=None, repos=None, save=True):
    """Rebuild the ranked workstreams from every source and store them. Returns {time, workstreams, stats}."""
    s = settings or Settings.load()
    now = store_.now_or(now)
    t0 = time.time()
    known = repos if repos is not None else _known_repos(s)
    resolve = Resolver(s, known)
    sessions = scan_sessions(s, claude_dir, resolve, now=now)
    commits = commit_times({p: Path(p).name.lower() for p in known}, now=now)
    chats = chat_turns(now - WINDOW_DAYS * 86400)
    up = _upcoming()
    ranked = build(sessions, commits, chats, up, resolve.names, now)
    stats = {"sessions": len({d.get("session") for d in sessions}), "session_files": len(sessions),
             "repos": len(known), "chat_turns": len(chats), "upcoming": len(up), "workstreams": len(ranked),
             "seconds": round(time.time() - t0, 2)}
    snap = {"time": now, "workstreams": ranked, "stats": stats}
    if save:
        try:
            from waggle.core import store
            store.set_meta(META_KEY, json.dumps(snap, default=str))
        except Exception:  # noqa: BLE001 - runtime store unavailable: the result is still returned
            stats["saved"] = False
    return snap


def workstreams():
    """The last stored snapshot ({time: None, workstreams: []} when none)."""
    try:
        from waggle.core import store
        raw = store.get_meta(META_KEY)
    except Exception:  # noqa: BLE001
        raw = None
    return json.loads(raw) if raw else {"time": None, "workstreams": [], "stats": {}}


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    snap = refresh(save="--no-save" not in argv)
    for i, w in enumerate(snap["workstreams"], 1):
        last = dt.datetime.fromtimestamp(w["last"]).strftime("%m-%d %H:%M") if w["last"] else "-"
        print(f"{i:2}. {w['name'][:30]:30} score {w['score']:6.2f}  last {last}  {w['evidence']}")
    print(json.dumps(snap["stats"]))


if __name__ == "__main__":
    main()
