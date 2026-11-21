import test from "node:test";
import assert from "node:assert/strict";
import { handle, runCron } from "../src/worker.js";
import {
  clock, isWaking, pickReminders, shouldAlertDown, shouldSendBack, sanitizeBeat, downText, backText, duration,
  safeEqual, MIN, parseHM,
} from "../src/logic.js";

const TZ = "America/Los_Angeles";
const at = (iso) => Date.parse(iso); // test times are UTC; 2026-11-21 is PST (UTC-8)
const NOON = at("2026-11-21T20:00:00Z"); // 12:00 in Los Angeles

function fakeKV() {
  const m = new Map();
  return {
    m,
    async get(k, type) {
      if (!m.has(k)) return null;
      return type === "json" ? JSON.parse(m.get(k)) : m.get(k);
    },
    async put(k, v) { m.set(k, String(v)); },
    async delete(k) { m.delete(k); },
  };
}

function fakeFetch(opts = {}) {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    const fail = (opts.fail || []).some((x) => String(url).includes(x));
    return { ok: !fail };
  };
  f.calls = calls;
  return f;
}

const textOf = (call) => call.body.text ?? call.body.message;

const env = () => ({
  KV: fakeKV(), BEAT_TOKEN: "t".repeat(32), TELEGRAM_BOT_TOKEN: "123:abc", TELEGRAM_CHAT_ID: "42",
  NTFY_TOPIC: "waggle-xyz", NTFY_SERVER: "https://ntfy.example", TZ_NAME: TZ, WAKE_START: "07:00", WAKE_END: "22:30",
});

const req = (method, path, body, token = "t".repeat(32)) =>
  new Request("https://w.example" + path, {
    method, headers: { authorization: `Bearer ${token}` }, body: body === undefined ? undefined : JSON.stringify(body),
  });

const beat = (e, now, reminders, f) => handle(req("POST", "/beat", { reminders }), e, now, f);
const secs = (ms) => ms / 1000;

test("clock and waking hours use the owner's time zone", () => {
  assert.equal(clock(NOON, TZ).hm, "12:00");
  assert.equal(clock(at("2026-11-21T06:59:00Z"), TZ).hm, "22:59");
  assert.ok(isWaking(at("2026-11-21T15:00:00Z"), TZ, 420, 1350)); // 07:00
  assert.ok(!isWaking(at("2026-11-21T14:59:00Z"), TZ, 420, 1350)); // 06:59
  assert.ok(isWaking(at("2026-11-22T06:29:00Z"), TZ, 420, 1350)); // 22:29
  assert.ok(!isWaking(at("2026-11-22T06:30:00Z"), TZ, 420, 1350)); // 22:30
  assert.equal(parseHM("22:30", 0), 1350);
  assert.equal(parseHM("junk", 7), 7);
});

test("auth: no token, wrong token and a short server token are refused", async () => {
  const e = env();
  assert.equal((await handle(req("GET", "/status", undefined, "nope"), e, NOON)).status, 401);
  assert.equal((await handle(new Request("https://w.example/status"), e, NOON)).status, 401);
  e.BEAT_TOKEN = "short";
  assert.equal((await handle(req("GET", "/status", undefined, "short"), e, NOON)).status, 401);
  assert.ok(safeEqual("abc", "abc") && !safeEqual("abc", "abd") && !safeEqual("abc", "abcd"));
});

test("sanitizeBeat keeps good reminders, drops junk and caps text", () => {
  const b = sanitizeBeat({ reminders: [{ id: "a1", at: 1000, text: "x".repeat(500) }, { id: "bad id!", at: 5 },
    { id: "b", at: "no" }, null] });
  assert.equal(b.reminders.length, 1);
  assert.equal(b.reminders[0].at, 1000000);
  assert.equal(b.reminders[0].text.length, 200);
  assert.equal(sanitizeBeat({}), null);
});

test("reminder delivered by the PC is never sent by the Worker (ack, fresh beat)", async () => {
  const e = env(), f = fakeFetch();
  const due = NOON + 10 * MIN;
  await beat(e, NOON, [{ id: "r1", at: secs(due), text: "charge batteries" }], f);
  assert.equal((await handle(req("POST", "/ack", { id: "r1" }), e, due + 2000, f)).status, 200);
  await beat(e, due + 4 * MIN, [], f);
  const out = await runCron(e, due + 5 * MIN, f);
  assert.deepEqual(out.sent, []);
  assert.equal(f.calls.length, 0);
});

test("an ack alone stops the Worker even when the PC then dies before its next beat", async () => {
  const e = env(), f = fakeFetch();
  const due = NOON + 10 * MIN;
  await beat(e, NOON, [{ id: "r1", at: secs(due), text: "x" }], f);
  await handle(req("POST", "/ack", { id: "r1" }), e, due + 1000, f);
  assert.deepEqual((await runCron(e, due + 20 * MIN, f)).sent, []); // beat is stale, but the ack stands
  assert.equal(f.calls.filter((c) => textOf(c).includes("⏰")).length, 0);
});

test("PC beating: nothing is sent inside the 3 min grace or before a post-due beat", async () => {
  const e = env(), f = fakeFetch();
  const due = NOON + 10 * MIN;
  await beat(e, due - 2 * MIN, [{ id: "r1", at: secs(due), text: "x" }], f);
  assert.deepEqual((await runCron(e, due + 1 * MIN, f)).sent, []); // inside the grace
  assert.deepEqual((await runCron(e, due + 3 * MIN, f)).sent, []); // beat predates the due time, PC still alive
  assert.equal(f.calls.length, 0);
});

test("PC alive but did not deliver: a post-due beat still lists it, so the Worker sends once", async () => {
  const e = env(), f = fakeFetch();
  const due = NOON + 10 * MIN;
  await beat(e, due + 3 * MIN, [{ id: "r1", at: secs(due), text: "call mom" }], f);
  assert.deepEqual((await runCron(e, due + 4 * MIN, f)).sent, ["r1"]);
  assert.equal(f.calls.length, 2); // telegram + ntfy
  assert.ok(f.calls.some((c) => c.url.includes("api.telegram.org")));
  assert.deepEqual((await runCron(e, due + 9 * MIN, f)).sent, []); // never twice
  assert.equal(f.calls.length, 2);
});

test("PC gone: the Worker sends after the grace time, and the beat reply tells the PC", async () => {
  const e = env(), f = fakeFetch();
  const due = NOON + 10 * MIN;
  await beat(e, NOON, [{ id: "r1", at: secs(due), text: "call mom" }], f);
  assert.deepEqual((await runCron(e, due + 5 * MIN, f)).sent, ["r1"]);
  assert.ok(textOf(f.calls.find((c) => c.url.includes("telegram"))).includes("call mom"));
  const res = await (await beat(e, due + 60 * MIN, [{ id: "r1", at: secs(due), text: "call mom" }], f)).json();
  assert.deepEqual(res.sent, ["r1"]);
  assert.equal((await (await handle(req("GET", "/sent?id=r1"), e, due + 61 * MIN, f)).json()).sent, true);
  assert.equal((await (await handle(req("GET", "/sent?id=zz"), e, due, f)).json()).sent, false);
});

test("a failed send is retried next run; one working channel counts as sent", async () => {
  const e = env();
  const due = NOON + 10 * MIN;
  await beat(e, NOON, [{ id: "r1", at: secs(due), text: "x" }], fakeFetch());
  assert.deepEqual((await runCron(e, due + 5 * MIN, fakeFetch({ fail: ["telegram", "ntfy"] }))).sent, []);
  assert.deepEqual((await runCron(e, due + 10 * MIN, fakeFetch({ fail: ["telegram"] }))).sent, ["r1"]);
  assert.deepEqual((await runCron(e, due + 15 * MIN, fakeFetch())).sent, []);
});

test("a reminder over 24 h late is dropped", () => {
  const snap = { t: NOON, reminders: [{ id: "old", at: NOON - 25 * 3600e3, text: "x" }] };
  assert.deepEqual(pickReminders(snap, NOON + 30 * MIN, () => false), []);
});

test("down alert once; back note only after an outage over an hour", async () => {
  const e = env(), f = fakeFetch();
  const last = NOON; // 12:00
  await beat(e, last, [], f);
  assert.equal((await runCron(e, last + 19 * MIN, f)).down, false);
  assert.equal((await runCron(e, last + 21 * MIN, f)).down, true);
  assert.equal(f.calls.length, 2);
  assert.equal(textOf(f.calls[0]), "Waggle is down since 12:00");
  await runCron(e, last + 40 * MIN, f); // no repeat
  assert.equal(f.calls.length, 2);
  await beat(e, last + 50 * MIN, [], f); // back after 50 min: no note
  assert.equal(f.calls.length, 2);
  assert.equal(await e.KV.get("down"), null);
  await runCron(e, last + 75 * MIN, f); // second outage, alert again
  assert.equal(f.calls.length, 4);
  const res = await (await beat(e, last + 150 * MIN, [], f)).json(); // 100 min outage
  assert.equal(res.back, true);
  assert.equal(f.calls.length, 6);
  assert.ok(textOf(f.calls[4]).startsWith("Waggle is back"));
});

test("down alert rules: sleeping hours, an overnight shutdown, no beat ever", () => {
  const s = { tz: TZ, startMin: 420, endMin: 1350 };
  const night = at("2026-11-22T06:15:00Z"); // 22:15 on the 21st
  assert.equal(shouldAlertDown({ now: at("2026-11-22T07:00:00Z"), lastBeat: NOON, down: null, ...s }), false); // 23:00
  assert.equal(shouldAlertDown({ now: at("2026-11-22T06:40:00Z"), lastBeat: night, down: null, ...s }), false); // 22:40
  assert.equal(shouldAlertDown({ now: at("2026-11-22T15:21:00Z"), lastBeat: night, down: null, ...s }), false); // next morning
  assert.equal(shouldAlertDown({ now: at("2026-11-22T23:00:00Z"), lastBeat: night, down: null, ...s }), false); // next afternoon
  assert.equal(shouldAlertDown({ now: NOON + 21 * MIN, lastBeat: NOON, down: null, ...s }), true);
  assert.equal(shouldAlertDown({ now: NOON + 21 * MIN, lastBeat: NOON, down: { since: NOON }, ...s }), false);
  assert.equal(shouldAlertDown({ now: NOON, lastBeat: null, down: null, ...s }), false);
  assert.equal(downText(night, at("2026-11-22T15:21:00Z"), TZ), "Waggle is down since 22:15 yesterday");
  assert.equal(shouldSendBack({ now: NOON + 61 * MIN, down: { since: NOON } }), true);
  assert.equal(shouldSendBack({ now: NOON + 60 * MIN, down: { since: NOON } }), false);
  assert.equal(duration(125 * MIN), "2 h 5 min");
  assert.ok(backText(90 * MIN).includes("1 h 30 min"));
});

test("PC switched off for the night: no down alert, no back note, a reminder still goes out", async () => {
  const e = env(), f = fakeFetch();
  const last = at("2026-11-22T06:15:00Z"); // 22:15, last beat before 22:30
  const due = at("2026-11-22T15:00:00Z"); // 07:00 next morning
  await beat(e, last, [{ id: "r1", at: secs(due), text: "wake up" }], f);
  for (let t = last + 5 * MIN; t < due; t += 5 * MIN) await runCron(e, t, f); // every cron all night
  assert.equal(f.calls.filter((c) => textOf(c).includes("down")).length, 0);
  // reminder at 07:00 goes out (PC silent), still no down alert
  const out = await runCron(e, due + 5 * MIN, f);
  assert.deepEqual(out.sent, ["r1"]);
  assert.equal(out.down, false);
  // PC starts at 09:00: no back note
  const res = await (await beat(e, due + 120 * MIN, [], f)).json();
  assert.equal(res.back, false);
  assert.equal(f.calls.filter((c) => /down|back/.test(textOf(c))).length, 0);
});

test("dying at 15:00 gives one down alert, and a back note after more than an hour", async () => {
  const e = env(), f = fakeFetch();
  const last = at("2026-11-21T23:00:00Z"); // 15:00 LA
  await beat(e, last, [], f);
  for (let t = last + 5 * MIN; t <= last + 60 * MIN; t += 5 * MIN) await runCron(e, t, f);
  const alerts = f.calls.filter((c) => textOf(c).startsWith("Waggle is down"));
  assert.equal(alerts.length, 2); // one alert, two channels
  assert.equal(textOf(alerts[0]), "Waggle is down since 15:00");
  const res = await (await beat(e, last + 90 * MIN, [], f)).json();
  assert.equal(res.back, true);
  assert.ok(f.calls.some((c) => textOf(c).startsWith("Waggle is back")));
});

test("KV writes stay far under 1000 a day: 288 beats and nothing else when idle", async () => {
  const e = env(), f = fakeFetch();
  let puts = 0;
  const put = e.KV.put.bind(e.KV);
  e.KV.put = async (...a) => { puts++; return put(...a); };
  for (let i = 0; i < 288; i++) {
    const now = NOON + i * 5 * MIN;
    await beat(e, now, [], f);
    await runCron(e, now + 30000, f);
  }
  assert.equal(puts, 288);
});

test("status and test endpoints, and unknown paths", async () => {
  const e = env(), f = fakeFetch();
  await beat(e, NOON, [{ id: "a", at: secs(NOON + 3600e3), text: "x" }], f);
  const st = await (await handle(req("GET", "/status"), e, NOON + 60000, f)).json();
  assert.equal(st.age_s, 60);
  assert.equal(st.queued, 1);
  assert.equal(st.telegram, true);
  assert.deepEqual(await (await handle(req("POST", "/test"), e, NOON, f)).json(), { telegram: true, ntfy: true });
  assert.equal((await handle(req("GET", "/nothing"), e, NOON, f)).status, 404);
  assert.equal(JSON.stringify(st).includes("123:abc"), false);
});
