"""Scoreboard: what the autonomous work achieved this week, per goal, from the records only.

    python -m waggle.scoreboard [--days 7]

weekly() returns the same data for the dashboard: per goal the steps attempted, outcomes proven, unproven,
failed, refused by the charter, waiting for approval, held back by the allowance, the background units the
steps spent (budget.db usage rows of the step's model calls), and each indicator's value at the start of the
week, now, and the change. Only readings measured by code are shown; a step's own report never is.
"""
import argparse
import datetime as dt
import sys

from . import autonomy_store as store_
from . import autowork, goals


def _indicator(con, goal_id, name, since):
    start = con.execute("SELECT value FROM readings WHERE goal_id=? AND indicator=? AND time<? AND value IS NOT NULL "
                        "ORDER BY time DESC LIMIT 1", (goal_id, name, since)).fetchone()
    week = [r[0] for r in con.execute("SELECT value FROM readings WHERE goal_id=? AND indicator=? AND time>=? AND "
                                      "value IS NOT NULL ORDER BY time", (goal_id, name, since))]
    first = start[0] if start else (week[0] if week else None)
    latest = week[-1] if week else first
    change = None if first is None or latest is None else latest - first
    return {"start": first, "latest": latest, "change": change, "readings": len(week)}


def weekly(now=None, days=7):
    """{since, until, autonomy_enabled, max_steps_per_day, goals: [...], candidates: {status: n}}"""
    now = store_.now_or(now)
    since = now - days * 86400
    out = {"since": since, "until": now, "autonomy_enabled": goals.autonomy_enabled(),
           "max_steps_per_day": goals.max_steps_per_day(), "goals": [], "candidates": {}}
    con = store_.db()
    try:
        for g in goals.all_goals():
            rows = [r for r in autowork.steps(g["id"], since) if r["time"] <= now]
            n = {k: sum(1 for r in rows if r["outcome"] == k) for k in ("proven", "unproven", "failed",
                                                                        "charter_refused", "needs_approval",
                                                                        "refused")}
            out["goals"].append({
                "id": g["id"], "title": g["title"], "status": g["status"],
                "expires": g["charter"].get("expires"),
                "attempted": sum(1 for r in rows if r["outcome"] in autowork.COUNTED),
                **n, "units": round(sum(r["units"] or 0 for r in rows), 4),
                "indicators": [{"name": ind["name"], "how": ind["how"], **_indicator(con, g["id"], ind["name"], since)}
                               for ind in g["indicators"]]})
        for status, count in con.execute("SELECT status, COUNT(*) FROM candidates GROUP BY status"):
            out["candidates"][status] = count
    finally:
        con.close()
    return out


def _num(v):
    return "-" if v is None else f"{v:g}"


def render(data):
    since = dt.datetime.fromtimestamp(data["since"]).strftime("%b %d")
    until = dt.datetime.fromtimestamp(data["until"]).strftime("%b %d")
    lines = [f"Autonomous work, {since} to {until}  (autonomy {'ON' if data['autonomy_enabled'] else 'off'}, "
             f"at most {data['max_steps_per_day']} steps a day)", ""]
    lines.append(f"{'goal':24} {'status':9} {'tried':>5} {'proven':>6} {'unproven':>8} {'failed':>6} "
                 f"{'refused':>7} {'held':>4} {'units':>6}")
    for g in data["goals"]:
        lines.append(f"{g['title'][:24]:24} {g['status']:9} {g['attempted']:5} {g['proven']:6} {g['unproven']:8} "
                     f"{g['failed']:6} {g['charter_refused']:7} {g['refused']:4} {g['units']:6.3f}")
        for ind in g["indicators"]:
            ch = ind["change"]
            lines.append(f"    {ind['name'][:40]:40} {_num(ind['start']):>6} -> {_num(ind['latest']):<6}"
                         + ("" if ch is None else f" ({'+' if ch >= 0 else ''}{ch:g})"))
    if not data["goals"]:
        lines.append("  no goals yet (python -m waggle.goals seed)")
    cands = data["candidates"]
    lines += ["", "Repeated work: " + (", ".join(f"{v} {k}" for k, v in sorted(cands.items())) or "nothing mined yet")]
    lines.append("refused = outside the charter; held = the background allowance said no (not a step)")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m waggle.scoreboard")
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    print(render(weekly(days=args.days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
