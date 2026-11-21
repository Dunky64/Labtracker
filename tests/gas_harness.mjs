// Runs backend/Code.gs locally with in-memory fakes of the Apps Script services.
// Usage as a server:  node tests/gas_harness.mjs <port> <authKeyB64>
//
// What this harness does NOT reproduce (see docs/LIVE_TESTING.md): real Sheets value coercion
// and number formats, real LockService contention, CacheService eviction, PropertiesService
// quotas, execution time limits, the 302 redirect to googleusercontent.com, and real latency.
// JavaScript here is single-threaded, so true concurrent executions cannot happen in-process.
import fs from 'node:fs';
import http from 'node:http';
import vm from 'node:vm';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const CODE = fs.readFileSync(path.join(here, '..', 'backend', 'Code.gs'), 'utf8');

const toSigned = (buf) => Array.from(buf, (b) => (b > 127 ? b - 256 : b));
const toBuf = (arr) => Buffer.from(arr.map((b) => (b < 0 ? b + 256 : b)));

export class SimulatedCrash extends Error {}

/**
 * opts.lockBusy        -> the script lock can never be acquired (simulates contention / timeout)
 * be.failOnWrite(n)    -> the n-th setValues call from now throws (simulates the execution dying
 *                         part-way, e.g. hitting the 6-minute limit), leaving earlier writes in place
 * be.stats             -> cells read/written and calls, to measure how much work each request does
 */
export function createBackend(props = {}, opts = {}) {
  const data = [];               // rows (including header) of the "events" sheet
  let sheetExists = false;
  const stats = { cellsRead: 0, cellsWritten: 0, getValues: 0, setValues: 0, textFormatted: [] };
  let failCountdown = 0;
  const range = (r, c, nr, nc) => ({
    setNumberFormat: (f) => { if (f === '@') stats.textFormatted.push([r, c, nr, nc]); return range(r, c, nr, nc); },
    getValues: () => {
      stats.getValues += 1;
      stats.cellsRead += nr * nc;
      return data.slice(r - 1, r - 1 + nr).map((row) => row.slice(c - 1, c - 1 + nc));
    },
    setValues: (vals) => {
      if (failCountdown > 0 && --failCountdown === 0) throw new SimulatedCrash('execution killed');
      stats.setValues += 1;
      stats.cellsWritten += vals.length * vals[0].length;
      vals.forEach((v, i) => {
        while (data.length < r + i) data.push(new Array(8).fill(''));
        const row = data[r - 1 + i].slice();
        v.forEach((x, j) => { row[c - 1 + j] = x; });
        data[r - 1 + i] = row;
      });
      return range(r, c, nr, nc);
    },
  });
  const sheet = {
    getLastRow: () => data.length,
    getMaxRows: () => Math.max(1000, data.length),
    setFrozenRows: () => {},
    getRange: (r, c, nr, nc) => (typeof r === 'string'
      ? { setNumberFormat: (f) => { if (f === '@') stats.textFormatted.push([r]); } }
      : range(r, c, nr ?? 1, nc ?? 1)),
  };
  const cache = new Map();
  const ctx = {
    Date, Math, JSON, String, Number, Array, Object, Logger: { log: (m) => (globalThis.__log = (globalThis.__log || []).concat(m)) },
    SpreadsheetApp: { flush: () => {}, getActiveSpreadsheet: () => ({
      getSheetByName: () => (sheetExists ? sheet : null),
      insertSheet: () => { sheetExists = true; return sheet; },
    }) },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (k) => (k in props ? props[k] : null),
      getProperties: () => ({ ...props }),
      setProperty: (k, v) => { props[k] = String(v); },
    }) },
    CacheService: { getScriptCache: () => ({
      get: (k) => cache.get(k) || null, put: (k, v) => cache.set(k, v),
    }) },
    LockService: { getScriptLock: () => ({
      waitLock: () => { if (opts.lockBusy) throw new Error('Lock timeout'); },
      tryLock: () => !opts.lockBusy,
      releaseLock: () => {},
    }) },
    Utilities: {
      base64DecodeWebSafe: (s) => toSigned(Buffer.from(s, 'base64url')),
      newBlob: (s) => ({ getBytes: () => toSigned(Buffer.from(s, 'utf8')) }),
      computeHmacSha256Signature: (v, k) =>
        toSigned(crypto.createHmac('sha256', toBuf(k)).update(toBuf(v)).digest()),
    },
    ContentService: {
      MimeType: { JSON: 'json' },
      createTextOutput: (s) => ({ setMimeType() { return this; }, getContent: () => s }),
    },
  };
  vm.createContext(ctx);
  vm.runInContext(CODE, ctx);
  return {
    props, data, stats, opts,
    post: (body) => JSON.parse(ctx.doPost({ postData: { contents: body } }).getContent()),
    run: (fn) => ctx[fn](),
    failOnWrite: (n) => { failCountdown = n; },
    resetStats: () => Object.assign(stats, { cellsRead: 0, cellsWritten: 0, getValues: 0, setValues: 0, textFormatted: [] }),
  };
}

export function sign(authKeyB64, payloadObj, overrides = {}) {
  const payload = JSON.stringify(payloadObj);
  const ts = overrides.ts ?? Date.now();
  const nonce = overrides.nonce ?? crypto.randomBytes(18).toString('base64url');
  const msg = `v1\n${ts}\n${nonce}\n${payload}`;
  const sig = crypto.createHmac('sha256', Buffer.from(authKeyB64, 'base64url')).update(msg).digest('base64url');
  return JSON.stringify({ v: 1, ts, nonce, payload, sig });
}

// ---------------------------------------------------------------- server --
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.argv[2] || 8765);
  const be = createBackend({ LT_AUTH_KEY: process.argv[3] });
  let crashNext = 0;
  http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const headers = { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' };
      if (req.method === 'GET' && req.url === '/__sheet') {
        res.writeHead(200, headers); res.end(JSON.stringify(be.data)); return;
      }
      if (req.method === 'GET' && req.url.startsWith('/__drop_next_response')) {
        crashNext = 1; res.writeHead(200, headers); res.end('{}'); return;   // commit, then lose the reply
      }
      const out = be.post(body);
      if (crashNext) { crashNext = 0; req.socket.destroy(); return; }
      res.writeHead(200, headers);
      res.end(JSON.stringify(out));
    });
  }).listen(port, '127.0.0.1', () => console.log(`harness listening on ${port}`));
}
