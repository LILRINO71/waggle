// Waggle dead-man switch. The PC posts a beat every 5 minutes (with the reminders due in the next 48 h).
// A cron every 5 minutes (a) sends a reminder the PC did not deliver and (b) says once when Waggle is down.
// Everything needs the shared token. Secrets (BEAT_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NTFY_TOPIC) are
// Worker secrets; nothing secret is logged. KV writes: one per beat (288 a day) plus one or two per reminder.
import {
  safeEqual, parseHM, sanitizeBeat, pickReminders, shouldAlertDown, shouldSendBack,
  reminderText, downText, backText,
} from "./logic.js";

const DAY_S = 86400;
const MAX_BODY = 64 * 1024;
const MAX_PER_RUN = 6; // reminders sent per cron run (the free plan allows 50 subrequests per run)
const ID_OK = /^[A-Za-z0-9_.:-]{1,64}$/;

const sec = (env, k) => String(env[k] ?? "").trim();
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

export async function sendTelegram(env, text, f = fetch) {
  const token = sec(env, "TELEGRAM_BOT_TOKEN"), chat = sec(env, "TELEGRAM_CHAT_ID");
  if (!token || !chat) return false;
  try {
    const res = await f(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: chat, text: text.slice(0, 3500) }),
    });
    return res.ok;
  } catch {
    return false; // never put the error text in a log: it can contain the token URL
  }
}

export async function sendNtfy(env, text, { title = "Waggle", priority = 4, tags = ["bee"] } = {}, f = fetch) {
  const topic = sec(env, "NTFY_TOPIC");
  if (!topic) return false;
  try {
    const res = await f(sec(env, "NTFY_SERVER") || "https://ntfy.sh", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ topic, title, message: text.slice(0, 3500), priority, tags }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// Both channels; true when at least one took the message.
async function sendBoth(env, text, opts, f) {
  const [a, b] = await Promise.all([sendTelegram(env, text, f), sendNtfy(env, text, opts, f)]);
  return a || b;
}

function settings(env) {
  return {
    tz: sec(env, "TZ_NAME") || "America/Los_Angeles",
    startMin: parseHM(sec(env, "WAKE_START"), 7 * 60),
    endMin: parseHM(sec(env, "WAKE_END"), 22 * 60 + 30),
  };
}

const isDone = async (env, id) => (await env.KV.get(`ack:${id}`)) !== null || (await env.KV.get(`sent:${id}`)) !== null;

export async function runCron(env, now = Date.now(), f = fetch) {
  const snap = await env.KV.get("snap", "json");
  if (!snap) return { skipped: "no beat yet" };
  const out = { sent: [], down: false };
  // 1. reminders the PC did not deliver (a few per run: the free plan allows 50 subrequests a run)
  let tried = 0;
  for (const r of pickReminders(snap, now, () => false).slice(0, 15)) {
    if (tried >= MAX_PER_RUN) break;
    if (await isDone(env, r.id)) continue;
    // claim first: at most once, even if this run dies half-way
    await env.KV.put(`sent:${r.id}`, String(now), { expirationTtl: 3 * DAY_S });
    const ok = await sendBoth(env, reminderText(r, now), { title: "Reminder", priority: 4, tags: ["alarm_clock"] }, f);
    tried++;
    if (ok) out.sent.push(r.id);
    else await env.KV.delete(`sent:${r.id}`); // nothing went out: try again next run
  }
  // 2. the "down" alert, once per outage
  const s = settings(env);
  const down = await env.KV.get("down", "json");
  if (shouldAlertDown({ now, lastBeat: snap.t, down, ...s })) {
    const ok = await sendBoth(env, downText(snap.t, now, s.tz), { title: "Waggle is down", priority: 5, tags: ["warning"] }, f);
    if (ok) {
      await env.KV.put("down", JSON.stringify({ since: snap.t, alertedAt: now }));
      out.down = true;
    }
  }
  return out;
}

async function readBody(req) {
  const text = await req.text();
  if (text.length > MAX_BODY) return undefined;
  try { return text ? JSON.parse(text) : {}; } catch { return undefined; }
}

export async function handle(req, env, now = Date.now(), f = fetch) {
  const token = sec(env, "BEAT_TOKEN");
  const given = /^Bearer (.+)$/.exec(req.headers.get("authorization") || "")?.[1] ?? "";
  if (token.length < 16 || !safeEqual(given.trim(), token)) return json({ error: "unauthorized" }, 401);
  const url = new URL(req.url);

  if (req.method === "POST" && url.pathname === "/beat") {
    const beat = sanitizeBeat(await readBody(req));
    if (!beat) return json({ error: "bad beat" }, 400);
    const down = await env.KV.get("down", "json");
    await env.KV.put("snap", JSON.stringify({ t: now, reminders: beat.reminders }));
    let back = false;
    if (down) {
      await env.KV.delete("down");
      if (shouldSendBack({ now, down })) {
        await sendBoth(env, backText(now - down.since), { title: "Waggle is back", priority: 3, tags: ["white_check_mark"] }, f);
        back = true;
      }
    }
    // ids the PC still thinks are open that the Worker already sent: the PC marks them done
    const sent = [];
    for (const r of beat.reminders.filter((x) => x.at <= now).slice(0, 25)) {
      if ((await env.KV.get(`sent:${r.id}`)) !== null) sent.push(r.id);
    }
    return json({ ok: true, t: now, sent, back });
  }

  if (req.method === "POST" && url.pathname === "/ack") {
    const body = await readBody(req);
    const id = String(body?.id ?? "");
    if (!ID_OK.test(id)) return json({ error: "bad id" }, 400);
    await env.KV.put(`ack:${id}`, String(now), { expirationTtl: 3 * DAY_S });
    return json({ ok: true });
  }

  if (req.method === "GET" && url.pathname === "/sent") {
    const id = url.searchParams.get("id") || "";
    if (!ID_OK.test(id)) return json({ error: "bad id" }, 400);
    return json({ sent: (await env.KV.get(`sent:${id}`)) !== null });
  }

  if (req.method === "GET" && url.pathname === "/status") {
    const snap = await env.KV.get("snap", "json");
    const down = await env.KV.get("down", "json");
    return json({
      now, last_beat: snap?.t ?? null, age_s: snap ? Math.round((now - snap.t) / 1000) : null,
      queued: (snap?.reminders || []).length, next_due: snap?.reminders?.[0]?.at ?? null, down: down ?? null,
      telegram: !!sec(env, "TELEGRAM_BOT_TOKEN") && !!sec(env, "TELEGRAM_CHAT_ID"), ntfy: !!sec(env, "NTFY_TOPIC"),
    });
  }

  if (req.method === "POST" && url.pathname === "/test") {
    const text = "Waggle's backup service is set up. If Waggle goes down you will hear it here.";
    const [telegram, ntfy] = await Promise.all([sendTelegram(env, text, f), sendNtfy(env, text, { title: "Waggle test" }, f)]);
    return json({ telegram, ntfy });
  }

  return json({ error: "not found" }, 404);
}

export default {
  fetch: (req, env) => handle(req, env),
  scheduled: (event, env, ctx) => { ctx.waitUntil(runCron(env)); },
};
