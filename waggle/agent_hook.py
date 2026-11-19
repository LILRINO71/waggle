"""Claude Code PreToolUse hook: every tool call a Waggle agent makes is checked by waggle.policy.

Claude Code runs:  python <repo>/waggle/agent_hook.py   with the hook event as JSON on stdin.
We print a permission decision (allow / deny) as JSON. "ask" is turned into deny + an approval id,
because nobody is at the keyboard during a headless run; Waggle relays the request to the user.
"""
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from waggle import policy  # noqa: E402


def handle(event):
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    session = os.environ.get("WAGGLE_SESSION", "")
    d = policy.check(tool, args, session=session, source=os.environ.get("WAGGLE_AGENT", "agent"))
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
        out = handle(event)
    except Exception as e:  # fail closed
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": f"Waggle policy check failed ({e}); not running it."}}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
