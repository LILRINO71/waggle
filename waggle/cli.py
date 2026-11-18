"""`waggle` command: setup and inspection outside of Hermes."""
import argparse
import json

from . import jobs, learn, memory, ollama, quota, router, vault
from .config import Settings


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
    elif a.cmd == "jobs":
        for j in jobs.load().values():
            print(f"{j['id']}  {j.get('status')}  {j.get('title')}  {j.get('workdir')}")


if __name__ == "__main__":
    main()
