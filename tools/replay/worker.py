"""One replay worker process: runs Waggle's engine (or its golden cases) from ONE code tree in a THROWAWAY home.

  python worker.py answer   < {"text": ..., "chat": ..., "claude": false}   -> one "REPLAY_RESULT {json}" line
  python worker.py golden   < {"golden": path, "ids": [...], "quick": false} -> one "REPLAY_RESULT {json}" line

Environment (set by run.py, checked here before anything of Waggle is imported):
  REPLAY_TREE   the code under test (a git worktree of base or head); it goes first on sys.path
  WAGGLE_HOME   the throwaway home; it must hold the .replay-throwaway marker that prepare_home writes
  USERPROFILE   the throwaway user folder, so Path.home() inside the engine is not the owner's

Safety, in code and not by trust:
  - a home without the marker, or the owner's real home, is refused
  - the sockets refuse every address that is not loopback (Ollama is on 127.0.0.1): no Telegram, no web
  - the delivery is an in-memory CollectDelivery, nothing is sent
  - the Claude CLI is hidden (claude_available is False, find_claude is None) unless the caller asked for the
    one-shot Claude answer, which has no tools
  - the turn's origin is "bench": nothing learns from it
Tools that change things are never reachable: the local and one-shot paths have none.
"""
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

MARK = ".replay-throwaway"
PREFIX = "REPLAY_RESULT "
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def die(msg):
    print(PREFIX + json.dumps({"error": msg}), flush=True)
    sys.exit(3)


def check_sandbox():
    home = Path(os.environ.get("WAGGLE_HOME") or "")
    tree = Path(os.environ.get("REPLAY_TREE") or "")
    if not home.name or not (home / MARK).exists():
        die("WAGGLE_HOME is not a replay throwaway home")
    if not tree.is_dir():
        die("REPLAY_TREE is not a directory")
    real = Path(os.environ.get("REPLAY_REAL_HOME") or "")
    if real.name and home.resolve() == real.resolve():
        die("refusing to run in the real Waggle home")
    sys.path.insert(0, str(tree))
    return home, tree


def wall_off_network():
    """Only loopback connections work from here on."""
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def host_of(addr):
        return addr[0] if isinstance(addr, tuple) and addr else str(addr)

    def refuse(addr):
        raise OSError(f"replay sandbox: connection to {host_of(addr)} refused (loopback only)")

    def connect(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6) and host_of(addr) not in LOOPBACK:
            refuse(addr)
        return real_connect(self, addr)

    def connect_ex(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6) and host_of(addr) not in LOOPBACK:
            refuse(addr)
        return real_connect_ex(self, addr)

    def getaddrinfo(host, *a, **k):
        if host not in LOOPBACK and host not in (None, ""):
            raise socket.gaierror(f"replay sandbox: name lookup for {host} refused")
        return real_getaddrinfo(host, *a, **k)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo


def loaded_from(tree):
    import waggle
    here = Path(waggle.__file__).resolve()
    if Path(tree).resolve() not in here.parents:
        die(f"waggle was imported from {here}, not from the tree under test")


LOCAL_LABEL_RE = re.compile(r"\n\n\(Answered by Waggle's small local model[^\n]*\)\s*$")


def one_shot_claude(chat_engine, claude_cli):
    """The Claude answer when the caller allowed it: one lean `claude -p`, no tools, the engine's own context."""
    def answer(chat, text, context, settings):
        from waggle import budget
        soul = ""
        try:
            soul = (Path(os.environ["REPLAY_TREE"]) / "persona" / "SOUL.md").read_text(encoding="utf-8")
        except OSError:
            pass
        prompt = (context + "\n\n" if context else "") + text
        with budget.background("replay"):
            res = claude_cli.ask(prompt, model=os.environ.get("REPLAY_CLAUDE_MODEL") or settings.router_model,
                                 system=soul or None, effort="low", purpose="replay", timeout=180)
        return (res.get("text") or "").strip()
    return answer


def cmd_answer(req):
    home, tree = check_sandbox()
    wall_off_network()
    os.environ["WAGGLE_ORIGIN"] = "bench"
    from waggle import chat_engine, claude_cli
    from waggle.config import Settings
    from waggle.delivery import CollectDelivery
    loaded_from(tree)
    s = Settings.load()
    use_claude = bool(req.get("claude"))
    if not use_claude:  # the Claude CLI does not exist for this run
        chat_engine.claude_available = lambda settings: (False, "replay: Claude is off")
        claude_cli.find_claude = lambda: None
        local = chat_engine.local_answer
    else:
        chat_engine.claude_available = lambda settings: (False, "replay: one-shot Claude answers only")
        local = one_shot_claude(chat_engine, claude_cli)
    d = CollectDelivery()
    t0 = time.time()
    rec = chat_engine.handle_inbound(req.get("chat") or "replay:case", req["text"], kind="text", platform="test",
                                     origin="bench", delivery=d, settings=s, local=local)
    reply = LOCAL_LABEL_RE.sub("", rec.reply or "").strip()
    failed = bool(getattr(rec, "failure_notice", False))
    return {"reply": reply, "answered_by": "claude-oneshot" if use_claude and not failed else rec.answered_by,
            "failure": failed,
            "why": (getattr(rec, "fallback_reason", "") or "")[:300], "ms": int((time.time() - t0) * 1000),
            "context": getattr(rec, "context", None) or {}}


def cmd_golden(req):
    home, tree = check_sandbox()
    wall_off_network()
    os.environ["WAGGLE_ORIGIN"] = "bench"
    from waggle import evals
    from waggle.config import Settings
    loaded_from(tree)
    data = evals.load_golden(req["golden"])
    out = evals.run(settings=Settings.load(), ids=req.get("ids") or None, quick=bool(req.get("quick")), claude=False,
                    save=False, golden=data)
    return {"results": [{k: r.get(k) for k in ("id", "category", "status", "detail", "ms", "model")}
                        for r in out["results"]], "notes": out.get("notes", [])}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        die("bad request")
    fn = {"answer": cmd_answer, "golden": cmd_golden}.get(cmd)
    if fn is None:
        die("usage: worker.py answer|golden")
    try:
        result = fn(req)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"[:300]}
    print(PREFIX + json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
