// Reproductions for the November 2026 engineering review (docs/ENGINEERING_REVIEW.md).
// Each test states the correct behaviour; they were written to FAIL on the pre-review code.
// node --test tests/test_review.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { createBackend, sign } from './gas_harness.mjs';

const KEY = crypto.randomBytes(32).toString('base64url') + '=';
const TOKEN = crypto.randomBytes(32).toString('base64url') + '=';
const b64 = (n) => crypto.randomBytes(n).toString('base64url');
const row = (o = {}) => ({ id: crypto.randomUUID(), user: 'u1', key_id: 's', nonce: b64(12), ct: b64(120), ...o });
const call = (be, payload) => be.post(sign(KEY, payload));
const dash = (be, extra = {}) => be.post(JSON.stringify({ action: 'dashboard', token: TOKEN, ...extra }));

function seeded(n = 0, opts = {}) {
  const be = createBackend({ LT_AUTH_KEY: KEY }, opts);
  call(be, { action: 'set_read_token', token: TOKEN });
  for (let i = 0; i < n; i += 200) {
    call(be, { action: 'append', rows: Array.from({ length: Math.min(200, n - i) }, () => row()) });
  }
  return be;
}

function adjustOp(origId, replId = crypto.randomUUID()) {
  return { action: 'adjust', supersedes: origId, reseal: { nonce: b64(12), ct: b64(120) },
           row: row({ id: replId }) };
}

// ---------------------------------------------------------------- R1/R2 partial corrections
test('R1 an adjust interrupted between its two writes converges when retried', () => {
  const be = seeded();
  const orig = row();
  call(be, { action: 'append', rows: [orig] });
  const op = adjustOp(orig.id);
  be.failOnWrite(2);                                   // first write lands, the second never happens
  assert.equal(call(be, op).ok, false);
  const retry = call(be, op);                          // the client retries the same operation
  assert.equal(retry.ok, true, `retry failed: ${JSON.stringify(retry)}`);
  const shared = dash(be).rows.map((r) => r.id);
  assert.deepEqual(shared, [op.row.id], 'employer must see exactly the corrected entry');
});

test('R2 a second attempt with a different replacement id does not create a duplicate', () => {
  for (const crashAt of [1, 2]) {
    const be = seeded();
    const orig = row();
    call(be, { action: 'append', rows: [orig] });
    be.failOnWrite(crashAt);
    call(be, adjustOp(orig.id));
    const again = call(be, adjustOp(orig.id));          // user clicks Save again: new ids
    assert.equal(again.ok, true, `crash at write ${crashAt}: ${JSON.stringify(again)}`);
    assert.equal(dash(be).rows.length, 1, `crash at write ${crashAt}: exactly one shared entry`);
  }
});

test('R8 a correction cannot move an entry to a different user', () => {
  const be = seeded();
  const orig = row({ user: 'alice' });
  call(be, { action: 'append', rows: [orig] });
  const op = adjustOp(orig.id);
  op.row.user = 'bob';
  assert.equal(call(be, op).ok, false);
});

// ---------------------------------------------------------------- R3/R4 scalability
test('R3 duplicate detection reads only the id column', () => {
  const be = seeded(2000);
  be.resetStats();
  call(be, { action: 'append', rows: [row()] });
  assert.ok(be.stats.cellsRead <= 2001 + 10, `append read ${be.stats.cellsRead} cells for 2000 rows`);
});

test('R4 a dashboard poll with no new shared data reads no rows', () => {
  const be = seeded(500);
  const first = dash(be);
  assert.ok(first.ver, 'response carries a data version');
  be.resetStats();
  const again = dash(be, { ver: first.ver });
  assert.equal(again.unchanged, true);
  assert.equal(be.stats.cellsRead, 0);
  call(be, { action: 'append', rows: [row()] });
  const after = dash(be, { ver: first.ver });
  assert.equal(after.rows.length, 501, 'a new shared row triggers a full refresh');
});

// ---------------------------------------------------------------- R5 privacy side channel
test('R5 private activity is invisible to the dashboard (no last_seen or version change)', () => {
  const be = seeded(3);
  call(be, { action: 'ping' });
  const before = dash(be);
  const t0 = before.last_seen;
  const later = Date.now() + 1;
  while (Date.now() < later + 2);                       // let the clock move
  call(be, { action: 'append', rows: [row({ key_id: 'p' })] });   // a private note
  call(be, { action: 'read' });                                    // opening "My notes"
  const after = dash(be, { ver: before.ver });
  assert.equal(after.last_seen, t0, 'last_seen leaked private activity');
  assert.equal(after.unchanged, true, 'version changed on a private-only write');
});

// ---------------------------------------------------------------- R6 contention
test('R6 lock contention returns a retryable "busy" error, not "bad_request"', () => {
  const be = seeded(0, { lockBusy: true });
  const out = call(be, { action: 'append', rows: [row()] });
  assert.equal(out.error, 'busy');
});

// ---------------------------------------------------------------- R7 value coercion
test('R7 every written range is formatted as plain text before values are written', () => {
  const be = seeded();
  be.resetStats();
  call(be, { action: 'append', rows: [row({ nonce: '-' + b64(11), ct: '-12345' })] });
  const covered = be.stats.textFormatted.some((f) => f.length === 4 && f[1] === 2 && f[3] >= 7);
  assert.ok(covered, 'the appended range (columns B-H) must be set to text first');
  const stored = be.data.at(-1);
  assert.equal(stored[5], '-12345');
});

// ---------------------------------------------------------------- measurements (not assertions)
test('M1 work per request as the sheet grows (printed)', () => {
  const lines = [];
  for (const n of [1000, 5000, 10000]) {
    const be = seeded(n);
    be.resetStats(); call(be, { action: 'append', rows: [row()] });
    const append = be.stats.cellsRead;
    be.resetStats(); const d = dash(be);
    const full = be.stats.cellsRead; const bytes = JSON.stringify(d).length;
    be.resetStats(); dash(be, { ver: d.ver });
    lines.push(`${String(n).padStart(6)} rows | append reads ${String(append).padStart(6)} cells | dashboard full ${String(full).padStart(6)} cells, ${(bytes / 1024).toFixed(0).padStart(5)} KB | unchanged poll ${be.stats.cellsRead} cells`);
  }
  console.log('\n' + lines.join('\n'));
});
