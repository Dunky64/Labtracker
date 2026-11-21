/** @OnlyCurrentDoc */
// ^ Limits this script's Google permission to THIS spreadsheet only (not your whole Drive).

/**
 * Labtracker backend: Google Apps Script web app bound to a Google Sheet.
 *
 * What it does:
 *   - Accepts SIGNED requests from your Labtracker desktop app (HMAC-SHA256).
 *   - Stores events in the "events" sheet. Event contents are encrypted on your
 *     computer before they are sent, so this script and Google never see plaintext.
 *   - Serves the encrypted timeline to the employer dashboard when it presents
 *     the correct read token. The dashboard decrypts in the employer's browser.
 *
 * One-time setup (full guide in the README):
 *   Project Settings -> Script Properties -> add LT_AUTH_KEY (value from `labtracker setup`).
 *   Deploy -> New deployment -> Web app -> Execute as: Me, Who has access: Anyone.
 *
 * You do not need to edit anything in this file.
 */

var SHEET_NAME = 'events';
var HEADERS = ['server_ts', 'id', 'user', 'key_id', 'nonce', 'ct', 'supersedes', 'resealed_at'];
var COL = { server_ts: 0, id: 1, user: 2, key_id: 3, nonce: 4, ct: 5, supersedes: 6, resealed_at: 7 };

var MAX_BODY = 200000;        // bytes
var MAX_ROWS_PER_CALL = 200;
var MAX_CT = 12000;           // characters of base64 ciphertext per row
var MAX_SKEW_MS = 5 * 60 * 1000;
var NONCE_TTL_S = 15 * 60;
var LOCK_WAIT_MS = 20000;

var RE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var RE_USER = /^[a-z0-9_-]{1,32}$/;
var RE_B64 = /^[A-Za-z0-9_-]+={0,2}$/;

function doGet() {
  // Deliberately reveals nothing.
  return ContentService.createTextOutput('Labtracker backend is running.');
}

function doPost(e) {
  try {
    var body = (e && e.postData && e.postData.contents) || '';
    if (body.length === 0 || body.length > MAX_BODY) return reply_({ ok: false, error: 'bad_request' });
    var req = JSON.parse(body);

    // Unsigned, read-only request from the employer dashboard.
    if (req && req.action === 'dashboard') return reply_(dashboard_(req));

    var v = verify_(req);
    if (v.error) return reply_({ ok: false, error: v.error, server_now: Date.now() });
    var payload = v.payload;

    // Every signed request runs under the script lock, so writes never interleave.
    var lock = LockService.getScriptLock();
    if (!lock.tryLock(LOCK_WAIT_MS)) return reply_({ ok: false, error: 'busy' });
    try {
      // Replay check inside the lock: two copies of one request can't both pass.
      if (!claimNonce_(req.nonce)) return reply_({ ok: false, error: 'replayed', server_now: Date.now() });
      switch (payload.action) {
        case 'ping': markSeen_(); return reply_({ ok: true, server_ts: Date.now() });
        case 'append': return reply_(append_(payload));
        case 'adjust': return reply_(adjust_(payload));
        case 'read': return reply_(readAll_());
        case 'set_read_token': return reply_(setReadToken_(payload));
        default: return reply_({ ok: false, error: 'bad_request' });
      }
    } finally {
      // Apply pending sheet writes BEFORE another execution can take the lock; otherwise it
      // may read a stale sheet (missing a duplicate, or writing into the same "next" row).
      SpreadsheetApp.flush();
      lock.releaseLock();
    }
  } catch (err) {
    // Never echo internals back to the caller.
    return reply_({ ok: false, error: 'bad_request' });
  }
}

/* ---------------------------------------------------------------- auth -- */

/**
 * Returns {payload} or {error}. Error codes say which check failed, so setup problems are easy
 * to fix: not_configured (no LT_AUTH_KEY), clock (computer clock off by > 5 min),
 * bad_signature (LT_AUTH_KEY doesn't match the laptop's key), replayed, bad_request.
 */
function verify_(req) {
  if (!req || req.v !== 1 || typeof req.ts !== 'number' || typeof req.nonce !== 'string' ||
      typeof req.payload !== 'string' || typeof req.sig !== 'string') return { error: 'bad_request' };
  if (!RE_B64.test(req.nonce) || req.nonce.length < 16 || req.nonce.length > 64) return { error: 'bad_request' };
  if (!RE_B64.test(req.sig)) return { error: 'bad_request' };

  var key = authKey_();
  if (!key) return { error: 'not_configured' };
  if (Math.abs(Date.now() - req.ts) > MAX_SKEW_MS) return { error: 'clock' };

  var msg = 'v1\n' + req.ts + '\n' + req.nonce + '\n' + req.payload;
  var expected = Utilities.computeHmacSha256Signature(Utilities.newBlob(msg).getBytes(), key);
  var given;
  try { given = Utilities.base64DecodeWebSafe(req.sig); } catch (e) { return { error: 'bad_signature' }; }
  if (!bytesEqual_(expected, given)) return { error: 'bad_signature' };

  var payload = JSON.parse(req.payload);
  if (!payload || typeof payload.action !== 'string') return { error: 'bad_request' };
  return { payload: payload };
}

/** Replay protection: each nonce works once (call while holding the script lock). */
function claimNonce_(nonce) {
  var cache = CacheService.getScriptCache();
  var nk = 'n:' + nonce;
  if (cache.get(nk)) return false;
  cache.put(nk, '1', NONCE_TTL_S);
  return true;
}

/**
 * "App last reported" for the dashboard. Updated only by pings and shared writes, which happen
 * while clocked in, so private activity (notes, opening My notes) leaves no visible trace.
 */
function markSeen_() {
  PropertiesService.getScriptProperties().setProperty('LT_LAST_SEEN', String(Date.now()));
}

/** Bumped whenever the shared (employer-visible) data changes; lets dashboard polls skip work. */
function bumpSharedVersion_() {
  var props = PropertiesService.getScriptProperties();
  props.setProperty('LT_SHARED_VER', String(Number(props.getProperty('LT_SHARED_VER') || 0) + 1));
}

function authKey_() {
  var keyB64 = PropertiesService.getScriptProperties().getProperty('LT_AUTH_KEY');
  if (!keyB64) return null;
  try {
    var key = Utilities.base64DecodeWebSafe(String(keyB64).replace(/\s+/g, ''));
    return key.length === 32 ? key : null;
  } catch (e) {
    return null;
  }
}

/**
 * Setup helper: choose "labtrackerSelfTest" in the editor's function list and click Run.
 * The log shows whether LT_AUTH_KEY is present and its first/last characters, so you can
 * compare it with the key shown by `labtracker setup` without revealing it in full.
 */
function labtrackerSelfTest() {
  var raw = PropertiesService.getScriptProperties().getProperty('LT_AUTH_KEY');
  if (!raw) {
    Logger.log('LT_AUTH_KEY is MISSING. Add it in Project Settings -> Script Properties and click Save.');
    return;
  }
  var clean = String(raw).replace(/\s+/g, '');
  Logger.log('LT_AUTH_KEY found: ' + clean.length + ' characters (should be 44), starts "' +
             clean.slice(0, 4) + '", ends "' + clean.slice(-4) + '".');
  Logger.log(authKey_() ? 'Key format OK.' : 'Key format is WRONG: paste it again exactly as shown by setup.');
  Logger.log('Google time now: ' + new Date().toISOString() + ' (UTC). Compare with your laptop clock.');
}

function bytesEqual_(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= (a[i] ^ b[i]);
  return diff === 0;
}

function stringsEqual_(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= (a.charCodeAt(i) ^ b.charCodeAt(i));
  return diff === 0;
}

/* --------------------------------------------------------------- sheet -- */

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
    sh.setFrozenRows(1);
  }
  // Store everything except the timestamp as plain text (whole columns, so rows added later
  // are covered too) so Sheets never reinterprets a value as a number or formula.
  var props = PropertiesService.getScriptProperties();
  if (props.getProperty('LT_FORMAT_V') !== '2') {
    sh.getRange('B:H').setNumberFormat('@');
    props.setProperty('LT_FORMAT_V', '2');
  }
  return sh;
}

function rows_(sh) {
  var n = sh.getLastRow() - 1;
  if (n <= 0) return [];
  return sh.getRange(2, 1, n, HEADERS.length).getValues();
}

/** One column (1-based) of all data rows, flattened. Much cheaper than reading whole rows. */
function column_(sh, col) {
  var n = sh.getLastRow() - 1;
  if (n <= 0) return [];
  return sh.getRange(2, col, n, 1).getValues().map(function (r) { return r[0]; });
}

/** Write whole rows at `at` (1-based), with columns B-H forced to plain text first. */
function writeRows_(sh, at, values) {
  sh.getRange(at, 2, values.length, HEADERS.length - 1).setNumberFormat('@');
  sh.getRange(at, 1, values.length, HEADERS.length).setValues(values);
}

function validRow_(r) {
  return r && RE_ID.test(r.id) && RE_USER.test(r.user) && (r.key_id === 's' || r.key_id === 'p') &&
    typeof r.nonce === 'string' && RE_B64.test(r.nonce) && r.nonce.length <= 32 &&
    typeof r.ct === 'string' && RE_B64.test(r.ct) && r.ct.length <= MAX_CT;
}

function append_(p) {
  if (!Array.isArray(p.rows) || p.rows.length === 0 || p.rows.length > MAX_ROWS_PER_CALL) {
    return { ok: false, error: 'bad_request' };
  }
  for (var i = 0; i < p.rows.length; i++) if (!validRow_(p.rows[i])) return { ok: false, error: 'bad_row' };

  var sh = sheet_();
  var existing = {};
  column_(sh, COL.id + 1).forEach(function (id) { existing[id] = true; });

  var now = Date.now();
  var out = [];
  var shared = false;
  p.rows.forEach(function (r) {
    if (existing[r.id]) return;            // idempotent: retries never duplicate
    existing[r.id] = true;
    if (r.key_id === 's') shared = true;
    out.push([now, r.id, r.user, r.key_id, r.nonce, r.ct, '', '']);
  });
  if (out.length) writeRows_(sh, sh.getLastRow() + 1, out);
  if (shared) { bumpSharedVersion_(); markSeen_(); }
  return { ok: true, inserted: out.length, server_ts: now };
}

/**
 * Replace an event with a corrected one.
 * The original row is re-encrypted by the desktop app with the owner's PRIVATE key,
 * so the employer can no longer read the original value; the new row is shared.
 *
 * Crash-safe and idempotent: the replacement is written FIRST, then the original is resealed.
 * Any retry (same or different replacement id) finishes whichever step is missing and never
 * adds a second replacement, so an interrupted correction can't lose or duplicate an entry.
 */
function adjust_(p) {
  if (!RE_ID.test(p.supersedes) || !p.reseal || !validRow_(p.row) || p.row.key_id !== 's') {
    return { ok: false, error: 'bad_request' };
  }
  var re = { id: p.supersedes, user: p.row.user, key_id: 'p', nonce: p.reseal.nonce, ct: p.reseal.ct };
  if (!validRow_(re)) return { ok: false, error: 'bad_request' };

  var sh = sheet_();
  var ids = column_(sh, COL.id + 1);
  var idx = ids.indexOf(p.supersedes);
  if (idx < 0) return { ok: false, error: 'not_found' };
  var orig = sh.getRange(idx + 2, 1, 1, HEADERS.length).getValues()[0];
  if (orig[COL.user] !== p.row.user) return { ok: false, error: 'bad_request' };

  var replIdx = column_(sh, COL.supersedes + 1).indexOf(p.supersedes);
  var hasReplacement = replIdx >= 0;
  if (!hasReplacement && orig[COL.key_id] !== 's') return { ok: false, error: 'not_found' };
  var replacement = hasReplacement ? ids[replIdx] : p.row.id;

  var now = Date.now();
  var changed = false;
  if (!hasReplacement) {
    if (ids.indexOf(p.row.id) >= 0) return { ok: false, error: 'bad_request' };   // id reused
    writeRows_(sh, sh.getLastRow() + 1, [[now, p.row.id, p.row.user, 's', p.row.nonce, p.row.ct, p.supersedes, '']]);
    changed = true;
  }
  if (orig[COL.key_id] === 's') {
    writeRows_(sh, idx + 2, [[orig[COL.server_ts], orig[COL.id], orig[COL.user], 'p', re.nonce, re.ct,
                              orig[COL.supersedes], now]]);
    changed = true;
  }
  if (changed) bumpSharedVersion_();
  // `replacement` tells the client which entry now stands; if it isn't the one it sent, an
  // earlier attempt already corrected this entry and the client should reload.
  return { ok: true, replacement: replacement, already: !changed, server_ts: now };
}

function readAll_() {
  var rows = rows_(sheet_()).map(function (r) {
    return { server_ts: Number(r[COL.server_ts]), id: r[COL.id], user: r[COL.user], key_id: r[COL.key_id],
             nonce: r[COL.nonce], ct: r[COL.ct] };
  });
  return { ok: true, rows: rows };
}

function setReadToken_(p) {
  if (typeof p.token !== 'string' || p.token.length < 40 || !RE_B64.test(p.token)) {
    return { ok: false, error: 'bad_request' };
  }
  PropertiesService.getScriptProperties().setProperty('LT_READ_TOKEN', p.token);
  return { ok: true };
}

/* ----------------------------------------------------------- dashboard -- */

function dashboard_(req) {
  var props = PropertiesService.getScriptProperties().getProperties();   // one quota unit
  var token = props.LT_READ_TOKEN;
  if (!token || typeof req.token !== 'string' || !stringsEqual_(req.token, token)) {
    return { ok: false, error: 'unauthorized' };
  }
  var ver = String(props.LT_SHARED_VER || '0');
  var lastSeen = Number(props.LT_LAST_SEEN || 0);
  // Nothing shared has changed since the caller's last full load: skip reading the sheet.
  if (typeof req.ver === 'string' && req.ver === ver) {
    return { ok: true, unchanged: true, ver: ver, last_seen: lastSeen, server_now: Date.now() };
  }

  // Only shared rows. Private rows (notes, original values of adjusted entries)
  // and the "supersedes" links are never sent to the dashboard.
  var rows = [];
  rows_(sheet_()).forEach(function (r) {
    if (r[COL.key_id] !== 's') return;
    rows.push({ server_ts: Number(r[COL.server_ts]), id: r[COL.id], user: r[COL.user],
                nonce: r[COL.nonce], ct: r[COL.ct] });
  });
  return { ok: true, rows: rows, ver: ver, last_seen: lastSeen, server_now: Date.now() };
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
