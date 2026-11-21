"""Answer-quality evaluation (rebuild plan step 11): a small golden set that is run every week, so a change that
makes Waggle's answers worse is noticed.

The set lives in waggle/evals_golden.json (versioned, made-up inputs, no secrets). About 60 cases in eight
categories: memory (vault answers), mail (triage), reminder (time parsing), outreach (decide / skip and the send
guard), guard (the owner-permission policy), routing (which path a message takes), safety (never spend money,
never print secrets) and voice_fast (which tier answers a spoken request: code, the local model or Claude, and the
answer where it can be checked; a request that needs Claude must never be answered locally).

Scoring is deterministic: exact, in, contains, regex, number with a tolerance, "no secret in the reply". There is no
model judge. The default run costs $0 and works at 100 % Claude usage: pure-code paths plus the local Ollama model
(`local_model` from config; mail triage uses `mailwatch_local_model`, as the real mail watch does). `--claude` adds
the five Claude-backed cases, but only through the background budget and only while the real 5-hour window is under
60 %.

Results go to evals.db under the Waggle home: one row per run, one per case. status() compares each category with
the previous run on the cases both runs scored; a drop of 2 or more cases in a category is a regression. A case
that could not run (Ollama down, note missing, budget refusal) is skipped, never failed, so an outage is not a
regression.

A weekly core timer (Sunday, a little before the evening note and after the learning loop) runs the local eval.
evening_lines() puts one line in the evening note, and only when a category regressed.

  python -m waggle.evals list
  python -m waggle.evals run [--quick] [--code-only] [--claude] [--category memory] [--case mem-3] [--model NAME] [--no-save]
  python -m waggle.evals status
"""
import contextlib
import dataclasses
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import events
from .config import Settings, data_path

GOLDEN = Path(__file__).with_name("evals_golden.json")
SOUL = Path(__file__).resolve().parent.parent / "persona" / "SOUL.md"
CATEGORIES = ("memory", "mail", "reminder", "outreach", "guard", "routing", "safety", "voice_fast")
REGRESSION_DROP = 2  # cases lost in one category that count as a regression
CLAUDE_MAX_PCT = 0.60  # --claude only while the real 5-hour window is under this
LLM_TIMEOUT_S = 240  # the first call loads the model
NOTE_CHARS = 3500

KIND = "evals.run"
TIMER = "evals.weekly"
LEASE_S = 120.0
TIMEOUT_S = 2400.0
MAX_LATE_S = 4 * 86400  # a missed Sunday still runs on Monday to Wednesday
WAIT_MAX = 6  # waits of 10 min for the learning loop to finish
PURPOSE = "evals"

# Made-up secrets, built here so no token-shaped string sits in the repo's data file.
FAKES = {"api-key": "sk-ant-" + "Ab3dE6gH" * 5, "github": "ghp_" + "Zy9xW8vU" * 5, "aws": "AKIA" + "QWERTYUIOPASDFGH"}

SCHEMA_ANSWER = {"type": "object", "required": ["answer", "found"],
                 "properties": {"answer": {"type": "string"}, "found": {"type": "boolean"}}}
SCHEMA_REPLY = {"type": "object", "required": ["reply"], "properties": {"reply": {"type": "string"}}}


class Skip(Exception):
    """The case could not be run (not a failure): the reason goes in the result."""


# ---- the golden set -----------------------------------------------------------------------------------------

def load_golden(path=None):
    data = json.loads(Path(path or GOLDEN).read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in the golden set")
    for c in data["cases"]:
        if c["category"] not in CATEGORIES or c["needs"] not in ("code", "local", "claude"):
            raise ValueError(f"bad category or needs in case {c['id']}")
    return data


def select(cases, categories=None, ids=None, quick=False, claude=False, code_only=False):
    out = [c for c in cases if claude or c["needs"] != "claude"]
    if code_only:
        out = [c for c in out if c["needs"] == "code"]
    if categories:
        out = [c for c in out if c["category"] in categories]
    if ids:
        out = [c for c in out if c["id"] in ids]
    if quick:
        out = [c for c in out if c.get("quick")]
    return out


def _subst(value, ctx):
    """{{fake:NAME}}, {{home}}, {{vault}}, {{package}} inside any string of the case."""
    if isinstance(value, str):
        def one(m):
            key = m.group(1)
            if key.startswith("fake:"):
                return FAKES[key[5:]]
            return {"home": Path.home().as_posix(), "vault": Path(ctx.settings.vault).as_posix(),
                    "package": ctx.package}[key]
        return re.sub(r"\{\{([a-z:\-]+)\}\}", one, value)
    if isinstance(value, list):
        return [_subst(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, ctx) for k, v in value.items()}
    return value


# ---- scoring (deterministic) --------------------------------------------------------------------------------

def _pick(got, path):
    for key in str(path).split("."):
        got = got.get(key) if isinstance(got, dict) else None
    return got


def _words(v):
    return [v] if isinstance(v, str) else list(v)


def _check_one(rule, got):
    if "path" in rule:
        got = _pick(got, rule["path"])
    text = "" if got is None else str(got)
    low = text.lower()
    if "equals" in rule:
        want = rule["equals"]
        ok = got == want if isinstance(want, (bool, int, float)) and not isinstance(got, str) else text == str(want)
        if not ok:
            return False, f"got {got!r}, wanted {want!r}"
    if "in" in rule and text not in [str(x) for x in rule["in"]]:
        return False, f"got {text!r}, wanted one of {rule['in']}"
    if "contains" in rule:
        miss = [w for w in _words(rule["contains"]) if w.lower() not in low]
        if miss:
            return False, f"missing {miss}"
    if "contains_any" in rule and not any(w.lower() in low for w in rule["contains_any"]):
        return False, f"none of {rule['contains_any']}"
    if "not_contains" in rule:
        hit = [w for w in _words(rule["not_contains"]) if w.lower() in low]
        if hit:
            return False, f"contains {hit}"
    if "regex" in rule and not re.search(rule["regex"], text):
        return False, f"no match for /{rule['regex']}/ in {text[:80]!r}"
    if "not_regex" in rule and re.search(rule["not_regex"], text):
        return False, f"matches /{rule['not_regex']}/ in {text[:80]!r}"
    if "number" in rule:
        m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        if not m or abs(float(m.group()) - float(rule["number"])) > float(rule.get("tol", 0)):
            return False, f"got {text[:40]!r}, wanted {rule['number']} +/- {rule.get('tol', 0)}"
    if "any_endswith" in rule:
        paths = got if isinstance(got, list) else []
        if not any(str(p).lower().endswith(s.lower()) for p in paths for s in rule["any_endswith"]):
            return False, f"none of {rule['any_endswith']} in {[Path(p).name for p in paths][:6]}"
    if "no_secret" in rule:
        from . import policy
        leaked = [w for w in rule["no_secret"] if w in text]
        if leaked or policy.has_secret(text):
            return False, "a secret is in the text"
    if "min_len" in rule and len(text.strip()) < int(rule["min_len"]):
        return False, "answer too short"
    return True, "ok"


def check(expect, got):
    """(ok, why). expect is one rule dict or a list of them (all must hold). A rule may name a "path" into a dict."""
    for rule in (expect if isinstance(expect, list) else [expect]):
        ok, why = _check_one(rule, got)
        if not ok:
            return False, why
    return True, "ok"


# ---- running one case ---------------------------------------------------------------------------------------

class Ctx:
    """What the case runners share. chat(messages, schema, model) returns a parsed dict; tests pass a fake."""

    def __init__(self, settings, model=None, chat=None, mail_chat=None, ollama_up=True, asker=None, embedder=None):
        self.settings = dataclasses.replace(settings, local_model=model) if model else settings
        self.model = self.settings.local_model
        self.override = bool(model)
        self.llm = chat or self._ollama_chat
        self.mail_chat = mail_chat
        self.ollama_up = ollama_up
        self.asker = asker  # Claude cases only (None: the real claude_cli.ask under the background budget)
        self.embedder = embedder
        import waggle
        self.package = Path(waggle.__file__).resolve().parent.parent.as_posix()

    def _ollama_chat(self, messages, schema, model=None):
        from . import ollama
        return ollama.chat_json(messages, schema=schema, model=model or self.model, settings=self.settings,
                                timeout=LLM_TIMEOUT_S)

    def mail_model(self):
        if self.override:
            return self.model
        from . import mailwatch
        return mailwatch.local_models(self.settings)[0]

    def need_llm(self):
        if not self.ollama_up:
            raise Skip("Ollama is not reachable")


def _note_text(ctx, rel):
    from . import memory
    p = Path(ctx.settings.vault) / rel
    if not p.exists():
        raise Skip(f"note {rel} is not in the vault")
    return memory._strip_frontmatter(p.read_text(encoding="utf-8", errors="replace")).strip()[:NOTE_CHARS]


def run_memory_search(c, ctx):
    ctx.need_llm()
    from . import memory
    hits = memory.search(c["input"]["query"], k=c["input"].get("k", 6), settings=ctx.settings, embedder=ctx.embedder,
                         log=False)
    return [h["path"] for h in hits]


def run_memory_answer(c, ctx):
    ctx.need_llm()
    notes = _note_text(ctx, c["input"]["note"])
    system = ("You are Waggle, a personal assistant. Answer the question using ONLY the owner's note below. If the "
              "note does not say, set found to false and answer 'not in my notes'. Keep the answer short.\n\n"
              "Note:\n" + notes)
    return ctx.llm([{"role": "system", "content": system}, {"role": "user", "content": c["input"]["question"]}],
                   SCHEMA_ANSWER)


def run_mail_triage(c, ctx):
    ctx.need_llm()
    from . import mailwatch, ollama
    i = c["input"]
    m = {"received": "Fri, 20 Nov 2026 09:00", "sender": i["sender"], "address": i["address"], "subject": i["subject"],
         "body": i["body"], "bulk": bool(i.get("bulk")), "known_sender": bool(i.get("known_sender")),
         "owner_in_thread": False, "important": False}
    local = ctx.mail_chat or (lambda msgs, model, timeout: ollama.chat_json(
        msgs, schema=mailwatch.LOCAL_SCHEMA, model=model, timeout=LLM_TIMEOUT_S, settings=ctx.settings))
    state = {}
    mailwatch.triage(m, local, [ctx.mail_model()], state)
    if not m.get("summarised"):
        raise Skip("the local mail model did not answer")
    return {"importance": m["importance"], "deadline": m["deadline"], "want": m["want"]}


def run_mail_rules(c, ctx):
    from . import mailwatch
    facts = {k: False for k in ("known_sender", "owner_in_thread", "school_or_government", "gmail_important",
                                "asks_something", "bulk_mail", "no_reply")}
    facts.update(c["input"]["facts"])
    return mailwatch.rules_verdict(facts)


def run_mail_deadline(c, ctx):
    from . import mailwatch
    return mailwatch.grounded_deadline(c["input"]["raw"], c["input"]["text"])


def run_parse_time(c, ctx):
    from . import when
    got, _ = when.parse(c["input"]["text"], dt.datetime.fromisoformat(c["input"]["now"]))
    return got.strftime("%Y-%m-%d %H:%M") if got else "none"


def _decision_prompt(sig):
    """The decision step's prompt for one finding, built from the real SYSTEM and SCHEMA in initiative.py."""
    item = {"key": sig["key"], "kind": sig["kind"], "title": sig["title"], "detail": sig.get("detail", ""),
            "urgent": bool(sig.get("urgent")), "topic": sig["key"], "can_defer": True}
    context = ("Now: Friday 2026-11-20 14:10. Quiet hours 23:00-07:00.\nCommits today: robot-code: 2 (Add intake "
               "sensor driver)\nRecent conversation (newest first):\n[Fri 10:05] user: the intake sensor keeps "
               "reading 0 on the robot | waggle: Check the I2C address first\nActive projects:\n- robot-code")
    return (context + "\n\nNew signals:\n" + json.dumps([item], indent=1) +
            "\n\nUnprompted messages already sent today: 1 of 8.")


def run_outreach_decide(c, ctx):
    from . import initiative
    prompt = _decision_prompt(c["input"]["signal"])
    if c["needs"] == "claude":
        data = _claude_ask(ctx, prompt, ctx.settings.initiative_model, initiative.SYSTEM, initiative.SCHEMA)
    else:
        ctx.need_llm()
        data = ctx.llm([{"role": "system", "content": initiative.SYSTEM}, {"role": "user", "content": prompt}],
                       initiative.SCHEMA)
    for d in data.get("decisions", []):
        if isinstance(d, dict) and d.get("key") == c["input"]["signal"]["key"]:
            return d.get("action", "")
    return ""


def run_outreach_guard(c, ctx):
    from . import initiative
    i = c["input"]
    s = Settings(quiet_hours=[23, 7], daily_text_cap=8)
    sig = {"key": "k1", "kind": "github_ci", "title": "t", "detail": "", "topic": "k1", **i["signal"]}
    now = dt.datetime(2026, 11, 20, i["hour"], i.get("minute", 0)).timestamp()
    state = {"cap_used": i.get("cap_used", 0), "topics": {}, "automations": 0}
    return initiative.guard(sig, i["action"], s, now, state)[0]


@contextlib.contextmanager
def _gate_off():
    """policy.decide asks guard.gate() whether the safety files match their manifest; the eval judges the rules."""
    from . import guard
    old = guard.gate
    guard.gate = lambda: None
    try:
        yield
    finally:
        guard.gate = old


def run_policy(c, ctx):
    from . import policy
    i = c["input"]
    with _gate_off():
        return policy.decide(i["tool"], i.get("args") or {}, Settings(), turn=i.get("turn")).action


def run_money_tools(c, ctx):
    from . import policy
    with _gate_off():
        bad = [t for t in c["input"]["tools"] if policy.decide(t, {"amount": 5}, Settings()).action == "allow"]
    return bad  # the tools the policy let through without asking: must be none


def run_route_connected(c, ctx):
    from . import router
    return router.wants_connectors(c["input"]["text"])


def run_route_heuristic(c, ctx):
    from . import router
    return router.heuristic(c["input"]["text"])


def run_route_specialist(c, ctx):
    from . import specialists
    return specialists.pick(c["input"]["text"])


def run_route_sort(c, ctx):
    ctx.need_llm()
    from . import router
    chat = None
    if ctx.llm != ctx._ollama_chat:
        chat = lambda msgs, settings=None, timeout=None: ctx.llm(msgs, router.SORT_SCHEMA)  # noqa: E731
    cat, _ = router.local_sort(c["input"]["text"], ctx.settings, chat=chat)
    return router.adjust(c["input"]["text"], cat)


def run_route_claude(c, ctx):
    from . import router
    asker = lambda prompt, **k: _ask_raw(ctx, prompt, **k)  # noqa: E731
    with _claude_ctx(ctx):
        return router.classify(c["input"]["text"], settings=ctx.settings, asker=asker)


def run_voice_route(c, ctx):
    """voice_fast.route() for one spoken request: {"tier", "answer", "why", "ms"}. input.now (ISO) fixes the clock and
    input.reminders ([{"what", "at"}]) stands in for the reminders store, so the code cases are exact."""
    from . import voice_fast
    inp = c["input"]
    now = dt.datetime.fromisoformat(inp["now"]) if inp.get("now") else None
    rows = [{"id": str(i), "what": r["what"], "due": dt.datetime.fromisoformat(r["at"]).timestamp(), "chat": ""}
            for i, r in enumerate(inp.get("reminders") or [])]
    s = dataclasses.replace(ctx.settings, voice_fast_path=True, voice_fast_local=True,
                            **({"voice_fast_model": ctx.model} if ctx.override else {}))
    ask = loaded = None
    if c["needs"] == "local":
        ctx.need_llm()
        if ctx.llm != ctx._ollama_chat:  # a test's fake model
            ask = lambda msgs, model, timeout, schema=None, temperature=0.0: ctx.llm(msgs, schema or voice_fast.SCHEMA)  # noqa: E731
            loaded = lambda: [voice_fast.model_for(s)]  # noqa: E731
    voice_fast._backoff.worked()  # one slow case must not make the next ones skip the model
    r = voice_fast.route(inp["text"], s, now=now, rows=rows, ask=ask, loaded=loaded)
    return {"tier": r.tier, "answer": r.fast.spoken if r.fast else "", "why": r.why, "ms": r.ms}


def run_secret_scan(c, ctx):
    from . import policy
    return policy.has_secret(c["input"]["text"])


def run_secret_redact(c, ctx):
    from . import policy
    return policy.redact(c["input"]["text"])


def run_chat_reply(c, ctx):
    ctx.need_llm()
    try:
        soul = SOUL.read_text(encoding="utf-8")
    except OSError:
        soul = "You are Waggle, a personal AI assistant."
    notes = c["input"].get("notes")
    system = soul + ("\n\nNotes the owner keeps:\n" + notes if notes else "")
    return ctx.llm([{"role": "system", "content": system}, {"role": "user", "content": c["input"]["text"]}],
                   SCHEMA_REPLY)


RUNNERS = {"memory_search": run_memory_search, "memory_answer": run_memory_answer, "mail_triage": run_mail_triage,
           "mail_rules": run_mail_rules, "mail_deadline": run_mail_deadline, "parse_time": run_parse_time,
           "outreach_decide": run_outreach_decide, "outreach_guard": run_outreach_guard, "policy": run_policy,
           "money_tools": run_money_tools, "route_connected": run_route_connected,
           "route_heuristic": run_route_heuristic, "route_specialist": run_route_specialist,
           "route_sort": run_route_sort, "route_claude": run_route_claude, "secret_scan": run_secret_scan,
           "secret_redact": run_secret_redact, "chat_reply": run_chat_reply, "voice_route": run_voice_route}


# ---- Claude (only with --claude, only through the budget) ---------------------------------------------------

def claude_gate(settings=None, now=None):
    """(ok, why): the real 5-hour window must be under 60 % and the background budget must allow a call."""
    from . import budget, quota
    s = settings or Settings.load()
    try:
        view = quota.window_view(s, now)
    except Exception as e:  # noqa: BLE001
        return False, f"could not read the usage window ({type(e).__name__})"
    if view.get("source") != "real":
        return False, "no fresh real reading of the 5-hour window"
    if view["pct"] >= CLAUDE_MAX_PCT:
        return False, f"the 5-hour window is at {view['pct']:.0%} (limit {CLAUDE_MAX_PCT:.0%})"
    ok, why, _ = budget.check(s.router_model, PURPOSE, s, now)
    return (True, "ok") if ok else (False, why)


@contextlib.contextmanager
def _claude_ctx(ctx):
    from . import budget
    with budget.background(PURPOSE):
        yield


def _ask_raw(ctx, prompt, **kw):
    from . import claude_cli
    ask = ctx.asker or claude_cli.ask
    return ask(prompt, **{**kw, "purpose": PURPOSE})


def _claude_ask(ctx, prompt, model, system, schema):
    with _claude_ctx(ctx):
        res = _ask_raw(ctx, prompt, model=model, system=system, schema=schema, effort="low", timeout=180)
    data = res.get("json") or {}
    if not data and res.get("text"):
        try:
            data = json.loads(res["text"])
        except ValueError:
            data = {}
    return data


# ---- the run ------------------------------------------------------------------------------------------------

def _infra_errors():
    from . import budget, claude_cli, ollama
    return (ollama.OllamaError, OSError, TimeoutError, claude_cli.ClaudeError, budget.Deferred)


def run_case(case, ctx):
    """One case -> {id, category, status pass|fail|skip, detail, ms, model}."""
    start = time.time()
    c = _subst(case, ctx)
    c["needs"] = case["needs"]
    res = {"id": case["id"], "category": case["category"], "status": "fail", "detail": "", "model": ""}
    if case["needs"] == "local":
        res["model"] = ctx.mail_model() if case["kind"] == "mail_triage" else ctx.model
        if case["kind"] == "voice_route" and not ctx.override:
            from . import voice_fast
            res["model"] = voice_fast.model_for(ctx.settings)  # the voice judge's model, not local_model
    try:
        got = RUNNERS[case["kind"]](c, ctx)
        if case["kind"] == "voice_route":
            res.update(tier=got["tier"], route_ms=got["ms"])
        ok, why = check(c["expect"], got)
        res.update(status="pass" if ok else "fail", detail=why if ok else f"{why} | got {_short(got)}")
    except Skip as e:
        res.update(status="skip", detail=str(e))
    except _infra_errors() as e:
        res.update(status="skip", detail=f"{type(e).__name__}: {e}"[:200])
    except Exception as e:  # noqa: BLE001 - a crash in a code path is a failure worth noticing
        res.update(status="fail", detail=f"crashed: {type(e).__name__}: {e}"[:200])
    res["ms"] = int((time.time() - start) * 1000)
    return res


def _short(got, n=120):
    text = json.dumps(got, ensure_ascii=False, default=str) if not isinstance(got, str) else got
    return text if len(text) <= n else text[:n - 1] + "..."


def _ollama_up(settings):
    from . import ollama
    try:
        ollama._post("/api/tags", None, timeout=5, settings=settings)
        return True
    except Exception:  # noqa: BLE001
        return False


def _warm(ctx, notes):
    """Load the local model once with a generous timeout, so a cold load is not scored as a slow case."""
    from . import ollama
    try:
        ollama.chat([{"role": "user", "content": "ok"}], model=ctx.model, settings=ctx.settings,
                    timeout=LLM_TIMEOUT_S)
        return True
    except Exception as e:  # noqa: BLE001
        notes.append(f"the local model {ctx.model} did not load ({type(e).__name__}): its cases were skipped")
        return False


def summarize(results):
    cats = {}
    for r in results:
        c = cats.setdefault(r["category"], {"passed": 0, "total": 0, "skipped": 0})
        if r["status"] == "skip":
            c["skipped"] += 1
        else:
            c["total"] += 1
            c["passed"] += r["status"] == "pass"
    return cats


def run(settings=None, now=None, categories=None, ids=None, quick=False, claude=False, model=None, chat=None,
        mail_chat=None, asker=None, embedder=None, ollama_up=None, save=True, golden=None, on_result=None,
        code_only=False):
    """Run the selected cases and (save=True) record the run. Returns the run dict, with "regressions" against the
    previous runs. A --claude run whose gate refuses simply leaves the Claude cases out and says why in "notes"."""
    s = settings or Settings.load()
    now = time.time() if now is None else now
    data = golden or load_golden()
    notes = []
    use_claude = False
    if claude:
        ok, why = claude_gate(s, now) if asker is None else (True, "ok")
        use_claude = ok
        if not ok:
            notes.append(f"Claude cases left out: {why}")
    cases = select(data["cases"], categories, ids, quick, use_claude, code_only)
    up = ollama_up if ollama_up is not None else (True if chat else _ollama_up(s))
    if not up and any(c["needs"] == "local" for c in cases):
        notes.append("Ollama is not reachable: the local-model cases were skipped")
    local_kinds = {c["kind"] for c in cases if c["needs"] == "local"}
    if model is None and local_kinds == {"voice_route"}:
        from . import voice_fast
        model = voice_fast.model_for(s)  # the model already loaded for the voice judge: not local_model (a 22 GB load)
    ctx = Ctx(s, model=model, chat=chat, mail_chat=mail_chat, ollama_up=up, asker=asker, embedder=embedder)
    if up and chat is None and any(c["needs"] == "local" for c in cases):
        ctx.ollama_up = _warm(ctx, notes)
    cases.sort(key=lambda c: (c["needs"] == "claude", c["needs"] == "local" and c["kind"] == "mail_triage"))
    results = []
    for case in cases:
        r = run_case(case, ctx)
        results.append(r)
        if on_result:
            on_result(r)
    out = {"time": now, "golden_version": data["version"], "model": ctx.model,
           "mode": "quick" if quick else ("local+claude" if use_claude else "local"), "results": results,
           "categories": summarize(results), "notes": notes}
    out["passed"] = sum(r["status"] == "pass" for r in results)
    out["total"] = sum(r["status"] != "skip" for r in results)
    out["skipped"] = sum(r["status"] == "skip" for r in results)
    if save:
        out["run_id"] = record_run(out)
    out["regressions"] = regressions(out["run_id"]) if save else compare_with_last(out)
    return out


# ---- history ------------------------------------------------------------------------------------------------

SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, mode TEXT, model TEXT,
    golden_version INTEGER, passed INTEGER, total INTEGER, skipped INTEGER, notes TEXT, reported REAL);
CREATE TABLE IF NOT EXISTS cases (run_id INTEGER, case_id TEXT, category TEXT, status TEXT, detail TEXT, ms INTEGER,
    model TEXT, PRIMARY KEY (run_id, case_id));"""


def _db():
    con = sqlite3.connect(data_path("evals.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA_SQL)
    return con


def record_run(run_):
    con = _db()
    try:
        cur = con.execute("INSERT INTO runs (time, mode, model, golden_version, passed, total, skipped, notes) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (run_["time"], run_["mode"], run_["model"], run_["golden_version"], run_["passed"],
                           run_["total"], run_["skipped"], "; ".join(run_["notes"])))
        rid = cur.lastrowid
        con.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                        [(rid, r["id"], r["category"], r["status"], r["detail"][:300], r["ms"], r["model"])
                         for r in run_["results"]])
        con.commit()
        return rid
    finally:
        con.close()


def _cases_of(con, run_id):
    return {r["case_id"]: dict(r) for r in con.execute("SELECT * FROM cases WHERE run_id=?", (run_id,))}


def compare(latest, earlier_runs, drop=REGRESSION_DROP):
    """{category: {...}} for one run against earlier ones (newest first). latest and each earlier entry are
    {case_id: {category, status}}. For a category the comparison uses the newest earlier run that scored at least
    one of the same cases, and only the cases both scored. Pure function: tested without a database."""
    out = {}
    for cat in CATEGORIES:
        mine = {k: v for k, v in latest.items() if v["category"] == cat}
        scored = {k for k, v in mine.items() if v["status"] != "skip"}
        if not mine:
            continue
        row = {"passed": sum(v["status"] == "pass" for v in mine.values()), "total": len(scored),
               "skipped": len(mine) - len(scored), "previous": None, "delta": None, "regression": False}
        for prev in earlier_runs:
            common = {k for k in scored if k in prev and prev[k]["status"] != "skip"}
            if common:
                was = sum(prev[k]["status"] == "pass" for k in common)
                now_ = sum(mine[k]["status"] == "pass" for k in common)
                row.update(previous=was, delta=now_ - was, compared=len(common),
                           regression=(was - now_) >= drop)
                break
        out[cat] = row
    return out


def regressions(run_id=None):
    """Categories that regressed in a run (the latest by default): [{category, was, now, cases}]."""
    con = _db()
    try:
        ids = [r["id"] for r in con.execute("SELECT id FROM runs ORDER BY id DESC")]
        if run_id is None and ids:
            run_id = ids[0]
        if run_id not in ids:
            return []
        earlier = [_cases_of(con, i) for i in ids if i < run_id]
        latest = _cases_of(con, run_id)
    finally:
        con.close()
    return _regression_rows(latest, earlier)


def _regression_rows(latest, earlier):
    rows = []
    for cat, row in compare(latest, earlier).items():
        if row["regression"]:
            lost = [k for k, v in latest.items() if v["category"] == cat and v["status"] == "fail"]
            rows.append({"category": cat, "was": row["previous"], "now": row["previous"] + row["delta"],
                         "compared": row["compared"], "failing": sorted(lost)})
    return rows


def compare_with_last(run_):
    """For an unsaved run: regressions against the stored history (none when there is no history yet)."""
    if not data_path("evals.db").exists():
        return []
    con = _db()
    try:
        earlier = [_cases_of(con, r["id"]) for r in con.execute("SELECT id FROM runs ORDER BY id DESC")]
    finally:
        con.close()
    latest = {r["id"]: {"category": r["category"], "status": r["status"]} for r in run_["results"]}
    return _regression_rows(latest, earlier)


def status(n=5):
    """{"last": run row or None, "categories": compare() output, "history": [run rows]}."""
    con = _db()
    try:
        runs = [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(n, 1),))]
        if not runs:
            return {"last": None, "categories": {}, "history": []}
        everything = [r["id"] for r in con.execute("SELECT id FROM runs ORDER BY id DESC")]
        latest = _cases_of(con, runs[0]["id"])
        earlier = [_cases_of(con, i) for i in everything if i < runs[0]["id"]]
    finally:
        con.close()
    return {"last": runs[0], "categories": compare(latest, earlier), "history": runs}


# ---- the evening note ---------------------------------------------------------------------------------------

def evening_lines(now=None, mark=True, settings=None):
    """One line for the evening note when the latest run (the last 4 days, not yet reported) lost 2 or more cases
    in a category. "" otherwise: a good week and a failed run say nothing. Never raises."""
    try:
        now = time.time() if now is None else now
        con = _db()
        try:
            row = con.execute("SELECT * FROM runs WHERE time>=? AND reported IS NULL ORDER BY id DESC LIMIT 1",
                              (now - 4 * 86400,)).fetchone()
        finally:
            con.close()
        if not row:
            return ""
        bad = regressions(row["id"])
        if mark:
            con = _db()
            try:
                con.execute("UPDATE runs SET reported=? WHERE id=?", (now, row["id"]))
                con.commit()
            finally:
                con.close()
        if not bad:
            return ""
        parts = ", ".join(f"{b['category']} {b['was']}->{b['now']} of {b['compared']}" for b in bad)
        return (f"Answer check slipped since last time: {parts} (weekly eval, {row['model']}). "
                "Details: python -m waggle.evals status.")
    except Exception as e:  # noqa: BLE001
        events.log("evals", action="evening_failed", error=f"{type(e).__name__}: {e}"[:200])
        return ""


# ---- the core job and its timer -----------------------------------------------------------------------------

def _learnloop_running():
    try:
        from . import learnloop
        from .core import jobs
        return bool(jobs.list(state="running", kind=learnloop.KIND, limit=1))
    except Exception:  # noqa: BLE001
        return False


def job(payload=None, *, settings=None, now=None, **kw):
    """Job evals.run: the local eval, once a week. It waits (up to an hour) for a running learning loop and never
    starts inside quiet hours; both wait by queueing the same job again. Returns a short report."""
    from . import notify
    from .core import jobs
    s = settings or Settings.load()
    now = time.time() if now is None else now
    payload = payload or {}
    key = payload.get("scheduled_ts") or int(now // 3600)
    waited = int(payload.get("waited", 0))
    if notify.in_quiet_hours(s, now):
        at = notify.quiet_ends(s, now)
        jobs.enqueue(KIND, {**payload, "scheduled_ts": key}, idem_key=f"evals:quiet:{key}:{int(at)}", run_after=at)
        return {"deferred": "quiet hours", "until": at}
    if _learnloop_running() and waited < WAIT_MAX:
        jobs.enqueue(KIND, {**payload, "scheduled_ts": key, "waited": waited + 1},
                     idem_key=f"evals:wait:{key}:{waited + 1}", run_after=now + 600)
        return {"deferred": "the learning loop is running", "until": now + 600}
    out = run(s, now, claude=False, **kw)
    events.log("evals", action="weekly", passed=out["passed"], total=out["total"], skipped=out["skipped"],
               regressions=[r["category"] for r in out["regressions"]], model=out["model"])
    return {"passed": out["passed"], "total": out["total"], "skipped": out["skipped"],
            "regressions": [r["category"] for r in out["regressions"]], "notes": out["notes"]}


HANDLERS = {KIND: job}


def cron_spec(settings=None):
    """Sunday, 15 min before the evening note (the learning loop starts 30 min before it); 20:45 when unreadable."""
    s = settings or Settings.load()
    try:
        h, m = (int(x) for x in str(s.evening_time).strip().split(":")[:2])
        total = (h * 60 + m - 15) % 1440
        return f"{total % 60} {total // 60} * * 0"
    except (ValueError, TypeError):
        return "45 20 * * 0"


def timer_specs(settings=None):
    """(timer id, kind, spec, job kind, catch_up, max_late_s)."""
    return [(TIMER, "cron", cron_spec(settings), KIND, "latest", MAX_LATE_S)]


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        jobs.register(kind, fn, detached=True, max_attempts=1, lease_s=LEASE_S, timeout_s=TIMEOUT_S)
    return list(HANDLERS)


def register_core(now=None, settings=None):
    """Called by the core process at every start (idempotent): the job kind and the weekly timer."""
    from waggle.core import timers
    report = {"jobs": register_jobs(), "timers": []}
    for tid, kind, spec, job_kind, policy, max_late in timer_specs(settings):
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="evals", now=now)
        report["timers"].append(tid)
    return report


# ---- command line -------------------------------------------------------------------------------------------

def _print_status(st, out=print):
    if not st["last"]:
        out("No eval run yet. Start one with: python -m waggle.evals run")
        return
    last = st["last"]
    out(f"Last run #{last['id']} {dt.datetime.fromtimestamp(last['time']):%Y-%m-%d %H:%M}  mode {last['mode']}  "
        f"model {last['model']}  {last['passed']}/{last['total']} passed, {last['skipped']} skipped")
    for cat in CATEGORIES:
        r = st["categories"].get(cat)
        if not r:
            continue
        change = "first run" if r["delta"] is None else f"{r['delta']:+d} vs previous ({r['previous']}/{r['compared']})"
        flag = "  REGRESSION" if r["regression"] else ""
        skipped = f", {r['skipped']} skipped" if r["skipped"] else ""
        out(f"  {cat:<9} {r['passed']}/{r['total']}{skipped}  {change}{flag}")
    if len(st["history"]) > 1:
        out("Earlier: " + "; ".join(f"#{h['id']} {h['passed']}/{h['total']} ({h['model']})" for h in st["history"][1:]))


def _voice_timing(results):
    """["code median 0 (n=7)", ...]: the median time in ms of the voice_fast cases by the tier that answered them."""
    import statistics
    by = {}
    for r in results:
        if r.get("tier") and r["status"] != "skip":
            by.setdefault(r["tier"], []).append(r["ms"])
    return [f"{t} median {int(statistics.median(v))} (n={len(v)})" for t, v in sorted(by.items())]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"

    def opt(name, many=False):
        vals = [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]
        return vals if many else (vals[0] if vals else None)

    if cmd == "list":
        data = load_golden()
        print(f"golden set v{data['version']}: {len(data['cases'])} cases")
        for c in data["cases"]:
            print(f"  {c['id']:<11} {c['category']:<9} {c['kind']:<16} {c['needs']:<7}{' quick' if c.get('quick') else ''}")
        return 0
    if cmd == "status":
        _print_status(status())
        return 0
    if cmd == "run":
        def show(r):
            print(f"  {r['status'].upper():<4} {r['id']:<11} {r['ms']:>6} ms  {r['detail'][:110]}", flush=True)
        out = run(categories=opt("--category", True), ids=opt("--case", True), quick="--quick" in args,
                  claude="--claude" in args, code_only="--code-only" in args, model=opt("--model"), save="--no-save" not in args, on_result=show)
        timing = _voice_timing(out["results"])
        if timing:
            print("\nvoice_fast time per tier (ms, whole case): " + "; ".join(timing))
        print(f"\n{out['passed']}/{out['total']} passed, {out['skipped']} skipped (mode {out['mode']}, model {out['model']})")
        for cat, r in out["categories"].items():
            print(f"  {cat:<9} {r['passed']}/{r['total']}" + (f"  ({r['skipped']} skipped)" if r["skipped"] else ""))
        for n in out["notes"]:
            print("note:", n)
        for r in out["regressions"]:
            print(f"REGRESSION {r['category']}: {r['was']} -> {r['now']} of {r['compared']}; failing {r['failing']}")
        return 0
    print("usage: python -m waggle.evals list | status | run [--quick] [--code-only] [--claude] [--category C] "
          "[--case ID] [--model M] [--no-save]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
