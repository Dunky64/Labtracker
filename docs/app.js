import { importKey, openRow } from './crypto.js';
import {
  UK, addDays, buildSessions, currentStatus, dayKey, dayStart, daySummary, fmtDuration, hours,
  monthDays, weekStart,
} from './model.js';

// Refuse to run inside someone else's frame (clickjacking).
if (window.top !== window.self) { document.body.textContent = ''; throw new Error('framed'); }

const $ = (id) => document.getElementById(id);
const SVGNS = 'http://www.w3.org/2000/svg';
const POLL_MS = 60 * 1000;
const tFmt = new Intl.DateTimeFormat('en-GB', { timeZone: UK, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const dFmt = new Intl.DateTimeFormat('en-GB', { timeZone: UK, weekday: 'short', day: 'numeric', month: 'short' });
const longFmt = new Intl.DateTimeFormat('en-GB', { timeZone: UK, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const monFmt = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', month: 'long', year: 'numeric' });
const t = (ms) => tFmt.format(new Date(ms));
const keyLabel = (k, fmt = dFmt) => fmt.format(new Date(dayStart(k) + 12 * 3600e3));

const state = { cfg: null, key: null, events: [], lastSeen: 0, serverSkew: 0, fetchedAt: 0, bad: 0,
                view: 'day', anchor: dayKey(Date.now()) };

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v; else n.setAttribute(k, v);
  }
  for (const c of kids) if (c != null) n.append(c);
  return n;
}
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function notice(title, body) {
  $('main').hidden = true;
  const n = $('notice');
  n.hidden = false;
  n.replaceChildren(el('h2', { text: title }), el('p', { text: body }));
}

// --------------------------------------------------------------- config --
function readConfig() {
  const p = new URLSearchParams(location.hash.slice(1));
  const cfg = { url: p.get('u') || '', key: p.get('k') || '', token: p.get('t') || '',
                id: p.get('id') || '', name: p.get('n') || '' };
  if (!/^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]{20,200}\/exec$/.test(cfg.url) ||
      !cfg.key || !cfg.token) return null;
  return cfg;
}

// ---------------------------------------------------------------- fetch --
async function load() {
  const res = await fetch(state.cfg.url, {
    method: 'POST',
    // `ver` = the data version we already hold: if nothing shared changed, the backend answers
    // "unchanged" without reading the sheet, which keeps one-minute polling cheap as data grows.
    body: JSON.stringify({ action: 'dashboard', token: state.cfg.token, ver: state.ver || undefined }),
    credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || 'error');
  state.serverSkew = (data.server_now || Date.now()) - Date.now();
  state.fetchedAt = Date.now();
  if (data.unchanged && state.ver === data.ver) {
    state.lastSeen = Math.max(data.last_seen || 0, state.maxRowTs || 0);
    return;
  }
  const events = [];
  let bad = 0, maxRowTs = 0;
  const seen = new Set();
  for (const row of data.rows) {
    if (state.cfg.id && row.user !== state.cfg.id) continue;
    if (seen.has(row.id)) continue;   // a row duplicated inside the Sheet must not count twice
    seen.add(row.id);
    if (row.server_ts > maxRowTs) maxRowTs = row.server_ts;
    try {
      const ev = await openRow(state.key, row);
      if (typeof ev.at === 'number' && typeof ev.type === 'string') events.push(ev);
    } catch { bad += 1; }
  }
  state.events = events;
  state.bad = bad;
  state.ver = data.ver;
  state.maxRowTs = maxRowTs;
  state.lastSeen = Math.max(data.last_seen || 0, maxRowTs);
}

const now = () => Date.now() + state.serverSkew;

// --------------------------------------------------------------- render --
function render() {
  const n = now();
  const sessions = buildSessions(state.events, { now: n, lastSeen: state.lastSeen });
  renderStatus(sessions, n);
  renderControls();
  const keys = state.view === 'day' ? [state.anchor]
    : state.view === 'week' ? Array.from({ length: 7 }, (_, i) => addDays(weekStart(state.anchor), i))
      : monthDays(state.anchor);
  const days = keys.map((k) => daySummary(sessions, k));
  renderTiles(days);
  if (state.view === 'day') { renderDayChart(days[0], n); renderDayList(days[0]); }
  else { renderBars(days, n); renderTable(days); }
  $('updated').textContent = `Live · updated ${t(state.fetchedAt)}` + (state.bad ? ` · ${state.bad} unreadable entries` : '');
}

function renderStatus(sessions, n) {
  const s = currentStatus(sessions, n, state.lastSeen);
  const box = $('status');
  const kids = [];
  if (s.state === 'in') {
    kids.push(el('span', { class: 'dot in', 'aria-hidden': 'true' }), el('span', { class: 'state', text: 'Working' }),
      el('span', { class: 'sub', text: `Clocked in at ${t(s.since)} · ${fmtDuration(n - s.since)} so far` }));
  } else if (s.state === 'away') {
    kids.push(el('span', { class: 'dot away', 'aria-hidden': 'true' }), el('span', { class: 'state', text: 'Away from desk' }),
      el('span', { class: 'sub', text: `Since ${t(s.since)} (${fmtDuration(n - s.since)}) · still clocked in` }));
  } else {
    const how = { auto: ' (automatic, no response)', prompt: '' }[s.endKind] || '';
    kids.push(el('span', { class: 'dot out', 'aria-hidden': 'true' }), el('span', { class: 'state', text: 'Clocked out' }),
      el('span', { class: 'sub', text: s.since ? `Last shift ended ${dFmt.format(new Date(s.since))} at ${t(s.since)}${how}` : 'No shifts recorded yet' }));
  }
  if (s.stale) {
    kids.push(el('span', { class: 'warnline',
      text: `⚠ The Labtracker app hasn't reported since ${t(state.lastSeen)}. The laptop may be asleep, off or offline.` }));
  }
  box.replaceChildren(...kids);
}

function renderControls() {
  document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === state.view)));
  const k = state.anchor;
  let label;
  if (state.view === 'day') label = keyLabel(k, longFmt);
  else if (state.view === 'week') { const ws = weekStart(k); label = `${keyLabel(ws)} – ${keyLabel(addDays(ws, 6))}`; }
  else label = monFmt.format(new Date(k.slice(0, 8) + '15T12:00:00Z'));
  $('range-label').textContent = label;
  const today = dayKey(now());
  const end = state.view === 'day' ? k : state.view === 'week' ? addDays(weekStart(k), 6) : monthDays(k).at(-1);
  $('next').disabled = end >= today;
}

function tile(k, v) { return el('div', { class: 'tile' }, el('div', { class: 'k', text: k }), el('div', { class: 'v', text: v })); }

function renderTiles(days) {
  const sum = (f) => days.reduce((a, d) => a + d[f], 0);
  const worked = sum('worked'), away = sum('away');
  const daysWorked = days.filter((d) => d.worked > 0).length;
  const tiles = [tile('Hours worked', fmtDuration(worked)), tile('At desk', fmtDuration(worked - away)),
                 tile('Away (counted)', fmtDuration(away))];
  if (state.view !== 'day') tiles.push(tile('Days worked', String(daysWorked)));
  $('tiles').replaceChildren(...tiles);
}

// ---- tooltip
const tip = $('tip');
function showTip(evt, html) {
  tip.replaceChildren(...html);
  tip.hidden = false;
  const pad = 14, r = tip.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { tip.hidden = true; }
function hover(node, lines, target) {
  const on = (e) => { showTip(e, lines()); target?.classList.add('hot'); };
  node.addEventListener('pointermove', on);
  node.addEventListener('pointerleave', () => { hideTip(); target?.classList.remove('hot'); });
}
const line = (b, rest) => el('div', {}, el('b', { text: b }), rest ? ` ${rest}` : '');

// ---- day timeline
function renderDayChart(day, n) {
  $('legend').hidden = false;
  const W = chartWidth(), H = 96, L = 8, R = 8, top = 22, barH = 34;
  const lo = dayStart(day.key), hi = dayStart(addDays(day.key, 1));
  const HOUR = 3600e3;
  let a = day.first != null ? Math.floor((day.first - lo) / HOUR) - 1 : 8;
  let b = day.last != null ? Math.ceil((day.last - lo) / HOUR) + 1 : 18;
  a = Math.max(0, Math.min(a, 8)); b = Math.min(24, Math.max(b, a + 8, 18));
  const x0 = lo + a * HOUR, x1 = lo + b * HOUR;
  const X = (ms) => L + ((ms - x0) / (x1 - x0)) * (W - L - R);
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `Timeline for ${keyLabel(day.key, longFmt)}` });
  const ax = svg('g', { class: 'axis' });
  const step = Math.ceil((b - a) / Math.max(3, Math.floor(W / 80)));
  for (let h = a; h <= b; h += step) {
    const x = X(lo + h * HOUR);
    ax.append(svg('line', { x1: x, x2: x, y1: top - 4, y2: top + barH + 4 }));
    const tx = svg('text', { x, y: top + barH + 20, 'text-anchor': h === a ? 'start' : h === b ? 'end' : 'middle' });
    tx.textContent = `${String(h % 24).padStart(2, '0')}:00`;
    ax.append(tx);
  }
  s.append(ax);
  s.append(svg('rect', { class: 'track', x: L, y: top, width: W - L - R, height: barH, rx: 4 }));

  for (const p of day.parts) {
    // Split the shift into at-desk and away pieces.
    const pieces = [];
    let cur = p.start;
    for (const i of p.idles) {
      if (i.start > cur) pieces.push({ kind: 'active', start: cur, end: i.start });
      pieces.push({ kind: 'away', start: i.start, end: i.end, open: i.open });
      cur = i.end;
    }
    if (p.end > cur) pieces.push({ kind: 'active', start: cur, end: p.end });
    pieces.forEach((pc, idx) => {
      const gapL = idx > 0 ? 1 : 0, gapR = idx < pieces.length - 1 ? 1 : 0;
      const x = X(pc.start) + gapL, w = Math.max(1, X(pc.end) - X(pc.start) - gapL - gapR);
      const r = svg('rect', { class: `seg bar-${pc.kind}`, x, y: top, width: w, height: barH, rx: 4 });
      s.append(r);
      const hit = svg('rect', { class: 'hit', x: x - 3, y: top - 8, width: w + 6, height: barH + 16 });
      hover(hit, () => [line(pc.kind === 'active' ? 'At desk' : 'Away', `${t(pc.start)}–${pc.open || (p.session.open && pc.end >= n - 60e3) ? 'now' : t(pc.end)}`),
        el('div', { class: 'muted', text: fmtDuration(pc.end - pc.start) })], r);
      s.append(hit);
    });
    const mk = (ms, label) => {
      const g = svg('g', { class: 'marker' });
      const x = X(ms);
      g.append(svg('line', { x1: x, x2: x, y1: top - 6, y2: top + barH + 6 }));
      const tx = svg('text', { x, y: top - 10, 'text-anchor': 'middle', class: 'vlabel' });
      tx.textContent = label;
      g.append(tx);
      s.append(g);
    };
    if (p.startsHere) mk(p.start, `${t(p.start)}${p.session.startAdjusted ? '*' : ''}`);
    if (p.endsHere && !p.session.open) mk(p.end, `${t(p.end)}${p.session.endAdjusted ? '*' : ''}`);
  }
  if (n > x0 && n < x1) {
    const g = svg('g', { class: 'now' });
    g.append(svg('line', { x1: X(n), x2: X(n), y1: top - 2, y2: top + barH + 2 }));
    s.append(g);
  }
  if (!day.parts.length) {
    const tx = svg('text', { x: W / 2, y: top + barH / 2 + 4, 'text-anchor': 'middle', class: 'vlabel' });
    tx.textContent = 'No work recorded on this day';
    s.append(tx);
  }
  $('chart').replaceChildren(s);
}

function renderDayList(day) {
  const items = [];
  const add = (ms, text, pill) => items.push({ ms, text, pill });
  for (const p of day.parts) {
    const sess = p.session;
    if (p.startsHere) add(p.start, 'Clocked in', sess.startAdjusted && 'adjusted');
    else add(p.start, 'Shift continued from previous day');
    for (const i of p.idles) {
      add(i.start, `Away ${i.open ? 'since' : 'until'} ${i.open ? t(i.start) : t(i.end)} · ${fmtDuration(i.end - i.start)}${i.open ? ' so far' : ''}`);
    }
    if (p.endsHere && !sess.open) {
      const label = { auto: 'Clocked out automatically (no response to the away prompt)',
                      prompt: 'Clocked out (at time last active)', missing: 'Shift ended (no clock-out recorded)' }[sess.endKind] || 'Clocked out';
      add(p.end, label, sess.endAdjusted && 'adjusted');
    } else if (sess.open) {
      add(p.end, sess.endKind === 'stale' ? 'App stopped reporting' : 'Still clocked in');
    }
  }
  items.sort((a, b) => a.ms - b.ms);
  const list = el('ul', { class: 'list' }, ...items.map((i) => el('li', {},
    el('span', { class: 't', text: t(i.ms) }),
    el('span', {}, i.text, i.pill ? el('span', { class: 'pill', text: i.pill, title: 'The employee corrected this time' }) : null))));
  const kids = [el('div', { class: 'detail-head' }, el('h2', { text: 'Timeline' })),
    items.length ? list : el('p', { class: 'muted', text: 'Nothing recorded.' })];
  if (day.parts.some((p) => p.session.startAdjusted || p.session.endAdjusted)) {
    kids.push(el('p', { class: 'muted', text: '* adjusted: corrected by the employee after the fact.' }));
  }
  $('detail').replaceChildren(...kids);
}

// ---- week / month bars
function renderBars(days, n) {
  $('legend').hidden = false;
  const W = chartWidth(), H = 260, L = 34, R = 8, top = 16, bottom = 30;
  const maxH = Math.max(8, Math.ceil(Math.max(...days.map((d) => d.worked)) / 3600e3));
  const Y = (ms) => top + (H - top - bottom) * (1 - ms / (maxH * 3600e3));
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Hours worked per day' });
  const grid = svg('g', { class: 'grid axis' });
  const stepH = maxH > 12 ? 4 : 2;
  for (let h = 0; h <= maxH; h += stepH) {
    const y = Y(h * 3600e3);
    grid.append(svg('line', { x1: L, x2: W - R, y1: y, y2: y }));
    const tx = svg('text', { x: L - 6, y: y + 4, 'text-anchor': 'end' });
    tx.textContent = `${h}h`;
    grid.append(tx);
  }
  s.append(grid);
  const slot = (W - L - R) / days.length;
  const bw = Math.min(46, slot * 0.62);
  const today = dayKey(n);
  days.forEach((d, i) => {
    const cx = L + slot * i + slot / 2, x = cx - bw / 2;
    const yBase = Y(0), yActive = Y(d.active), yTop = Y(d.worked);
    const g = svg('g');
    if (d.active > 0) g.append(barPath(x, yActive, bw, yBase - yActive, d.away > 0 ? 0 : 4, 'seg bar-active'));
    if (d.away > 0) {
      const hgt = Math.max(1, yActive - yTop - 2);
      g.append(barPath(x, yTop, bw, hgt, 4, 'seg bar-away'));
    }
    s.append(g);
    const lab = svg('text', { x: cx, y: H - 10, 'text-anchor': 'middle', class: 'axis' });
    const dd = new Date(dayStart(d.key) + 12 * 3600e3);
    lab.textContent = days.length <= 7 ? (W < 600 ? dFmt.format(dd).slice(0, 2) : dFmt.format(dd).replace(/ \w+$/, ''))
      : ((i % (W < 600 ? 5 : 2) === 0) ? String(dd.getUTCDate()) : '');
    lab.setAttribute('class', 'vlabel');
    if (d.key === today) lab.setAttribute('font-weight', '700');
    s.append(lab);
    if (days.length <= 7 && W >= 600 && d.worked > 0) {
      const v = svg('text', { x: cx, y: yTop - 6, 'text-anchor': 'middle', class: 'vlabel' });
      v.textContent = fmtDuration(d.worked);
      s.append(v);
    }
    const hit = svg('rect', { class: 'hit', x: cx - slot / 2, y: top, width: slot, height: H - top - bottom });
    hover(hit, () => [line(keyLabel(d.key)),
      el('div', { text: `Worked ${fmtDuration(d.worked)}` }),
      el('div', { class: 'muted', text: `At desk ${fmtDuration(d.active)} · Away ${fmtDuration(d.away)}` }),
      d.first ? el('div', { class: 'muted', text: `${t(d.first)}–${t(d.last)}${d.adjusted ? ' · adjusted' : ''}` }) : null], g);
    s.append(hit);
  });
  $('chart').replaceChildren(s);
}

function chartWidth() { return Math.max(300, Math.round($('chart').clientWidth || 900)); }

function barPath(x, y, w, h, r, cls) {
  r = Math.min(r, w / 2, h);
  const d = r > 0
    ? `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`
    : `M${x},${y + h} V${y} H${x + w} V${y + h} Z`;
  return svg('path', { d, class: cls });
}

function renderTable(days) {
  const head = el('tr', {}, ...['Day', 'First in', 'Last out', 'Worked', 'At desk', 'Away', ''].map((h, i) =>
    el('th', { class: i >= 3 && i <= 5 ? 'num' : '', text: h })));
  const rows = days.map((d) => el('tr', {},
    el('td', { text: keyLabel(d.key) }),
    el('td', { text: d.first ? t(d.first) : '–' }),
    el('td', { text: d.open ? 'still in' : d.last ? t(d.last) : '–' }),
    el('td', { class: 'num', text: d.worked ? fmtDuration(d.worked) : '–' }),
    el('td', { class: 'num', text: d.worked ? fmtDuration(d.active) : '–' }),
    el('td', { class: 'num', text: d.away ? fmtDuration(d.away) : '–' }),
    el('td', {}, d.adjusted ? el('span', { class: 'pill', text: 'adjusted' }) : '')));
  const sum = (f) => days.reduce((a, d) => a + d[f], 0);
  const foot = el('tr', {}, el('td', { text: 'Total' }), el('td'), el('td'),
    el('td', { class: 'num', text: fmtDuration(sum('worked')) }),
    el('td', { class: 'num', text: fmtDuration(sum('active')) }),
    el('td', { class: 'num', text: fmtDuration(sum('away')) }), el('td'));
  const dl = el('button', { class: 'dl', text: 'Download CSV' });
  dl.addEventListener('click', () => downloadCsv(days));
  $('detail').replaceChildren(
    el('div', { class: 'detail-head' }, el('h2', { text: 'Daily breakdown' }), dl),
    el('div', { class: 'scroll' }, el('table', {}, el('thead', {}, head), el('tbody', {}, ...rows), el('tfoot', {}, foot))));
}

function downloadCsv(days) {
  const q = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const lines = [['date', 'first_in', 'last_out', 'hours_worked', 'hours_at_desk', 'hours_away', 'adjusted'].join(',')];
  for (const d of days) {
    lines.push([d.key, d.first ? t(d.first) : '', d.open ? 'still in' : d.last ? t(d.last) : '', hours(d.worked), hours(d.active),
      hours(d.away), d.adjusted ? 'yes' : ''].map(q).join(','));
  }
  const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/csv' });
  const a = el('a', { href: URL.createObjectURL(blob), download: `labtracker-${days[0].key}-to-${days.at(-1).key}.csv` });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// ---------------------------------------------------------------- wiring --
function step(dir) {
  const k = state.anchor;
  if (state.view === 'day') state.anchor = addDays(k, dir);
  else if (state.view === 'week') state.anchor = addDays(weekStart(k), 7 * dir);
  else {
    const [y, m] = k.split('-').map(Number);
    state.anchor = new Date(Date.UTC(y, m - 1 + dir, 1)).toISOString().slice(0, 10);
  }
  render();
}

async function refresh() {
  try {
    await load();
    $('main').hidden = false;
    $('notice').hidden = true;
    render();
  } catch (e) {
    if (state.fetchedAt) { $('updated').textContent = `Couldn't refresh (last update ${t(state.fetchedAt)})`; return; }
    if (String(e.message) === 'unauthorized') {
      notice('This link no longer works', 'The employee may have replaced it with a new one. Ask them for the current link.');
    } else {
      notice('Couldn\'t load the timeline', 'Check your internet connection and reload the page.');
    }
  }
}

async function start() {
  state.cfg = readConfig();
  if (!state.cfg) {
    notice('This page needs a private link',
      'Labtracker dashboards open from a private link that the employee sends you. The link contains the key ' +
      'that decrypts their timeline. Ask them for it (in the app: "Copy employer dashboard link").');
    return;
  }
  $('who').textContent = state.cfg.name;
  // Lets the employer notice if a link suddenly points at a different backend (a spoofed link).
  $('source').textContent = `Data source ID ends …${state.cfg.url.split('/').at(-2).slice(-6)}`;
  if (state.cfg.name) document.title = `${state.cfg.name} · Labtracker`;
  try { state.key = await importKey(state.cfg.key); } catch {
    notice('This link is damaged', 'Part of the link is missing. Ask the employee to send it again.');
    return;
  }
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => { state.view = b.dataset.view; render(); }));
  $('prev').addEventListener('click', () => step(-1));
  $('next').addEventListener('click', () => step(1));
  $('today').addEventListener('click', () => { state.anchor = dayKey(now()); render(); });
  await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('hashchange', () => location.reload());
}

let resizeT;
window.addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(() => state.cfg && state.fetchedAt && render(), 150); });

start();
