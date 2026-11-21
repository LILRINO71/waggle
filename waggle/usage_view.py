"""`python -m waggle.usage_view`: Waggle's own window numbers plus ccusage's view by 5-hour block and by session.

Read-only. waggle.quota says HOW FULL the window is (the real rate_limit_event); ccusage reads the Claude Code JSONL logs
in ~/.claude/projects (the ones `claude -p` runs write too) and says WHICH sessions and blocks used the tokens. It runs
offline (cached prices, no network), pinned at 20.0.51 under ~/.waggle-tools/ccusage:
    npm install --prefix ~/.waggle-tools/ccusage --ignore-scripts ccusage@20.0.51
The dollar figures are ccusage's API-price equivalent of the tokens, not money charged on a Pro plan.

waggle/quota.py is a protected safety file, so this lives beside it rather than as `python -m waggle.quota status`.
Off switch: config `ccusage_enabled` false. Removal: delete this file and tests/test_usage_view.py.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import Settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def entry():
    p = Path(os.environ.get("WAGGLE_CCUSAGE") or Path.home() / ".waggle-tools" / "ccusage" / "node_modules" / "ccusage" / "src" / "cli.js")
    return p if p.exists() else None


def enabled(settings=None):
    return bool(getattr(settings or Settings.load(), "ccusage_enabled", True))


def fetch(kind, days=2, timeout=90):
    """ccusage `blocks` or `session` as parsed JSON; raises RuntimeError with the reason."""
    node, js = shutil.which("node"), entry()
    if not node or not js:
        raise RuntimeError("ccusage is not installed (see waggle/usage_view.py)")
    since = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
    p = subprocess.run([node, str(js), kind, "--json", "--offline", "--since", since], capture_output=True,
                       timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                       env=dict(os.environ, NO_COLOR="1"))
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).decode("utf-8", "replace").strip()[:160] or f"exit {p.returncode}")
    return json.loads(p.stdout.decode("utf-8", "replace"))


def _t(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%a %H:%M")


def _m(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def blocks_lines(data, limit=6):
    rows = [b for b in data.get("blocks", []) if not b.get("isGap")][-limit:]
    out = []
    for b in rows:
        tc = b.get("tokenCounts", {})
        models = ",".join(sorted({m.replace("claude-", "").rsplit("-", 2)[0] for m in b.get("models", [])}))
        out.append(f"  {_t(b['startTime'])}-{_t(b['endTime'])}{' (active)' if b.get('isActive') else ''}: "
                   f"{_m(b.get('totalTokens', 0))} tokens ({_m(tc.get('outputTokens', 0))} written), "
                   f"{b.get('entries', 0)} messages, ~${b.get('costUSD', 0):.0f} at API prices [{models}]")
    return out


def sessions_lines(data, limit=5):
    rows = sorted(data.get("session", []), key=lambda s: s.get("totalTokens", 0), reverse=True)[:limit]
    out = []
    for s in rows:
        models = ",".join(sorted({m.replace("claude-", "").rsplit("-", 2)[0] for m in s.get("modelsUsed", [])}))
        last = _t(s["metadata"]["lastActivity"]) if s.get("metadata", {}).get("lastActivity") else "?"
        out.append(f"  {str(s.get('period', '?'))[:8]}: {_m(s.get('totalTokens', 0))} tokens "
                   f"({_m(s.get('outputTokens', 0))} written), ~${s.get('totalCost', 0):.0f}, last {last} [{models}]")
    return out


def view(settings=None, fetcher=fetch, quota_status=None):
    """The text `status` prints: the window line from waggle.quota, then ccusage's blocks and busiest sessions."""
    s = settings or Settings.load()
    if quota_status is None:
        from . import quota
        quota_status = quota.status(s)
    q = quota_status
    lines = [f"Window: {q['window_pct']}% used ({q['source']}), resets {q['resets_at']}; Waggle's share "
             f"{q['waggle_units']} units; seven-day {q['seven_day_pct']}%"]
    if not enabled(s):
        return "\n".join(lines + ["ccusage view is off (config ccusage_enabled)"])
    try:
        lines += ["By 5-hour block (ccusage, offline):"]
        lines += blocks_lines(fetcher("blocks")) or ["  nothing in the last 2 days"]
        lines += ["Busiest sessions, last 2 days:"]
        lines += sessions_lines(fetcher("session")) or ["  none"]
    except Exception as e:  # noqa: BLE001
        lines.append(f"ccusage view unavailable: {str(e)[:120]}")
    return "\n".join(lines)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "status"
    if cmd == "status":
        print(view())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
