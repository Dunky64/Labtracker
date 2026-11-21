// Runs backend/Code.gs locally with in-memory fakes of the Apps Script services.
// Usage as a server:  node tests/gas_harness.mjs <port> <authKeyB64>
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

export function createBackend(props = {}) {
  const data = [];               // rows (including header) of the "events" sheet
  let sheetExists = false;
  const sheet = {
    getLastRow: () => data.length,
    getMaxRows: () => 1000,
    setFrozenRows: () => {},
    getRange: (r, c, nr, nc) => ({
      setNumberFormat: () => {},
      getValues: () => data.slice(r - 1, r - 1 + nr).map((row) => row.slice(c - 1, c - 1 + nc)),
      setValues: (vals) => vals.forEach((v, i) => {
        while (data.length < r + i) data.push([]);
        data[r - 1 + i] = v.slice();
      }),
    }),
  };
  const cache = new Map();
  const ctx = {
    Date, Math, JSON, String, Number, Array, Object,
    SpreadsheetApp: { getActiveSpreadsheet: () => ({
      getSheetByName: () => (sheetExists ? sheet : null),
      insertSheet: () => { sheetExists = true; return sheet; },
    }) },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (k) => (k in props ? props[k] : null),
      setProperty: (k, v) => { props[k] = String(v); },
    }) },
    CacheService: { getScriptCache: () => ({
      get: (k) => cache.get(k) || null, put: (k, v) => cache.set(k, v),
    }) },
    LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
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
    props, data,
    post: (body) => JSON.parse(ctx.doPost({ postData: { contents: body } }).getContent()),
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
  http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const headers = { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' };
      if (req.method === 'GET' && req.url === '/__sheet') {
        res.writeHead(200, headers); res.end(JSON.stringify(be.data)); return;
      }
      res.writeHead(200, headers);
      res.end(JSON.stringify(be.post(body)));
    });
  }).listen(port, '127.0.0.1', () => console.log(`harness listening on ${port}`));
}
