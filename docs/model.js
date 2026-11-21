// Pure logic: turn the decrypted event log into shifts, away periods and daily totals.
// No DOM access here, so it can be tested in Node.

export const UK = 'Europe/London';
export const STALE_MS = 15 * 60 * 1000;   // app hasn't reported for this long -> "not reporting"

const dayFmt = new Intl.DateTimeFormat('en-CA', { timeZone: UK, year: 'numeric', month: '2-digit', day: '2-digit' });
const partsFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: UK, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

export function dayKey(ms) { return dayFmt.format(new Date(ms)); }

function ukOffset(ms) {
  const p = Object.fromEntries(partsFmt.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return asUtc - Math.floor(ms / 1000) * 1000;
}

export function dayStart(key) {
  const [y, m, d] = key.split('-').map(Number);
  const guess = Date.UTC(y, m - 1, d);
  return guess - ukOffset(guess);
}

export function addDays(key, n) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

export function weekStart(key) {            // Monday
  const [y, m, d] = key.split('-').map(Number);
  const dow = (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
  return addDays(key, -dow);
}

export function monthDays(key) {
  const first = key.slice(0, 8) + '01';
  const out = [];
  for (let k = first; k.slice(0, 7) === first.slice(0, 7); k = addDays(k, 1)) out.push(k);
  return out;
}

export function sortEvents(events) {
  return events.slice().sort((a, b) => (a.at - b.at) || (a.seq - b.seq));
}

/**
 * Sessions = shifts. Each: {start, end, open, endKind, startAdjusted, endAdjusted, idles:[{start,end,open}]}
 * endKind: 'out' | 'auto' | 'prompt' | 'open' | 'stale' | 'missing'
 */
export function buildSessions(events, { now, lastSeen = 0 }) {
  const sessions = [];
  let cur = null;
  const closeIdle = (t) => {
    const last = cur && cur.idles[cur.idles.length - 1];
    if (last && last.end == null) last.end = Math.max(t, last.start);
  };
  for (const e of sortEvents(events)) {
    switch (e.type) {
      case 'clock_in':
        if (cur) { closeIdle(e.at); cur.end = e.at; cur.endKind = 'missing'; }
        cur = { start: e.at, end: null, open: false, endKind: null, startAdjusted: !!e.adjusted,
                endAdjusted: false, idles: [] };
        sessions.push(cur);
        break;
      case 'clock_out':
      case 'auto_clock_out':
        if (!cur) break;
        closeIdle(e.at);
        cur.end = Math.max(e.at, cur.start);
        cur.endKind = e.type === 'auto_clock_out' ? 'auto' : (e.via === 'prompt' ? 'prompt' : 'out');
        cur.endAdjusted = !!e.adjusted;
        cur = null;
        break;
      case 'idle_start': {
        if (!cur) break;
        const last = cur.idles[cur.idles.length - 1];
        if (!last || last.end != null) cur.idles.push({ start: Math.max(e.at, cur.start), end: null });
        break;
      }
      case 'idle_end':
        if (cur) closeIdle(e.at);
        break;
      default:
        break;
    }
  }
  if (cur) {
    const lastEvent = Math.max(cur.start, ...cur.idles.map((i) => i.start));
    const stale = now - lastSeen > STALE_MS;
    cur.open = true;
    cur.endKind = stale ? 'stale' : 'open';
    cur.end = stale ? Math.max(lastEvent, Math.min(lastSeen, now)) : now;
    const last = cur.idles[cur.idles.length - 1];
    if (last && last.end == null) { last.end = cur.end; last.open = true; }
  }
  for (const s of sessions) s.idles = s.idles.filter((i) => i.end > i.start);
  return sessions;
}

export function currentStatus(sessions, now, lastSeen) {
  const last = sessions[sessions.length - 1];
  if (!last || !last.open) return { state: 'out', since: last ? last.end : null, endKind: last && last.endKind };
  const idle = last.idles[last.idles.length - 1];
  const stale = last.endKind === 'stale';
  if (idle && idle.open) return { state: 'away', since: idle.start, sessionStart: last.start, stale };
  return { state: 'in', since: last.start, stale, lastSeen };
}

const clip = (a, b, lo, hi) => Math.max(0, Math.min(b, hi) - Math.max(a, lo));

export function daySummary(sessions, key) {
  const lo = dayStart(key);
  const hi = dayStart(addDays(key, 1));
  let worked = 0, away = 0, first = null, last = null, adjusted = false;
  const parts = [];
  for (const s of sessions) {
    if (s.end <= lo || s.start >= hi) continue;
    const w = clip(s.start, s.end, lo, hi);
    if (w <= 0) continue;
    worked += w;
    const st = Math.max(s.start, lo), en = Math.min(s.end, hi);
    first = first == null ? st : Math.min(first, st);
    last = last == null ? en : Math.max(last, en);
    adjusted = adjusted || s.startAdjusted || s.endAdjusted;
    const idles = [];
    for (const i of s.idles) {
      const a = clip(i.start, i.end, lo, hi);
      if (a > 0) { away += a; idles.push({ start: Math.max(i.start, lo), end: Math.min(i.end, hi), open: !!i.open }); }
    }
    parts.push({ session: s, start: st, end: en, idles,
                 startsHere: s.start >= lo, endsHere: s.end <= hi });
  }
  const open = parts.some((p) => p.session.open && p.endsHere);
  return { key, worked, away, active: worked - away, first, last, adjusted, open, parts };
}

export function fmtDuration(ms) {
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function hours(ms) { return Math.round((ms / 3600000) * 100) / 100; }
