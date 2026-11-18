"""Append-only event log: the raw data for the Waggle benchmark.

Every route decision, Claude call, build job, finding and notification lands here with a timestamp,
so later we can measure success rate, latency, quota cost, autonomy (how often you had to step in)
and proactivity (whether unsolicited suggestions were useful)."""
import json
import time
import uuid

from .config import data_path

KINDS = {"route", "claude_call", "job", "finding", "notify", "feedback", "project_update", "task", "error"}


def log(kind, **fields):
    row = {"id": uuid.uuid4().hex[:12], "time": time.time(), "kind": kind, **fields}
    with open(data_path("events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return row


def read(kind=None, since=0.0):
    p = data_path("events.jsonl")
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("time", 0) >= since and (kind is None or r.get("kind") == kind):
            out.append(r)
    return out


def summary(since=0.0):
    """Quick numbers for `waggle stats` and the future dashboard."""
    ev = read(since=since)
    routes = [e for e in ev if e["kind"] == "route"]
    by_route = {}
    for e in routes:
        by_route[e.get("route")] = by_route.get(e.get("route"), 0) + 1
    findings = [e for e in ev if e["kind"] == "finding"]
    fb = [e for e in ev if e["kind"] == "feedback"]
    useful = sum(1 for e in fb if e.get("useful") is True)
    jobs_done = [e for e in ev if e["kind"] == "job" and e.get("status") in ("done", "failed")]
    return {
        "messages": len(routes),
        "routes": by_route,
        "local_share": round(by_route.get("local", 0) / len(routes), 3) if routes else None,
        "avg_route_latency_s": round(sum(e.get("latency_s", 0) for e in routes) / len(routes), 2) if routes else None,
        "findings": len(findings),
        "findings_sent": sum(1 for e in findings if e.get("delivered")),
        "feedback": len(fb), "useful_rate": round(useful / len(fb), 3) if fb else None,
        "jobs_finished": len(jobs_done),
        "job_success_rate": (round(sum(1 for e in jobs_done if e["status"] == "done") / len(jobs_done), 3)
                             if jobs_done else None),
    }
