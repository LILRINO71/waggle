"""Prove the learning loop on a COPY of the real data (never the live home, never the live vault).

It copies ~/.waggle's databases and the vault into a temporary sandbox, plants one wrong fact, one wrong answer and
the owner's correction as ordinary user turns (the live canary turns are not learning input, so the loop cannot be
proved through tools/testclient/ask.py), runs the loop with the real embedder and the real local model (and never
Claude), prints the ledger, the evening-note lines and the undo, and checks each step.

  python tools/learnloop_proof.py [--keep] [--wrong "..."] [--correction "..."]

Exit code 0 when every step held. The sandbox is deleted unless --keep. Takes about a minute (embeddings and the
held-out quiz sample run locally)."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ASKED = "when does my robotics team meet?"
WRONG = "Your robotics team meets on Monday and Wednesday."
OLD_FACT = "robotics team meets on Monday and Wednesday"
CORRECTION = "No, my robotics team meets on Tuesday and Thursday"
CHAT = "proof:chat"


def build_sandbox(live):
    box = Path(tempfile.mkdtemp(prefix="learnloop-proof-"))
    home = box / "home"
    home.mkdir()
    for name in ("traces.db", "knowledge.db", "memory.db", "graph.db"):
        if (live / name).exists():
            shutil.copy2(live / name, home / name)
    cfg = json.loads((live / "config.json").read_text(encoding="utf-8")) if (live / "config.json").exists() else {}
    vault_src = Path(cfg.get("vault") or Path.home() / "WaggleVault")
    if vault_src.exists():
        shutil.copytree(vault_src, box / "vault", copy_function=shutil.copy2)  # mtimes kept: the index stays valid
    cfg["vault"] = str(box / "vault")
    cfg["quiet_hours"] = [0, 0]
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return box, home


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--wrong", default=WRONG)
    ap.add_argument("--correction", default=CORRECTION)
    ap.add_argument("--old-fact", default=OLD_FACT)
    args = ap.parse_args(argv)
    live = Path(os.environ.get("WAGGLE_LIVE_HOME") or Path.home() / ".waggle")
    box, home = build_sandbox(live)
    os.environ["WAGGLE_HOME"] = str(home)  # before waggle is imported: config.HOME reads it once
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from waggle import chat_engine, learn, learnloop, traces, vault
    from waggle.config import Settings
    assert str(home) == str(__import__("waggle.config", fromlist=["x"]).HOME), "the sandbox home was not used"
    s = Settings.load()
    checks, out = [], {"sandbox": str(box)}

    def check(name, ok, detail=""):
        checks.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

    def turn(request, reply):
        return traces.record(request, session=CHAT, chat=CHAT, platform="telegram", origin="user", reply=reply,
                             route="sonnet")
    old_turn = turn("my " + args.old_fact, "Noted.")
    ok, old_id = learn.file_fact({"kind": "about", "key": "routine", "text": args.old_fact}, s, source="digest proof",
                                 turn=traces.get(old_turn))
    check("an old fact is filed", ok, str(old_id))
    answered = turn(ASKED, args.wrong)
    corrected = turn(args.correction, "Sorry, I will fix that.")
    t0 = time.time()
    rep = learnloop.run(s, now=time.time(), asker=lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("the proof never calls Claude")))
    out["run_s"] = round(time.time() - t0, 1)
    rows = [r for r in learnloop.rows() if r["source"] == "correction"]
    check("one correction signal", rep["signals"]["correction"] == 1 and len(rows) == 1, str(rep["signals"]))
    row = rows[0] if rows else {"status": "none", "id": 0, "summary": "", "reason": "", "checks": {}, "change": {}}
    print(json.dumps({k: row[k] for k in ("id", "status", "kind", "summary", "reason", "checks", "change")}, indent=1,
                     default=str))
    check("the answered turn carries the owner's verdict", traces.get(answered)["feedback"] == -1)
    check("the correction was kept (re-check and held-out passed)", row["status"] == "kept", row["reason"])
    if row["status"] == "kept":
        active = [f["text"] for f in learn.facts("active")]
        check("the new fact is active, the old one replaced", args.correction.split(",", 1)[-1].strip().lower()
              in " ".join(active).lower() and args.old_fact not in active)
        line = learnloop.evening_lines(time.time(), settings=s)
        print(line)
        check("the evening note names it with an undo code", f"undo L{row['id']}" in line)
        kind, text = chat_engine.approval_preflight(CHAT, f"undo L{row['id']}")
        print(text)
        check("undo answers with no model and restores the old fact", kind == "reply" and args.old_fact in
              [f["text"] for f in learn.facts("active")] and "Tuesday" not in vault.read("Me/Profile.md", s))
    out["checks_passed"] = f"{sum(checks)}/{len(checks)}"
    print(json.dumps(out))
    if not args.keep:
        shutil.rmtree(box, ignore_errors=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
