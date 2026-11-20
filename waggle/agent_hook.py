"""Claude Code PreToolUse hook: every tool call a Waggle agent makes is checked by waggle.policy.

Claude Code runs:  python <repo>/waggle/agent_hook.py   with the hook event as JSON on stdin.
We print a permission decision (allow / deny) as JSON. "ask" is turned into deny + an approval id,
because nobody is at the keyboard during a headless run; Waggle relays the request to the user.
"""
import hashlib
import importlib
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Integrity gate. Deliberately self-contained and importing nothing from waggle until the hashes match: it must not
# trust the files it is checking. Keep _CHECKED equal to guard.HOOK_CHECKED (tests/test_guard.py asserts it).
_GUARD_DIR = Path.home() / ".waggle-guard"
_ROOT = Path(__file__).resolve().parent.parent
_CHECKED = ("waggle/__init__.py", "waggle/policy.py", "waggle/agent_hook.py", "waggle/guard.py",
            "waggle/security.py")
_TRUSTED_FOR_AUDIT = {"waggle/agent_hook.py", "waggle/security.py"}


def integrity_failure():
    """None when the checked files match the manifest, else [{file, expected, actual}]. Any error is a failure."""
    try:
        try:
            expected = json.loads((_GUARD_DIR / "manifest.json").read_text(encoding="utf-8"))["files"]
        except FileNotFoundError:
            return [{"file": "manifest.json", "expected": "an approved manifest", "actual": "missing"}]
        if not isinstance(expected, dict):
            raise ValueError("files is not a table")
        bad = []
        for rel in _CHECKED:
            try:
                actual = hashlib.sha256((_ROOT / rel).read_bytes()).hexdigest()
            except OSError:
                actual = None
            if actual is None or expected.get(rel) != actual:
                bad.append({"file": rel, "expected": expected.get(rel), "actual": actual})
        if not bad:
            try:
                importlib.import_module("waggle.guard")
            except Exception as e:
                bad.append({"file": "waggle/guard.py", "expected": "importable",
                            "actual": f"import failed ({type(e).__name__})"})
        return bad or None
    except Exception as e:
        return [{"file": "manifest.json", "expected": "a readable manifest",
                 "actual": f"check failed ({type(e).__name__})"}]


def _record_violation(bad, tool, args):
    """violation.json for the rest of Waggle to read, plus an integrity_violation row in the audit log."""
    try:
        _GUARD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _GUARD_DIR / f"violation.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"time": time.time(), "files": bad}, indent=1), encoding="utf-8")
        os.replace(tmp, _GUARD_DIR / "violation.json")
    except Exception:
        pass
    reason = "integrity_violation: " + ", ".join(b["file"] for b in bad)
    try:
        if {b["file"] for b in bad} <= _TRUSTED_FOR_AUDIT:
            from waggle import policy
            policy.audit(tool, args, policy.Decision("integrity_violation", reason), os.environ.get("WAGGLE_AGENT", "agent"))
        else:  # policy.py itself can't be trusted: same row format, written without importing anything
            home = Path(os.environ.get("WAGGLE_HOME") or Path.home() / ".waggle")
            home.mkdir(parents=True, exist_ok=True)
            row = {"time": time.time(), "source": os.environ.get("WAGGLE_AGENT", "agent"), "tool": tool,
                   "decision": "integrity_violation", "reason": reason, "call": f"{tool}"}
            with open(home / "audit.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def lockdown(bad, tool="", args=None):
    _record_violation(bad, tool, args or {})
    names = ", ".join(b["file"] for b in bad)
    why = (f"Waggle's safety files changed or could not be verified ({names}), so every action is blocked until the "
           f"user re-approves them. Do not retry or work around this. Tell the user in one plain sentence to run "
           f"`python -m waggle.guard approve` (after checking the change), then try again.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def handle(event):
    from waggle import policy
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    session = os.environ.get("WAGGLE_SESSION", "")
    d = policy.check(tool, args, session=session, source=os.environ.get("WAGGLE_AGENT", "agent"),
                     cwd=event.get("cwd"))
    if d.allowed:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "permissionDecisionReason": d.reason or "allowed by Waggle policy"}}
    if d.action == policy.DENY:
        why = f"Blocked by Waggle's safety policy: {d.reason}. Do not retry or work around this; tell the user."
    else:
        code = (policy.get(d.approval_id) or {}).get("code", "")
        why = (f"NEEDS_APPROVAL [{code}]: {d.reason}. You cannot do this without the user's OK. Stop trying "
               f"alternatives for this step; finish what you can and tell the user in one plain sentence what you "
               f"want to do, so they can reply 'yes' (or 'approve {code}'). Never show internal ids or hook details.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
        bad = integrity_failure()
        out = lockdown(bad, event.get("tool_name", ""), event.get("tool_input")) if bad else handle(event)
    except Exception as e:  # fail closed
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": f"Waggle policy check failed ({e}); not running it."}}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
