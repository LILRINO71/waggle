// Pure decisions for the Waggle dead-man Worker. No I/O here, so node can test every rule.
export const MIN = 60000;
export const GRACE_MS = 3 * MIN; // a reminder waits this long past its time before the Worker sends it
export const BEAT_AFTER_DUE_MS = 150000; // a beat this long after the due time that still lists it = not delivered
export const STALE_MS = 7 * MIN; // the PC counts as gone when its last beat is older than this (beats are every 5 min)
export const DOWN_AFTER_MS = 20 * MIN; // missing this long (in waking hours) = send the "down" alert
export const BACK_MIN_OUTAGE_MS = 60 * MIN; // the "back" note goes out only after an outage longer than this
export const MAX_LATE_MS = 24 * 3600 * 1000; // a reminder later than this is dropped, not sent
export const MAX_REMINDERS = 200;
export const TEXT_MAX = 200;

export function safeEqual(a, b) {
  a = String(a ?? "");
  b = String(b ?? "");
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

export function parseHM(text, fallback) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(text ?? "").trim());
  if (!m) return fallback;
  const h = Number(m[1]), mi = Number(m[2]);
  return h < 24 && mi < 60 ? h * 60 + mi : fallback;
}

// Local clock in a time zone: minutes since midnight, "HH:MM", and a day number for comparing days.
export function clock(ms, tz) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }).formatToParts(new Date(ms));
  const get = (t) => Number(parts.find((p) => p.type === t).value);
  const h = get("hour") % 24, mi = get("minute");
  return {
    minutes: h * 60 + mi,
    hm: `${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}`,
    day: Math.floor(Date.UTC(get("year"), get("month") - 1, get("day")) / 86400000),
  };
}

export function isWaking(ms, tz, startMin, endMin) {
  const m = clock(ms, tz).minutes;
  return m >= startMin && m < endMin;
}

// Beat as the PC sends it (at = epoch seconds) -> {reminders: [{id, at (ms), text}]}, or null when it is not usable.
export function sanitizeBeat(body) {
  if (!body || typeof body !== "object" || !Array.isArray(body.reminders)) return null;
  const out = [];
  for (const r of body.reminders.slice(0, MAX_REMINDERS)) {
    if (!r || typeof r !== "object") continue;
    const id = String(r.id ?? "");
    const at = Number(r.at);
    if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(id) || !Number.isFinite(at) || at <= 0) continue;
    out.push({ id, at: Math.round(at * 1000), text: String(r.text ?? "").slice(0, TEXT_MAX) });
  }
  return { reminders: out };
}

// Which reminders the Worker must send now. `done(id)` is true for one already delivered (ack) or sent.
export function pickReminders(snap, now, done) {
  if (!snap || !Array.isArray(snap.reminders)) return [];
  const alive = now - snap.t <= STALE_MS;
  const out = [];
  for (const r of snap.reminders) {
    if (now < r.at + GRACE_MS) continue; // not due yet, or inside the grace time the PC gets to deliver
    if (now > r.at + MAX_LATE_MS) continue; // too old to be useful
    // PC still beating: only act when a beat taken after the due time still lists it as undelivered.
    if (alive && snap.t < r.at + BEAT_AFTER_DUE_MS) continue;
    if (done(r.id)) continue;
    out.push(r);
  }
  return out.sort((a, b) => a.at - b.at);
}

// Should the "down" alert go out now? lastBeat: ms of the last beat; down: the stored alert, if any.
// Only when the last beat itself fell in waking hours, it is waking hours now, both are on the same local day
// (so the night is not inside the outage) and more than 20 min passed. A PC switched off for the night never alerts.
export function shouldAlertDown({ now, lastBeat, down, tz, startMin, endMin }) {
  if (!lastBeat || down) return false;
  if (!isWaking(now, tz, startMin, endMin) || !isWaking(lastBeat, tz, startMin, endMin)) return false;
  if (clock(now, tz).day !== clock(lastBeat, tz).day) return false;
  return now - lastBeat > DOWN_AFTER_MS;
}

export function shouldSendBack({ now, down }) {
  return !!down && now - down.since > BACK_MIN_OUTAGE_MS;
}

export function duration(ms) {
  const total = Math.max(0, Math.round(ms / MIN));
  const h = Math.floor(total / 60), m = total % 60;
  return h ? `${h} h${m ? ` ${m} min` : ""}` : `${m} min`;
}

export function reminderText(r, now) {
  const late = now - r.at > 10 * MIN;
  return `⏰ ${r.text || "Reminder"}${late ? "\n(sent by the backup service: Waggle is not running on the PC)" : ""}`;
}

export function downText(lastBeat, now, tz) {
  const a = clock(lastBeat, tz), b = clock(now, tz);
  const when = a.day === b.day ? a.hm : `${a.hm} ${b.day - a.day === 1 ? "yesterday" : "on an earlier day"}`;
  return `Waggle is down since ${when}`;
}

export function backText(outageMs) {
  return `Waggle is back. It was down for ${duration(outageMs)}.`;
}
