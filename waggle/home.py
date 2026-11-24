"""Waggle Home: the page the owner uses Waggle from, served by the dashboard at http://127.0.0.1:8897/.

  GET  /            this page (the older engine-room page moved to /ops)
  GET  /api/home    one JSON snapshot: what is on Waggle's mind, what it told the owner today, what it is working on,
                    the last night shift, reminders, usage
  POST /api/say     {"text"}  one typed turn for the owner's chat through the core's voice line (reply as JSON)
  POST /api/talk    audio/*   one spoken turn; the answer comes back as WAV with the spoken words in a header
  POST /api/night   {"text"}  "accept 1", "redo 2", "reject 3 because ..." for the last night report
  POST /api/taste   {"line"}  one line in the owner's words for the note the thinking pass reads (think.TASTE_NOTE)
  POST /api/think   {}        run one thinking pass now (in the background; it shows up in the snapshot)

Every POST acts as the owner, so three things are checked first (check_post): the Host header is this machine
(a web site whose name points at 127.0.0.1 is refused), an Origin header, when the browser sends one, is this page,
and the request carries the token that only this page was given (a new one each time the process starts). A page on
another site can do none of the three. Turns still go through the core, the engine and the permission hook like any
other owner turn: this page adds no new power, only a nicer way in.
"""
import datetime as dt
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from .config import Settings

TOKEN = secrets.token_urlsafe(24)
MAX_BODY = 2 * 1024 * 1024
_thinking = {"running": False, "started": 0.0, "last": None}


# ---- who may post --------------------------------------------------------------------------------------------

def local_host(host, port):
    return (host or "").strip().lower() in (f"127.0.0.1:{port}", f"localhost:{port}")


def check_post(headers, port):
    """'' when the request may act, else why not."""
    if not local_host(headers.get("Host"), port):
        return "not this machine"
    origin = (headers.get("Origin") or "").strip().lower()
    if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return "another page"
    if not hmac.compare_digest(str(headers.get("X-Waggle-Token") or ""), TOKEN):
        return "no page token"
    return ""


# ---- the snapshot --------------------------------------------------------------------------------------------

def _safe(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one broken panel never blanks the page
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"} if default is None else default


def _usage(now):
    from . import quota
    out = {}
    st = quota.status()
    out["five_hour_pct"] = st.get("window_pct")
    seven = quota.seven_day_reading(now)
    if seven:
        out["week_pct"] = round(100 * seven["utilization"])
        out["week_resets"] = seven["resets_at"]
    return out


def _thinking_state(s, now):
    from . import think
    last = think.passes(1)
    nxt = None
    try:
        from waggle.core import timers
        runs = [t["next_run"] for t in (timers.get(f"outreach.think.{i}") for i in range(4)) if t and t.get("enabled")]
        nxt = min(runs) if runs else None
    except Exception:  # noqa: BLE001
        pass
    thoughts = [{"time": t["time"], "topic": t["topic"], "message": t["message"], "draft": t["draft"],
                 "action": t["action"], "wanted": t["model_action"], "why": t["why"], "guard": t["guard"]}
                for t in reversed(think.recent_thoughts(now, days=3))][:12]
    return {"enabled": bool(getattr(s, "think_enabled", True)), "times": [t for _, t in think.times(s)], "next": nxt,
            "running": _thinking["running"],
            "last": ({"time": last[0]["time"], "ok": bool(last[0]["ok"]), "note": last[0]["note"]} if last else None),
            "thoughts": thoughts,
            "questions": [{"time": q["time"], "question": q["question"]} for q in think.recent_questions(now, 7)]}


def _told(now):
    from . import notify
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = [r for r in notify.rows(since=start) if r["status"] in ("sent", "queued", "sending", "held")]
    return [{"time": r["created"], "text": " ".join((r["text"] or "").split())[:260], "origin": r["origin"],
             "status": r["status"], "kind": r["kind"]} for r in rows[-25:]][::-1]


def _night():
    from . import nightshift
    rep = nightshift.latest_report()
    items = []
    for n, tid in enumerate(json.loads((rep or {}).get("items") or "[]"), 1):
        t = nightshift.task(tid) or {}
        items.append({"n": n, "title": t.get("title") or tid, "status": t.get("status"), "pr": t.get("pr_url"),
                      "answer": t.get("answer")})
    queued = len(nightshift.tasks("queued"))
    return {"sent": (rep or {}).get("sent"), "items": items, "queued": queued}


def _work():
    from . import worklist
    rows, bad = worklist.items()
    return {"items": [{k: r.get(k) for k in ("source", "title", "state", "detail")} for r in rows[:14]],
            "unreadable": bad}


def _reminders(now):
    from . import followups
    return [{"due": it.get("due"), "what": str(it.get("what") or "")[:140], "kind": it.get("kind")}
            for it in followups.items(status="open") if (it.get("due") or 0) > now - 3600][:8]


def state(now=None):
    """The snapshot for GET /api/home."""
    from . import dashboard, think
    s = Settings.load()
    now = now or time.time()
    return {"time": now,
            "health": dashboard._cached("health", 10, dashboard.health),
            "usage": dashboard._cached("home_usage", 30, lambda: _usage(now)),
            "thinking": _safe(lambda: _thinking_state(s, now), None),
            "told": _safe(lambda: _told(now), []),
            "work": _safe(_work, {"items": [], "unreadable": ["work list"]}),
            "night": _safe(_night, {"items": [], "queued": 0}),
            "reminders": _safe(lambda: _reminders(now), []),
            "taste": _safe(lambda: [l.lstrip("- ").strip() for l in think.taste(s).splitlines()
                                    if l.strip().startswith("-")], [])}


# ---- acting as the owner -------------------------------------------------------------------------------------

def _core(path, body, ctype, timeout=90):
    """POST to the core's voice line. (status, headers dict, body bytes); never raises."""
    from waggle.core import settings as core_settings
    from waggle.core import voiceline
    req = urllib.request.Request(f"http://127.0.0.1:{core_settings.port()}{path}", data=body, method="POST",
                                 headers={"Content-Type": ctype, "Authorization": f"Bearer {voiceline.token()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the local core
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # noqa: BLE001 - the core is off or restarting
        return 503, {}, json.dumps({"error": f"Waggle's core did not answer ({type(e).__name__})"}).encode()


def say(text, post=None):
    """One typed turn. {"ok", "text", "spoken", "status"} or {"ok": False, "error"}."""
    text = " ".join(str(text or "").split())
    if not text:
        return {"ok": False, "error": "nothing to send"}
    code, _, body = (post or _core)("/voice?reply=json&client=dash", json.dumps({"text": text[:4000]}).encode("utf-8"),
                                    "application/json")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if code != 200:
        return {"ok": False, "error": data.get("error") or f"the core answered {code}"}
    return {"ok": True, "text": data.get("text") or "", "spoken": data.get("spoken") or "",
            "status": data.get("status") or "done"}


def talk(audio, ctype, post=None):
    """One spoken turn. (status, content type, body, spoken words)."""
    import urllib.parse
    if not audio or len(audio) > MAX_BODY or not str(ctype or "").lower().startswith("audio/"):
        return 400, "application/json", json.dumps({"error": "send up to 2 MB of audio"}).encode(), ""
    code, headers, body = (post or _core)("/voice?format=wav&client=dash", audio, ctype.split(";")[0])
    low = {k.lower(): v for k, v in headers.items()}
    spoken = urllib.parse.unquote(low.get("x-waggle-spoken", ""))
    return code, low.get("content-type", "application/json"), body, spoken


def night(text):
    from . import nightshift
    reply = nightshift.answer(" ".join(str(text or "").split())[:300])
    return {"ok": reply is not None, "text": reply or "That is not an answer to the last night report."}


def teach(line):
    from . import think
    return {"ok": think.add_taste(line)}


def think_now(runner=None):
    """Start one thinking pass in the background (one at a time). The snapshot shows it when it is done."""
    if _thinking["running"]:
        return {"ok": False, "error": "already thinking"}
    _thinking.update(running=True, started=time.time())

    def go():
        try:
            from . import think
            _thinking["last"] = (runner or think.run)(slot="asked")
        except Exception as e:  # noqa: BLE001
            _thinking["last"] = {"ok": False, "note": f"{type(e).__name__}: {e}"[:200]}
        finally:
            _thinking["running"] = False
    threading.Thread(target=go, daemon=True, name="waggle-think-now").start()
    return {"ok": True}


def page():
    return PAGE.replace("__TOKEN__", TOKEN)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waggle</title>
<style>
:root{--bg:#0b0d11;--panel:#141821;--panel2:#1a2030;--line:#252b3a;--text:#eceef4;--dim:#8b93a7;--honey:#f5b301;
--honey2:#ffd866;--ok:#3ddc84;--bad:#ff6b6b;--cyan:#4fd6f2;--violet:#a78bfa;--r:16px}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:radial-gradient(900px 520px at 50% -8%,#3a2d0a 0,transparent 62%),var(--bg);color:var(--text);
font:14px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif;overflow:hidden}
.wrap{display:grid;grid-template-columns:330px minmax(380px,1fr) 350px;gap:16px;height:100vh;padding:16px}
.col{display:flex;flex-direction:column;gap:14px;min-height:0;overflow:auto;scrollbar-width:thin}
.card{background:linear-gradient(180deg,var(--panel),#11141b);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px}
.card h2{font-size:11.5px;letter-spacing:1px;text-transform:uppercase;color:var(--dim);margin:0 0 10px;display:flex;
justify-content:space-between;align-items:center;gap:8px}
.mid{display:flex;flex-direction:column;min-height:0;align-items:center}
.top{display:flex;align-items:center;gap:10px;width:100%;padding:2px 4px 0}
.logo{font-weight:800;font-size:19px;letter-spacing:.3px}.logo span{color:var(--honey)}
.pills{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.pill{display:flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;background:var(--panel);
border:1px solid var(--line);font-size:12px;color:var(--dim);text-decoration:none}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}.dot.ok{background:var(--ok);box-shadow:0 0 7px var(--ok)}
.dot.bad{background:var(--bad);box-shadow:0 0 7px var(--bad)}
#orb{width:300px;height:300px;cursor:pointer;margin-top:4px;flex:none;touch-action:none;user-select:none}
#mood{font-size:15px;font-weight:600;margin-top:-6px}#hint{color:var(--dim);font-size:12.5px;margin-bottom:10px}
#log{flex:1;min-height:0;width:100%;max-width:760px;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:4px 6px;
scrollbar-width:thin}
.b{max-width:86%;padding:9px 13px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word}
.b.me{align-self:flex-end;background:#3a2f0b;border:1px solid #5a470d}
.b.w{align-self:flex-start;background:var(--panel2);border:1px solid var(--line)}
.b.sys{align-self:center;color:var(--dim);font-size:12.5px;background:none;padding:2px}
form{display:flex;gap:8px;width:100%;max-width:760px;padding:10px 6px 2px}
input[type=text],textarea{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;color:var(--text);
padding:11px 14px;font:inherit;outline:none;resize:none}input[type=text]:focus,textarea:focus{border-color:var(--honey)}
button{background:var(--honey);color:#1a1300;border:0;border-radius:12px;padding:0 16px;font:inherit;font-weight:700;
cursor:pointer;min-height:38px}button.ghost{background:var(--panel2);color:var(--text);border:1px solid var(--line);
font-weight:500;padding:4px 10px;min-height:0;border-radius:9px;font-size:12.5px}button:disabled{opacity:.5;cursor:default}
.col{overflow-x:hidden}.item{padding:9px 0;border-bottom:1px solid #1e2431;overflow-wrap:anywhere}.item:last-child{border:0}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;background:#232a3a;color:var(--dim);white-space:nowrap}
.tag.text,.tag.voice{background:#3a2f0b;color:var(--honey)}.tag.evening{background:#2a1f45;color:var(--violet)}
.tag.ok{background:#1d3324;color:var(--ok)}.tag.bad{background:#3a1c1c;color:var(--bad)}
.dim{color:var(--dim)}.small{font-size:12.5px}.draft{margin-top:5px;padding:7px 10px;border-left:2px solid var(--honey);
background:#1b1a12;border-radius:0 8px 8px 0;font-size:13px}
.bar{height:8px;border-radius:99px;background:#232a3a;overflow:hidden;margin:4px 0 8px}
.bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--ok),var(--honey) 70%,var(--bad));transition:width .6s}
.empty{color:var(--dim);font-style:italic}.acts{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
a{color:var(--cyan)}@media(max-width:1150px){body{overflow:auto}.wrap{grid-template-columns:1fr;height:auto}.col{overflow:visible}.mid{order:-1}
#log{min-height:260px}}
</style></head><body><div class="wrap">
<div class="col">
 <section class="card"><h2>On my mind <button class="ghost" id="thinkbtn">Think now</button></h2>
  <div class="small dim" id="thinkmeta"></div><div id="thoughts"></div></section>
 <section class="card"><h2>Teach me what you want to hear</h2>
  <div id="taste"></div>
  <form id="tasteform" style="padding:8px 0 0"><input type="text" id="tasteline" placeholder="e.g. tell me when a friend has a big day"><button>Add</button></form></section>
 <section class="card"><h2>Reminders</h2><div id="reminders"></div></section>
</div>
<div class="mid">
 <div class="top"><div class="logo">Wag<span>gle</span></div><div class="pills" id="health"></div></div>
 <canvas id="orb" width="600" height="600" title="Hold to talk"></canvas>
 <div id="mood">Hello.</div><div id="hint">Hold the orb or the Space key and talk. Or type below.</div>
 <div id="log"></div>
 <form id="say"><input type="text" id="text" placeholder="Ask Waggle anything, or tell it to do something" autocomplete="off"><button id="send">Send</button></form>
</div>
<div class="col">
 <section class="card"><h2>What I told you today</h2><div id="told"></div></section>
 <section class="card"><h2>Working on</h2><div id="work"></div></section>
 <section class="card"><h2>Night shift</h2><div id="night"></div></section>
 <section class="card"><h2>Claude allowance</h2><div id="usage"></div></section>
</div></div>
<script>
const TOKEN="__TOKEN__";const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const clock=t=>t?new Date(t*1000).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"}):"";
const day=t=>{const d=new Date(t*1000),n=new Date();return d.toDateString()===n.toDateString()?clock(t):d.toLocaleDateString([], {weekday:"short"})+" "+clock(t)};
const post=(path,body,type)=>fetch(path,{method:"POST",headers:{"X-Waggle-Token":TOKEN,"Content-Type":type||"application/json"},body:type?body:JSON.stringify(body)});
let mood="idle",level=0,busy=false;
function setMood(m,label){mood=m;$("mood").textContent=label||{idle:"Here when you need me.",listening:"Listening…",thinking:"Thinking…",speaking:"Speaking…"}[m]}
function bubble(kind,text){const d=document.createElement("div");d.className="b "+kind;d.textContent=text;$("log").appendChild(d);$("log").scrollTop=1e9;return d}

/* the orb: a honeycomb cell that breathes, listens, thinks and speaks */
const cv=$("orb"),cx=cv.getContext("2d");let t0=performance.now();
function hex(r,rot){cx.beginPath();for(let i=0;i<6;i++){const a=rot+i*Math.PI/3;cx[i?"lineTo":"moveTo"](300+r*Math.cos(a),300+r*Math.sin(a))}cx.closePath()}
function draw(now){const t=(now-t0)/1000;cx.clearRect(0,0,600,600);
 const breathe=Math.sin(t*1.6)*.5+.5;let amp=mood==="listening"?level:mood==="speaking"?level:mood==="thinking"?.35+.2*Math.sin(t*6):.08*breathe;
 const glow=cx.createRadialGradient(300,300,40,300,300,290);glow.addColorStop(0,`rgba(245,179,1,${.28+amp*.5})`);glow.addColorStop(1,"rgba(245,179,1,0)");
 cx.fillStyle=glow;cx.fillRect(0,0,600,600);
 for(let i=0;i<4;i++){const r=150+i*26+amp*46*(1+i*.35)+Math.sin(t*2+i)*4;cx.lineWidth=2.2-i*.4;
  cx.strokeStyle=`rgba(255,216,102,${(.5-i*.11)*(mood==="idle"?.55:1)})`;hex(r,(mood==="thinking"?t*(i%2?-1:1)*1.4:Math.PI/6)+i*.02);cx.stroke()}
 const g=cx.createLinearGradient(190,190,410,410);g.addColorStop(0,"#ffe58a");g.addColorStop(.5,"#f5b301");g.addColorStop(1,"#b97e00");
 cx.fillStyle=g;hex(118+amp*22+breathe*3,Math.PI/6);cx.fill();
 cx.fillStyle="rgba(26,19,0,.9)";const e=mood==="listening"?16:mood==="thinking"?6:12,b=Math.sin(t*.9)>.985?2:e;
 const m=mood==="speaking"?10+level*26:mood==="listening"?6:4;
 cx.beginPath();cx.ellipse(262,282,10,b,0,0,7);cx.ellipse(338,282,10,b,0,0,7);cx.fill();
 cx.beginPath();cx.roundRect(300-26,330,52,m,m/2);cx.fill();
 level*=.9;requestAnimationFrame(draw)}
requestAnimationFrame(draw);

/* typing */
$("say").onsubmit=async ev=>{ev.preventDefault();const text=$("text").value.trim();if(!text||busy)return;$("text").value="";
 bubble("me",text);busy=true;setMood("thinking");
 try{const r=await (await post("/api/say",{text})).json();bubble(r.ok?"w":"sys",r.ok?(r.text||r.spoken):("Could not send: "+r.error))}
 catch(e){bubble("sys","Waggle did not answer. Is the core running?")}
 busy=false;setMood("idle");refresh()};

/* talking: hold the orb or Space */
let rec=null,chunks=[],stream=null,actx=null,an=null,buf=null,raf=0;
function meter(){if(!an)return;an.getByteTimeDomainData(buf);let s=0;for(const v of buf){const x=(v-128)/128;s+=x*x}
 level=Math.max(level,Math.min(1,Math.sqrt(s/buf.length)*3.2));raf=requestAnimationFrame(meter)}
async function startTalk(){if(busy||rec)return;try{stream=stream||await navigator.mediaDevices.getUserMedia({audio:true})}
 catch(e){bubble("sys","The browser has no microphone permission for this page.");return}
 actx=actx||new AudioContext();const src=actx.createMediaStreamSource(stream);an=actx.createAnalyser();an.fftSize=512;buf=new Uint8Array(an.fftSize);src.connect(an);
 chunks=[];rec=new MediaRecorder(stream);rec.ondataavailable=e=>chunks.push(e.data);rec.onstop=sendTalk;rec.start();setMood("listening");meter()}
function stopTalk(){if(rec&&rec.state!=="inactive"){rec.stop()}cancelAnimationFrame(raf);an=null}
async function sendTalk(){const type=rec.mimeType||"audio/webm";const blob=new Blob(chunks,{type});rec=null;
 if(blob.size<3000){setMood("idle");return}busy=true;setMood("thinking");const mine=bubble("me","🎙 …");
 try{const r=await post("/api/talk",blob,type.split(";")[0]);const spoken=decodeURIComponent(r.headers.get("X-Waggle-Spoken")||"");
  if(!r.ok){const j=await r.json().catch(()=>({}));mine.textContent="🎙 (not sent)";bubble("sys",j.error||"Waggle could not take that.")}
  else{mine.textContent="🎙 (spoken)";bubble("w",spoken||"(answer sent to your chat)");const a=new Audio(URL.createObjectURL(await r.blob()));
   const s2=actx.createMediaElementSource(a);const an2=actx.createAnalyser();an2.fftSize=512;const b2=new Uint8Array(512);s2.connect(an2);an2.connect(actx.destination);
   setMood("speaking");const tick=()=>{if(mood!=="speaking")return;an2.getByteTimeDomainData(b2);let s=0;for(const v of b2){const x=(v-128)/128;s+=x*x}
    level=Math.max(level,Math.min(1,Math.sqrt(s/512)*3));requestAnimationFrame(tick)};tick();
   a.onended=()=>setMood("idle");await a.play().catch(()=>setMood("idle"));busy=false;refresh();return}}
 catch(e){bubble("sys","Waggle did not answer. Is the core running?")}
 busy=false;setMood("idle");refresh()}
cv.addEventListener("pointerdown",e=>{cv.setPointerCapture(e.pointerId);startTalk()});cv.addEventListener("pointerup",stopTalk);cv.addEventListener("pointercancel",stopTalk);
addEventListener("keydown",e=>{if(e.code==="Space"&&!e.repeat&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();startTalk()}});
addEventListener("keyup",e=>{if(e.code==="Space"&&rec){e.preventDefault();stopTalk()}});

/* side panels */
$("thinkbtn").onclick=async()=>{$("thinkbtn").disabled=true;await post("/api/think",{});refresh()};
$("tasteform").onsubmit=async ev=>{ev.preventDefault();const line=$("tasteline").value.trim();if(!line)return;$("tasteline").value="";await post("/api/taste",{line});refresh()};
async function nightAnswer(text){const r=await (await post("/api/night",{text})).json();bubble("sys",r.text);refresh()}
function render(d){
 const h=d.health||{};$("health").innerHTML=[["Gateway",h.gateway],["Local AI",h.ollama],["Voice",h.kokoro],["Claude",h.claude_cli]]
  .map(([n,v])=>`<span class="pill"><span class="dot ${v?"ok":"bad"}"></span>${n}</span>`).join("")+`<a class="pill" href="/ops">engine room</a>`;
 const th=d.thinking||{};$("thinkbtn").disabled=!!th.running;$("thinkbtn").textContent=th.running?"Thinking…":"Think now";
 $("thinkmeta").innerHTML=th.error?esc(th.error):(th.enabled===false?"Thinking passes are switched off.":
  `Passes at ${(th.times||[]).join(", ")}`+(th.last?` · last ${day(th.last.time)}${th.last.ok?"":` <span class="tag bad">${esc(th.last.note||"failed")}</span>`}`:" · none yet")+(th.next?` · next ${day(th.next)}`:""));
 const ts=th.thoughts||[];$("thoughts").innerHTML=(ts.length?ts.map(t=>`<div class="item"><span class="tag ${esc(t.action)}">${esc({text:"told you",voice:"said it",evening:"for tonight's note",none:"kept to myself"}[t.action]||t.action)}</span>
  <span class="dim small">${day(t.time)}</span><div>${esc(t.message)}</div>${t.draft?`<div class="draft">${esc(t.draft)}</div>`:""}
  <div class="dim small">${esc(t.why)}${t.guard?" · "+esc(t.guard):""}</div></div>`).join(""):`<div class="empty">Nothing yet. Press "Think now" to see what I notice.</div>`)
  +(th.questions||[]).map(q=>`<div class="item"><span class="tag">I asked</span> <span class="dim small">${day(q.time)}</span><div>${esc(q.question)}</div></div>`).join("");
 $("taste").innerHTML=(d.taste||[]).length?d.taste.map(l=>`<div class="item small">${esc(l)}</div>`).join(""):`<div class="empty small">Nothing yet. Tell me, in your own words, what is worth a message.</div>`;
 $("reminders").innerHTML=(d.reminders||[]).length?d.reminders.map(r=>`<div class="item small"><span class="dim">${day(r.due)}</span> ${esc(r.what)}</div>`).join(""):`<div class="empty small">None open.</div>`;
 $("told").innerHTML=(d.told||[]).length?d.told.map(r=>`<div class="item"><span class="dim small">${clock(r.time)} · ${esc(r.origin)}${r.status!=="sent"?" · "+esc(r.status):""}</span><div class="small">${esc(r.text)}</div></div>`).join(""):`<div class="empty">Nothing today.</div>`;
 const w=(d.work||{}).items||[];$("work").innerHTML=w.length?w.map(r=>`<div class="item small"><span class="tag">${esc(r.source)}</span> ${esc(r.title)}<div class="dim">${esc(r.state)}${r.detail?" · "+esc(r.detail):""}</div></div>`).join(""):`<div class="empty">Nothing running or queued.</div>`;
 const n=d.night||{};$("night").innerHTML=((n.items||[]).length?n.items.map(i=>`<div class="item small"><b>${i.n}.</b> ${esc(i.title)} <span class="tag ${i.status==="delivered"?"ok":"bad"}">${esc(i.status)}</span>
  ${i.pr?`<div><a href="${esc(i.pr)}" target="_blank" rel="noopener">draft pull request</a></div>`:""}
  ${i.answer?`<div class="dim">you said: ${esc(i.answer)}</div>`:`<div class="acts"><button class="ghost" onclick="nightAnswer('accept ${i.n}')">Accept</button><button class="ghost" onclick="nightAnswer('redo ${i.n}')">Redo</button><button class="ghost" onclick="nightAnswer('reject ${i.n}')">Reject</button></div>`}</div>`).join(""):`<div class="empty">No night report yet.</div>`)
  +`<div class="dim small" style="margin-top:6px">${n.queued||0} task(s) queued for the next night.</div>`;
 const u=d.usage||{};const bar=(l,p)=>p==null?"":(p=Math.round(p),`<div class="small">${l} <b>${p}%</b></div><div class="bar"><i style="width:${Math.min(100,p)}%"></i></div>`);
 $("usage").innerHTML=(bar("5-hour window",u.five_hour_pct)+bar("This week",u.week_pct)+(u.week_resets?`<div class="dim small">Week resets ${day(u.week_resets)}</div>`:""))||`<div class="empty small">No reading yet.</div>`;
}
async function refresh(){try{render(await (await fetch("/api/home")).json())}catch(e){}}
refresh();setInterval(refresh,5000);
</script></body></html>"""
