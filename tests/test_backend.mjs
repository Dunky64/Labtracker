// node --test tests/test_backend.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { createBackend, sign } from './gas_harness.mjs';

const KEY = crypto.randomBytes(32).toString('base64url') + '=';
const TOKEN = crypto.randomBytes(32).toString('base64url') + '=';
const row = (over = {}) => ({ id: crypto.randomUUID(), user: 'u1', key_id: 's',
  nonce: crypto.randomBytes(12).toString('base64url'), ct: crypto.randomBytes(40).toString('base64url'), ...over });
const fresh = () => createBackend({ LT_AUTH_KEY: KEY });

test('rejects unsigned, wrongly signed, stale and replayed requests', () => {
  const be = fresh();
  assert.equal(be.post('{}').ok, false);
  assert.equal(be.post('not json').error, 'bad_request');
  const wrongKey = crypto.randomBytes(32).toString('base64url');
  assert.equal(be.post(sign(wrongKey, { action: 'ping' })).error, 'bad_signature');
  assert.equal(be.post(sign(KEY, { action: 'ping' }, { ts: Date.now() - 10 * 60e3 })).error, 'clock');
  const body = sign(KEY, { action: 'ping' });
  assert.equal(be.post(body).ok, true);
  assert.equal(be.post(body).error, 'replayed', 'replay must fail');
  // Tampered payload with the original signature.
  const b = JSON.parse(sign(KEY, { action: 'ping' }));
  b.payload = JSON.stringify({ action: 'read' });
  assert.equal(be.post(JSON.stringify(b)).error, 'bad_signature');
});

test('a key pasted with spaces or a line break still works; a truncated key is reported', () => {
  assert.equal(createBackend({ LT_AUTH_KEY: `  ${KEY}\n` }).post(sign(KEY, { action: 'ping' })).ok, true);
  assert.equal(createBackend({ LT_AUTH_KEY: KEY.slice(10) }).post(sign(KEY, { action: 'ping' })).error, 'not_configured');
});

test('nothing works before LT_AUTH_KEY is configured', () => {
  const be = createBackend({});
  assert.equal(be.post(sign(KEY, { action: 'ping' })).error, 'not_configured');
});

test('append is validated and idempotent', () => {
  const be = fresh();
  const r = row();
  assert.equal(be.post(sign(KEY, { action: 'append', rows: [r] })).inserted, 1);
  assert.equal(be.post(sign(KEY, { action: 'append', rows: [r] })).inserted, 0);
  assert.equal(be.post(sign(KEY, { action: 'append', rows: [row({ id: 'nope' })] })).error, 'bad_row');
  assert.equal(be.post(sign(KEY, { action: 'append', rows: [row({ ct: '=cmd|x' })] })).error, 'bad_row');
  assert.equal(be.post(sign(KEY, { action: 'append', rows: [row({ key_id: 'x' })] })).error, 'bad_row');
  assert.equal(be.data.length, 2); // header + one row
});

test('dashboard needs the read token and never sees private rows', () => {
  const be = fresh();
  assert.equal(be.post(JSON.stringify({ action: 'dashboard', token: TOKEN })).error, 'unauthorized');
  be.post(sign(KEY, { action: 'set_read_token', token: TOKEN }));
  const shared = row(), priv = row({ key_id: 'p' });
  be.post(sign(KEY, { action: 'append', rows: [shared, priv] }));
  assert.equal(be.post(JSON.stringify({ action: 'dashboard', token: 'x'.repeat(TOKEN.length) })).error, 'unauthorized');
  const d = be.post(JSON.stringify({ action: 'dashboard', token: TOKEN }));
  assert.equal(d.ok, true);
  assert.deepEqual(d.rows.map((r) => r.id), [shared.id]);
  assert.ok(d.last_seen > 0);
  assert.ok(!('supersedes' in d.rows[0]));
});

test('adjust reseals the original privately and appends the replacement', () => {
  const be = fresh();
  be.post(sign(KEY, { action: 'set_read_token', token: TOKEN }));
  const orig = row();
  be.post(sign(KEY, { action: 'append', rows: [orig] }));
  const repl = row();
  const reseal = { nonce: crypto.randomBytes(12).toString('base64url'), ct: crypto.randomBytes(40).toString('base64url') };
  assert.equal(be.post(sign(KEY, { action: 'adjust', supersedes: orig.id, reseal, row: repl })).ok, true);
  const d = be.post(JSON.stringify({ action: 'dashboard', token: TOKEN }));
  assert.deepEqual(d.rows.map((r) => r.id), [repl.id]);
  const all = be.post(sign(KEY, { action: 'read' })).rows;
  const o = all.find((r) => r.id === orig.id);
  assert.equal(o.key_id, 'p');
  assert.equal(o.ct, reseal.ct);
  // Repeating is harmless. A different correction of an already-corrected entry changes nothing
  // and reports which replacement stands, so the client can tell the user to reload.
  const again = be.post(sign(KEY, { action: 'adjust', supersedes: orig.id, reseal, row: repl }));
  assert.equal(again.already, true);
  assert.equal(again.replacement, repl.id);
  const other = be.post(sign(KEY, { action: 'adjust', supersedes: orig.id, reseal, row: row() }));
  assert.equal(other.already, true);
  assert.equal(other.replacement, repl.id);
  assert.equal(be.post(JSON.stringify({ action: 'dashboard', token: TOKEN })).rows.length, 1);
  assert.equal(be.post(sign(KEY, { action: 'adjust', supersedes: crypto.randomUUID(), reseal, row: row() })).error, 'not_found');
});

test('read token must be long', () => {
  const be = fresh();
  assert.equal(be.post(sign(KEY, { action: 'set_read_token', token: 'short' })).error, 'bad_request');
});

test('labtrackerSelfTest reports the key without revealing it', () => {
  globalThis.__log = [];
  createBackend({ LT_AUTH_KEY: KEY }).run('labtrackerSelfTest');
  const out = globalThis.__log.join('\n');
  assert.match(out, /44 characters/);
  assert.match(out, /Key format OK/);
  assert.ok(!out.includes(KEY.slice(4, -4)), 'must not log the middle of the key');
  globalThis.__log = [];
  createBackend({}).run('labtrackerSelfTest');
  assert.match(globalThis.__log.join('\n'), /MISSING/);
});

test('dashboard token must be a string (no list/object tricks)', () => {
  const be = fresh();
  be.post(sign(KEY, { action: 'set_read_token', token: TOKEN }));
  assert.equal(be.post(JSON.stringify({ action: 'dashboard', token: [TOKEN] })).error, 'unauthorized');
  assert.equal(be.post(JSON.stringify({ action: 'dashboard', token: TOKEN })).ok, true);
});

test('script asks only for access to its own spreadsheet', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../backend/Code.gs', import.meta.url), 'utf8');
  assert.match(src.slice(0, 200), /@OnlyCurrentDoc/);
});
