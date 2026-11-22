"""Waggle Command Center: one local page that shows everything Waggle is doing.

    python -m waggle.dashboard            -> http://127.0.0.1:8897  (started hidden at login by Waggle Gateway.vbs)

Read-only, 127.0.0.1 only, stdlib only. /api/state returns one JSON snapshot; the page refreshes it every 5 s.
Heavy parts (memory health, model catalogue, skills) are cached for a minute so the page stays instant.
"""
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):  # run as a script: python waggle/dashboard.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "waggle"

from .config import Settings, data_path  # noqa: E402

PORT = 8897
_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = {"error": f"{type(e).__name__}: {e}"}
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _up(url, timeout=0.6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:  # it answered, so it's running (e.g. 404/405 on GET /)
        return True
    except Exception:
        return False


def health():
    s = Settings.load()
    from . import bridge, claude_cli
    loaded = []
    try:
        with urllib.request.urlopen(f"{s.ollama_url}/api/ps", timeout=0.8) as r:
            loaded = [m["name"] for m in json.loads(r.read()).get("models", [])]
        ollama = True
    except Exception:
        ollama = False
    return {"gateway": bridge.Client(timeout=1).health(), "ollama": ollama, "ollama_loaded": loaded,
            "kokoro": _up("http://127.0.0.1:8899/"), "claude_cli": bool(claude_cli.find_claude()),
            "vault": Path(s.vault).exists()}


def _jsonl_tail(name, n=30):
    p = data_path(name)
    if not p.exists():
        return []
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 200_000))
        lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def activity(n=25):
    try:
        con = sqlite3.connect(data_path("traces.db"))
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT id, time, route, model, agent, latency_s, ok, feedback, substr(request,1,160) AS request, "
            "(SELECT COUNT(*) FROM steps s WHERE s.trace_id=t.id) AS tools FROM traces t ORDER BY id DESC LIMIT ?", (n,))]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def cron_jobs():
    p = Path.home() / ".hermes" / "cron" / "jobs.json"
    try:
        jobs = json.loads(p.read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        return []
    out = []
    for j in jobs if isinstance(jobs, list) else jobs.values():
        out.append({"name": j.get("name"), "schedule": j.get("schedule_display") or j.get("schedule"),
                    "enabled": j.get("enabled", True), "last": j.get("last_run_at") or j.get("last_run"),
                    "status": j.get("last_status")})
    return out


def snapshot():
    from . import agents, jobs, policy, prompts, quota, routines, skills, traces
    s = Settings.load()
    day = time.time() - 86400
    js = sorted(jobs.load().values(), key=lambda j: j.get("created", 0), reverse=True)
    return {
        "time": time.time(),
        "health": _cached("health", 10, health),
        "quota": _cached("quota", 30, quota.status),
        "today": _cached("today", 15, lambda: traces.stats(day)),
        "week": _cached("week", 60, lambda: traces.stats(time.time() - 7 * 86400)),
        "activity": activity(),
        "jobs": [{k: j.get(k) for k in ("id", "title", "status", "created", "project")} for j in js[:8]],
        "agents": _cached("agents", 30, agents.status),
        "routines": routines.listing(),
        "recipes": _cached("recipes", 60, lambda: __import__("waggle.recipes", fromlist=["x"]).summary(s)),
        "objectives": _cached("objectives", 10, lambda: __import__("waggle.objectives", fromlist=["x"]).status()[:8]),
        "cron": _cached("cron", 30, cron_jobs),
        "approvals": policy.pending(),
        "ops": _cached("ops", 5, lambda: __import__("waggle.objectives", fromlist=["x"]).ops()),
        "budget": _cached("budget", 30, lambda: __import__("waggle.budget", fromlist=["x"]).status(s)),
        "audit": [a for a in _jsonl_tail("audit.jsonl", 200) if a.get("decision") != "allow"][-12:],
        "notifications": _jsonl_tail("notifications.jsonl", 10),
        "skills": _cached("skills", 300, skills.stats),
        "learning": {"router_prompt": prompts.versions("router"),
                     "candidates": len(_load_json("skill_candidates.json") or []),
                     "events": [e for e in _jsonl_tail("events.jsonl", 400) if e.get("kind") == "learning"][-6:]},
        "memory": _cached("memory", 120, lambda: __import__("waggle.memory_care", fromlist=["x"]).status(s)),
        "models": _cached("models", 600, _models_summary),
        "errors": [e for e in _jsonl_tail("events.jsonl", 400) if e.get("kind") == "error"][-8:]
                  + [{"time": a["time"], "text": f"{a['route']}: {a['request'][:80]}"} for a in activity(60) if not a["ok"]][:6],
    }


def _load_json(name):
    p = data_path(name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except ValueError:
        return None


def _models_summary():
    cat = _load_json("models.json")
    if not cat or time.time() - cat.get("time", 0) > 86400:
        from . import models
        cat = models.catalog()
    return {"hardware": cat.get("hardware"), "local": [{k: m.get(k) for k in ("name", "size_gb", "where", "vision",
                                                                                  "tools")} for m in cat.get("local", [])],
            "cloud": [c["name"] for c in cat.get("cloud", [])]}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            return self._send(200, json.dumps(snapshot(), default=str), "application/json")
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        self._send(404, "not found", "text/plain")


def serve(port=PORT):
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.daemon_threads = True
    srv.serve_forever()


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT
    try:
        serve(port)
    except OSError:
        pass  # already running


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waggle Command Center</title>
<style>
:root{--bg:#0d0f13;--panel:#151922;--panel2:#1b2030;--line:#262c3b;--text:#e8ebf2;--dim:#8a93a6;--honey:#f5b301;
--ok:#3ddc84;--warn:#ffb020;--bad:#ff5d5d;--cyan:#3ed2f0;--violet:#a78bfa;--r:14px}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#2a2410 0,transparent 60%),
radial-gradient(900px 500px at 110% 0,#0f2430 0,transparent 55%),var(--bg);color:var(--text);
font:14px/1.45 "Segoe UI Variable","Segoe UI",system-ui,sans-serif;min-height:100vh}
header{display:flex;align-items:center;gap:14px;padding:22px 28px 10px}
.logo{width:38px;height:38px;border-radius:11px;background:conic-gradient(from 200deg,var(--honey),#ffde7a,var(--honey));
display:grid;place-items:center;color:#1a1300;font-weight:800;font-size:19px;box-shadow:0 0 24px #f5b30155}
h1{font-size:20px;margin:0;letter-spacing:.2px}.sub{color:var(--dim);font-size:12.5px}
.pills{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}.pill{display:flex;align-items:center;gap:6px;padding:5px 11px;
border-radius:99px;background:var(--panel);border:1px solid var(--line);font-size:12.5px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}.dot.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.dot.bad{background:var(--bad);box-shadow:0 0 8px var(--bad)}
main{display:grid;grid-template-columns:repeat(12,1fr);gap:16px;padding:12px 28px 32px}
.card{background:linear-gradient(180deg,var(--panel),#12151c);border:1px solid var(--line);border-radius:var(--r);
padding:16px 18px;min-width:0}.card h2{font-size:12px;letter-spacing:.9px;text-transform:uppercase;color:var(--dim);
margin:0 0 12px;display:flex;align-items:center;gap:8px}.card h2 b{color:var(--text);font-weight:600}
.s3{grid-column:span 3}.s4{grid-column:span 4}.s5{grid-column:span 5}.s6{grid-column:span 6}.s7{grid-column:span 7}.s8{grid-column:span 8}.s12{grid-column:span 12}
@media(max-width:1100px){.s3,.s4,.s5,.s6,.s7,.s8{grid-column:span 12}}
.big{font-size:30px;font-weight:700;letter-spacing:-.5px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.kpi{background:var(--panel2);border-radius:10px;padding:10px 12px}.kpi .v{font-size:20px;font-weight:700}
.kpi .l{color:var(--dim);font-size:11.5px}
.bar{height:10px;border-radius:99px;background:#232a3a;overflow:hidden}.bar i{display:block;height:100%;
background:linear-gradient(90deg,var(--ok),var(--honey) 70%,var(--bad));border-radius:99px;transition:width .6s}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 6px;border-bottom:1px solid #1f2533;text-align:left;
vertical-align:top}th{color:var(--dim);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.6px}
tr:last-child td{border-bottom:0}.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11.5px;
background:#232a3a;color:var(--dim);white-space:nowrap}.t-act{background:#3a2f0b;color:var(--honey)}
.t-sonnet,.t-answer{background:#0f2f3a;color:var(--cyan)}.t-opus{background:#2a1f45;color:var(--violet)}
.t-local{background:#1d3324;color:var(--ok)}.t-agent{background:#3a2f0b;color:var(--honey)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.dim{color:var(--dim)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.feed{max-height:420px;overflow:auto}.empty{color:var(--dim);font-style:italic;padding:6px 0}
.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #1f2533}.row:last-child{border:0}
.chips{display:flex;flex-wrap:wrap;gap:6px}.live{animation:pulse 1.6s infinite}@keyframes pulse{50%{opacity:.35}}
footer{color:var(--dim);font-size:12px;padding:0 28px 24px}
</style></head><body>
<header><div class="logo">W</div><div><h1>Waggle Command Center</h1><div class="sub" id="updated">connecting…</div></div>
<div class="pills" id="health"></div></header>
<main>
<section class="card s8"><h2>Today</h2><div class="kpis" id="kpis"></div></section>
<section class="card s4"><h2>Claude budget <b id="qpct"></b></h2><div class="bar"><i id="qbar" style="width:0"></i></div>
<div class="sub" style="margin-top:8px" id="qtext"></div></section>
<section class="card s7"><h2>Activity</h2><div class="feed"><table id="activity"></table></div></section>
<section class="card s5"><h2>Needs you</h2><div id="approvals"></div><h2 style="margin-top:16px">Running</h2><div id="running"></div></section>
<section class="card s5"><h2>Background budget <b id="bpct"></b></h2><div class="bar"><i id="bbar" style="width:0"></i></div>
<div class="sub" style="margin-top:8px" id="btext"></div><div id="bmodels" style="margin-top:10px"></div><div id="bdecisions" style="margin-top:10px"></div></section>
<section class="card s7"><h2>Background work</h2><div id="ops"></div></section>
<section class="card s12"><h2>Objectives</h2><div id="objectives"></div></section>
<section class="card s4"><h2>Agents</h2><div id="agents"></div></section>
<section class="card s4"><h2>Routines</h2><div id="routines"></div></section>
<section class="card s4"><h2>Learning</h2><div id="learning"></div></section>
<section class="card s4"><h2>Skills</h2><div id="skills"></div></section>
<section class="card s4"><h2>Memory</h2><div id="memory"></div></section>
<section class="card s4"><h2>Models</h2><div id="models"></div></section>
<section class="card s6"><h2>Safety log</h2><div id="audit"></div></section>
<section class="card s6"><h2>Errors &amp; notifications</h2><div id="errors"></div></section>
</main><footer>Local only (127.0.0.1). Refreshes every 5 s.</footer>
<script>
const $=id=>document.getElementById(id);const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ago=t=>{if(!t)return"–";const d=(Date.now()/1000-t);if(d<60)return Math.round(d)+"s ago";if(d<3600)return Math.round(d/60)+"m ago";
if(d<86400)return Math.round(d/3600)+"h ago";return Math.round(d/86400)+"d ago"};
const tag=r=>{const k=(r||"").startsWith("agent")?"agent":r;return `<span class="tag t-${esc(k)}">${esc(r==="sonnet"?"answer":r)}</span>`};
const empty=t=>`<div class="empty">${t}</div>`;
function render(d){
 $("updated").textContent="Updated "+new Date(d.time*1000).toLocaleTimeString();
 const h=d.health||{};$("health").innerHTML=[["Gateway",h.gateway],["Local AI",h.ollama],["Voice",h.kokoro],["Claude",h.claude_cli],["Vault",h.vault]]
  .map(([n,v])=>`<span class="pill"><span class="dot ${v?"ok":"bad"}"></span>${n}</span>`).join("");
 const t=d.today||{};const fb=t.feedback||{};
 $("kpis").innerHTML=[["Turns",t.turns||0],["Success",t.ok_rate!=null?Math.round(t.ok_rate*100)+"%":"–"],
  ["Median reply",t.median_latency_s!=null?t.median_latency_s+" s":"–"],["Tool steps",t.tool_steps||0],
  ["👍 / 👎",(fb.good||0)+" / "+(fb.bad||0)],["Approvals asked",t.denied||0],["Tool failures",t.tool_failures||0],
  ["Week turns",(d.week||{}).turns||0]].map(([l,v])=>`<div class="kpi"><div class="v">${esc(v)}</div><div class="l">${l}</div></div>`).join("");
 const q=d.quota||{};$("qpct").textContent=q.window_pct!=null?q.window_pct+"%":"";$("qbar").style.width=Math.min(100,q.window_pct||0)+"%";
 $("qtext").innerHTML=q.error?esc(q.error):`5-hour window: ${q.window_used_units} of ~${q.window_estimate_units} units · Waggle used ${q.waggle_units}`+
  (q.blocked_until&&q.blocked_until*1000>Date.now()?` · <span class="bad">limit hit, waiting</span>`:"");
 const a=d.activity||[];$("activity").innerHTML=a.length?`<tr><th>When</th><th>Route</th><th>Request</th><th>Tools</th><th>Time</th></tr>`+
  a.map(r=>`<tr><td class="dim">${ago(r.time)}</td><td>${tag(r.route)}</td><td>${esc(r.request)}${r.feedback<0?' <span class="bad">✗ corrected</span>':r.feedback>0?' <span class="ok">✓</span>':""}</td>
  <td>${r.tools||""}</td><td class="${r.ok?"":"bad"}">${r.latency_s!=null?r.latency_s+"s":""}</td></tr>`).join(""):empty("No conversations yet.");
 const ap=(d.ops||{}).waiting||[];const left=t=>{const m=Math.max(0,Math.round((t-Date.now()/1000)/60));return m>=120?Math.round(m/60)+" h":m+" min"};
 $("approvals").innerHTML=ap.length?ap.map(x=>`<div class="row"><span><span class="tag t-act mono">${esc(x.code)}</span> ${esc(x.summary.slice(0,70))}<br><span class="dim">${esc(x.reason)}${x.title?" · objective "+esc(x.title):" · "+esc(x.kind)}</span></span><span class="dim">expires in ${left(x.expires)}</span></div>`).join("")+`<div class="sub" style="margin-top:6px">Reply "approve CODE" or "deny CODE".</div>`:empty("Nothing waiting on you.");
 const b=d.budget||{};const bp=b.limit_units?Math.round(100*(b.background_units+b.reserved_units)/b.limit_units):0;
 $("bpct").textContent=b.limit_units!=null?bp+"%":"";$("bbar").style.width=Math.min(100,bp)+"%";
 $("btext").innerHTML=b.error?esc(b.error):`Today: ${(b.background_units||0).toFixed(3)} of ${(b.limit_units||0).toFixed(2)} units · ${(b.remaining_units||0).toFixed(3)} left · resets ${new Date((b.resets_at||0)*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}<br>`+
  `${b.can_start?'<span class="ok">background can start work</span>':'<span class="bad">waiting:</span> '+esc(b.why||"")} · ${b.running_calls||0}/${b.max_workers||0} calls running · chat used ${(b.interactive_units||0).toFixed(3)} units`+
  (b.estimated_rows?` · <span class="dim">${b.estimated_rows} estimated rows</span>`:"");
 const tr=(d.today||{}).by_route||{};const loc=tr.local||0;const cloud=Object.entries(tr).filter(([k])=>k!=="local").reduce((a,[,v])=>a+v,0);
 $("bmodels").innerHTML=`<div class="row"><span>Chat turns local / Claude</span><span>${loc} / ${cloud}</span></div>`+(b.by_model||[]).map(m=>`<div class="row"><span>${m.local?'<span class="tag t-local">local</span>':'<span class="tag t-answer">cloud</span>'} ${esc(m.model)} <span class="dim">${esc(m.kind)}</span></span><span class="dim">${m.calls} calls · ${m.units} u${m.measured<m.calls?" · "+(m.calls-m.measured)+" est.":""}</span></span></div>`).join("");
 $("bdecisions").innerHTML=(b.decisions||[]).slice(0,5).map(x=>`<div class="row"><span><span class="tag ${x.action==="defer"?"t-opus":"t-act"}">${esc(x.action)}</span> ${esc(x.reason)}</span><span class="dim">${ago(x.time)}</span></div>`).join("");
 const op=d.ops||{};const orow=(o,extra)=>`<div class="row"><span>${o.worker_live?'<span class="dot ok live" style="display:inline-block"></span> ':""}${esc(o.title||o.id)} <span class="dim mono">${esc(o.id)}</span></span><span class="dim">${extra}</span></div>`;
 $("ops").innerHTML=`<h3 class="dim" style="margin:0 0 4px;font-size:12px">ACTIVE</h3>`+((op.active||[]).length?op.active.map(o=>orow(o,`${o.done}/${o.total} · ${o.parked?"parked: "+esc((o.defer_reason||"").slice(0,70)):o.worker_live?"working":"queued"}`)).join(""):empty("Nothing in flight."))+
  `<h3 class="dim" style="margin:10px 0 4px;font-size:12px">BLOCKED</h3>`+((op.blocked||[]).length?op.blocked.map(o=>orow(o,`<span class="bad">${esc((o.error||o.status).slice(0,90))}</span>`)).join(""):empty("Nothing blocked."))+
  `<h3 class="dim" style="margin:10px 0 4px;font-size:12px">RECENT FAILURES</h3>`+((op.failures||[]).length?op.failures.slice(0,5).map(f=>`<div class="row"><span><span class="tag">${esc(f.kind)}</span> ${esc(f.title)}: ${esc(f.text.slice(0,90))}</span><span class="dim">${ago(f.time)}</span></div>`).join(""):empty("No failures."))+
  `<div class="sub" style="margin-top:8px">Workers: ${(op.workers||[]).length} · last recovery: ${op.last_recovery?ago(op.last_recovery):"never needed"}</div>`;
 const run=(d.jobs||[]).filter(j=>["running","queued","paused_limit"].includes(j.status));
 $("running").innerHTML=run.length?run.map(j=>`<div class="row"><span><span class="dot ok live" style="display:inline-block"></span> ${esc(j.title)}</span><span class="tag">${esc(j.status)}</span></div>`).join(""):empty("No background builds running.");
 const ag=d.agents||{};const ak=Object.keys(ag);$("agents").innerHTML=ak.length?ak.map(k=>`<div class="row"><span>${esc(k)}</span><span class="dim">${ag[k].runs} runs · ${Math.round(100*ag[k].ok/ag[k].runs)}% ok · ${ag[k].median_s??"–"}s</span></div>`).join(""):empty("No agent runs this week yet.");
 const ob=d.objectives||[];$("objectives").innerHTML=ob.length?ob.map(o=>`<div class="row"><span>${esc(o.title||o.id)}${o.project?` <span class="tag">${esc(o.project)}</span>`:""}</span><span class="dim">${o.done}/${o.total} steps · ${esc((o.status||"").replace("_"," "))}</span></div>`).join(""):empty("No objectives yet. Say \"objective: ...\" to start one.");
 const ro=d.routines||[];$("routines").innerHTML=ro.length?ro.map(r=>`<div class="row"><span>${r.enabled?"":"⏸ "}${esc(r.name)} <span class="tag">${esc(r.agent)}</span></span><span class="dim">next ${esc(r.next||"–")}${r.last_status?" · "+esc(r.last_status):""}</span></div>`).join(""):empty("No routines.");
 const l=d.learning||{};const rp=l.router_prompt||{};
 $("learning").innerHTML=`<div class="row"><span>Router prompt</span><span>v${rp.active||0} <span class="dim">(${Object.keys(rp.versions||{}).length} tried)</span></span></div>
  <div class="row"><span>Skill candidates</span><span>${l.candidates||0}</span></div>
  <div class="row"><span>Automation ideas</span><span>${(d.recipes&&d.recipes.open&&d.recipes.open.length)?d.recipes.open.map(r=>esc(r.code)+" "+esc(r.name)).join(", "):"none waiting"}</span></div>`+(l.events||[]).slice().reverse().map(e=>`<div class="row"><span class="dim">${esc(e.action)}</span><span class="dim">${ago(e.time)}</span></div>`).join("");
 const sk=d.skills||{};$("skills").innerHTML=sk.error?esc(sk.error):`<div class="big">${sk.total||0}</div><div class="chips" style="margin:6px 0 10px">${Object.entries(sk.by_tier||{}).map(([k,v])=>`<span class="tag">${esc(k)} ${v}</span>`).join("")}</div>`+
  ((sk.most_used||[]).length?(sk.most_used.map(m=>`<div class="row"><span>${esc(m[0])}</span><span class="dim">${m[1]} uses</span></div>`).join("")):empty("No skills used yet."));
 const m=d.memory||{};$("memory").innerHTML=m.error?esc(m.error):`<div class="row"><span>Vault notes</span><span>${m.notes}</span></div><div class="row"><span>Indexed chunks</span><span>${m.indexed_chunks}</span></div>
  <div class="row"><span>Conversation turns</span><span>${m.conversation_turns??"–"}</span></div><div class="row"><span>Facts learned</span><span>${Object.values(m.facts||{}).reduce((a,b)=>a+b,0)}</span></div>
  <div class="row"><span>Projects missing state</span><span class="${(m.projects_missing_state||[]).length?"bad":"ok"}">${(m.projects_missing_state||[]).length}</span></div>`;
 const mo=d.models||{};const hw=mo.hardware||{};$("models").innerHTML=mo.error?esc(mo.error):`<div class="dim" style="margin-bottom:6px">${esc((hw.gpus||[])[0]?.name||"no GPU")} · ${hw.vram_gb??"?"} GB VRAM · ${hw.ram_gb??"?"} GB RAM</div>`+
  `<div class="chips">${(mo.cloud||[]).map(c=>`<span class="tag t-answer">${esc(c)}</span>`).join("")}${(mo.local||[]).slice(0,10).map(x=>`<span class="tag ${x.where==="GPU"?"t-local":""}">${esc(x.name.split(":")[0].slice(0,22))}${x.vision?" 👁":""}</span>`).join("")}</div>`;
 const au=d.audit||[];$("audit").innerHTML=au.length?au.slice().reverse().map(x=>`<div class="row"><span><span class="tag ${x.decision==="deny"?"t-opus":"t-act"}">${esc(x.decision)}</span> ${esc(x.reason)}</span><span class="dim">${ago(x.time)}</span></div>`).join(""):empty("Nothing blocked or asked.");
 const er=(d.errors||[]).map(e=>({t:e.time,x:e.text||e.error||e.message||JSON.stringify(e).slice(0,120),c:"bad"}))
  .concat((d.notifications||[]).map(n=>({t:n.time,x:n.text,c:n.urgent?"bad":""}))).sort((a,b)=>b.t-a.t).slice(0,12);
 $("errors").innerHTML=er.length?er.map(e=>`<div class="row"><span class="${e.c}">${esc(String(e.x).slice(0,140))}</span><span class="dim">${ago(e.t)}</span></div>`).join(""):empty("All quiet.");
}
async function tick(){try{const r=await fetch("/api/state");render(await r.json())}catch(e){$("updated").textContent="Waggle dashboard not responding"}}
tick();setInterval(tick,5000);
</script></body></html>"""


if __name__ == "__main__":
    main()
