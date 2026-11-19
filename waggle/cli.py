"""`waggle` command: setup and inspection outside of Hermes."""
import argparse
import json
import sys
from pathlib import Path

if not __package__:
    # Run as a plain file (python waggle\cli.py): make the package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "waggle"

from . import jobs, learn, memory, ollama, quota, router, vault  # noqa: E402
from .config import Settings  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="waggle", description="Waggle personal agent tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create config + Obsidian vault")
    sub.add_parser("status", help="quota, jobs, models")
    sub.add_parser("reindex", help="re-embed changed vault notes")
    s = sub.add_parser("search", help="search the vault")
    s.add_argument("query")
    r = sub.add_parser("route", help="show how a message would be routed (uses Sonnet unless trivial)")
    r.add_argument("message")
    sub.add_parser("consolidate", help="Sonnet cleans up your profile note")
    sub.add_parser("jobs", help="list build jobs")
    sub.add_parser("login", help="open the Claude CLI in a new window so you can /login")
    sub.add_parser("doctor", help="health check of every part of Waggle, with fixes")
    sub.add_parser("models", help="hardware, installed models and recommendations")
    sub.add_parser("skills", help="rebuild the skill index and show stats")
    sub.add_parser("learn", help="run the learning pass now (analysis, skill discovery)")
    sub.add_parser("bench", help="routing benchmark on the labelled cases (real Haiku calls)")
    sub.add_parser("routines", help="list scheduled routines")
    b = sub.add_parser("budget", help="background Claude budget: status, or `budget set <units/day>`")
    b.add_argument("action", nargs="?", default="status", choices=["status", "set"])
    b.add_argument("units", nargs="?", type=float)
    c = sub.add_parser("connectors", help="re-read which claude.ai connectors are available")
    c.add_argument("--refresh", action="store_true")
    a = p.parse_args(argv)
    st = Settings.load()

    if a.cmd == "init":
        st.save()
        print(f"vault: {vault.init(st)}\nconfig: saved")
    elif a.cmd == "status":
        print(json.dumps({"ollama_up": ollama.is_up(st), "local_model": st.local_model, "quota": quota.status(st),
                          "jobs": {k: v.get("status") for k, v in jobs.load().items()}}, indent=2))
    elif a.cmd == "reindex":
        print(f"updated {memory.reindex(st)} notes")
    elif a.cmd == "search":
        for h in memory.search(a.query, settings=st):
            print(f"{h['score']:.3f}  {h['path']}\n    {h['text'][:160]!r}")
    elif a.cmd == "route":
        d = router.decide(a.message, memory.context_block(a.message, st), st)
        print(json.dumps(d, indent=2))
    elif a.cmd == "budget":
        from . import budget
        if a.action == "set":
            if a.units is None:
                p.error("budget set needs a number of units per day, e.g. `budget set 6`")
            print(f"background budget: {budget.set_limit(a.units)} units/day")
        else:
            b = budget.status(st)
            print(f"background today: {b['background_units']:.3f} of {b['limit_units']:.2f} units "
                  f"({b['remaining_units']:.3f} left, {b['running_calls']} running); interactive "
                  f"{b['interactive_units']:.3f}; {'can start' if b['can_start'] else 'waiting: ' + b['why']}")
            for d in b["decisions"][:5]:
                print(f"  {d['action']:<9} {d.get('objective') or '':<9} {d['reason']}")
    elif a.cmd == "consolidate":
        print(learn.consolidate(st))
    elif a.cmd == "login":
        import os
        import subprocess
        from .claude_cli import find_claude
        exe = find_claude()
        if not exe:
            print("Claude CLI not found.")
            return
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        subprocess.Popen([exe], creationflags=flags)
        print("A Claude window opened. Type /login there, sign in, then close it.")
    elif a.cmd == "doctor":
        from . import doctor
        print(doctor.report(doctor.run_all(st)))
    elif a.cmd == "models":
        from . import models
        cat = models.catalog(st, refresh=True)
        print(json.dumps(cat["hardware"], indent=2))
        for m in cat["local"]:
            print(f"  {m['name'][:48]:<48} {m['size_gb']:>5} GB  {m['where']:<14} ctx {m['context']}"
                  f"{'  vision' if m['vision'] else ''}{'  tools' if m['tools'] else ''}")
        for t in models.recommendations(cat, st):
            print("tip:", t)
    elif a.cmd == "skills":
        from . import skills
        print(skills.reindex(force=True), json.dumps(skills.stats(), indent=2))
    elif a.cmd == "learn":
        from . import learning
        out = learning.run(settings=st)
        print(json.dumps({k: out[k] for k in ("candidates", "drafted")}, indent=2, default=str))
    elif a.cmd == "bench":
        from . import learning
        for name in ("routes.json", "routes_holdout.json"):
            r = learning.evaluate_router(cases=learning.load_cases(name), settings=st)
            print(f"{name}: {r['accuracy']} ({r['n']} cases)")
            for m in r["misses"]:
                print(f"  want {m['want']:<9} got {m['got']:<9} {m['text']}")
    elif a.cmd == "routines":
        from . import routines
        for r in routines.listing():
            print(f"{'  ' if r['enabled'] else '# '}{r['name']:<28} {r['schedule']:<18} next {r['next']}  {r['agent']}")
    elif a.cmd == "connectors":
        from . import claude_cli
        print("\n".join(claude_cli.mcp_servers(refresh=True)))
    elif a.cmd == "jobs":
        for j in jobs.load().values():
            print(f"{j['id']}  {j.get('status')}  {j.get('title')}  {j.get('workdir')}")


if __name__ == "__main__":
    main()
