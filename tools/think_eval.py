"""Run the thinking pass on situations nobody wrote code for (bench/think_cases.json) with the real model.

Nothing is sent and nothing touches the live home: the script makes its own throwaway WAGGLE_HOME unless one is
set, calls think.reason() on each made-up picture and scores the answer in code.

  py -3.14 tools/think_eval.py                  every case
  py -3.14 tools/think_eval.py --group holdout  only the cases the prompt was never worded against
  py -3.14 tools/think_eval.py --case mens-day  one case, with the model's full answer
  py -3.14 tools/think_eval.py --write          also write docs/THINK-EVAL.md

Scoring per case (expect):
  raise     a list of word groups; one thought that is not "none" must contain a word from EVERY group
  silent    no thought with action text or voice (a line for the evening note is allowed)
  question  the model put a question for the owner
  lookup    the model asked for a look-up before answering
Every case also fails when more than two thoughts would buzz the phone (noise), or when a name appears that is
not in the picture and the case says no_invented_people.
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def build_sections(case, base):
    from waggle import think
    from waggle.config import Settings
    now = dt.datetime.strptime(case["now"], "%Y-%m-%d %H:%M").timestamp()
    s = Settings()
    s.quiet_hours = [23, 7]
    s.home_location = ""
    out = {"Now": think._now_section(s, now)}
    if not case.get("no_calendar"):   # the real sense, as the live pass has it (cached in the throwaway home)
        from waggle import worldcal
        cal = "\n".join(worldcal.lines(now, days=3))
        if cal:
            out[think.CALENDAR_TITLE] = cal
    merged = {**base, **(case.get("replace") or {})}
    for k, v in merged.items():
        if v and k not in case.get("sections", {}):
            out[k] = v
    for k, v in case.get("sections", {}).items():
        if v:
            out[k] = v
    order = ["Now", think.CALENDAR_TITLE, think.TASTE_TITLE, "Who they are (from their own notes)", "People in their life (Waggle's notes)", "Active projects",
             "Coming up (dated)", "Open reminders and check-ins",
             "What they said lately (their own words, newest first)", "Work today (commits)", "Weather"]
    return {k: out[k] for k in sorted(out, key=lambda t: order.index(t) if t in order else 99)}


def score(case, result):
    exp = case.get("expect", {})
    raised = [t for t in result["thoughts"] if t["action"] != "none"]
    loud = [t for t in raised if t["action"] in ("text", "voice")]
    why = []
    if exp.get("raise"):
        def hit(t):
            blob = " ".join([t["topic"].replace("-", " "), t["message"], t.get("draft", ""), t.get("why", "")]).lower()
            return all(any(w in blob for w in group) for group in exp["raise"])
        if not any(hit(t) for t in raised):
            why.append("did not raise it")
    if exp.get("silent") and loud:
        why.append(f"buzzed for: {loud[0]['topic']}")
    if exp.get("question") and not result["questions"]:
        why.append("asked no question")
    if exp.get("lookup") and not result["lookups"]:
        why.append("asked for no look-up")
    if exp.get("no_invented_people"):
        blob = " ".join(t["message"] + " " + t.get("draft", "") for t in raised).lower()
        for name in ("dad", "mom", "father", "mother", "sister", "brother", "arjun", "priya"):
            if f" {name}" in f" {blob}":
                why.append(f"invented a person: {name}")
                break
    if len(loud) > 2:
        why.append(f"{len(loud)} buzzes in one pass")
    return (not why), "; ".join(why)


def run_case(case, base, settings, model):
    from waggle import think
    sections = build_sections(case, base)
    canned = case.get("lookups") or {}
    runners = {k: (lambda q, st, _k=k: canned.get(_k, "nothing found")) for k in ("vault", "weather", "web")}
    settings.think_model = model
    t0 = time.time()
    try:
        result = think.reason(think.render(sections), settings, lookup_runners=runners)
    except Exception as e:  # noqa: BLE001
        return {"id": case["id"], "group": case["group"], "ok": False, "why": f"error: {str(e)[:160]}",
                "seconds": round(time.time() - t0, 1), "result": {"thoughts": [], "questions": [], "lookups": []}}
    ok, why = score(case, result)
    return {"id": case["id"], "group": case["group"], "ok": ok, "why": why, "seconds": round(time.time() - t0, 1),
            "result": result}


def report(rows, model):
    lines = [f"# Thinking pass: situations nobody coded for ({dt.date.today():%Y-%m-%d}, model {model})", "",
             "Each row is a made-up picture of a day (bench/think_cases.json) given to the real model through "
             "`think.reason()`. No situation here has code or a prompt line of its own. Run again with "
             "`py -3.14 tools/think_eval.py --write`.", ""]
    for g in ("tune", "holdout"):
        part = [r for r in rows if r["group"] == g]
        if part:
            lines.append(f"**{g}: {sum(r['ok'] for r in part)} of {len(part)} passed**")
    lines += ["", "| Case | Group | Result | What it said (first thought) | Seconds |", "|---|---|---|---|---|"]
    for r in rows:
        th = [t for t in r["result"]["thoughts"] if t["action"] != "none"]
        said = (f"[{th[0]['action']}] {th[0]['message'][:150]}" if th else
                ("question: " + r["result"]["questions"][0]["question"][:120] if r["result"]["questions"] else "(nothing)"))
        lines.append(f"| {r['id']} | {r['group']} | {'pass' if r['ok'] else 'FAIL: ' + r['why']} | "
                     f"{said.replace('|', '/')} | {r['seconds']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="all", choices=["all", "tune", "holdout"])
    ap.add_argument("--case", default="")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not os.environ.get("WAGGLE_HOME"):
        os.environ["WAGGLE_HOME"] = tempfile.mkdtemp(prefix="waggle-think-eval-")
    from waggle import budget
    from waggle.config import Settings
    budget.OWN_ALLOWANCE["think"] = {**budget.OWN_ALLOWANCE["think"], "daily_calls": 10 ** 6}  # this process only
    data = json.loads((REPO / "bench" / "think_cases.json").read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if (args.group == "all" or c["group"] == args.group)
             and (not args.case or c["id"] == args.case)]
    s = Settings()
    rows = []
    for c in cases:
        r = run_case(c, data["base"], s, args.model)
        rows.append(r)
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {c['id']:<28} {r['seconds']:>5}s  {r['why']}", flush=True)
        if args.case:
            print(json.dumps(r["result"], indent=1, ensure_ascii=False))
    for g in ("tune", "holdout"):
        part = [r for r in rows if r["group"] == g]
        if part:
            print(f"{g}: {sum(r['ok'] for r in part)} of {len(part)}")
    out = Path(os.environ["WAGGLE_HOME"]) / "think_eval.json"
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"details: {out}")
    if args.write and not args.case:
        (REPO / "docs" / "THINK-EVAL.md").write_text(report(rows, args.model), encoding="utf-8")
        print("wrote docs/THINK-EVAL.md")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
