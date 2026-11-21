# Engineering review: November 2026

An independent investigation of scalability, concurrency, security and live-deployment testing,
done against the actual code after an external review.

**Bottom line:** 14 issues were confirmed and fixed. The most serious was a crash during a
correction, which could permanently hide an entry from the employer and the app. No way was found
for an outsider to read, forge or alter records. Two design limitations and three low-probability
risks remain and are documented below.

**What wasn't possible here:** this build environment can't reach `script.google.com`, so nothing in
this report has been run against real Google servers. Everything marked *verified* was reproduced
locally: real agent code, real HTTP, real Chromium, and `Code.gs` running in a harness with fake
Google services. Behaviour that only real Google exhibits is marked *theoretical* and is covered by
the live test runner (`docs/LIVE_TESTING.md`), which still needs to be run against a test
deployment.

## Prioritised findings

Status key: **Verified** = reproduced by a test that failed before the fix and passes after it.
**Code-verified** = confirmed by reading the code and demonstrated on the pre-review code, without
a full failure scenario. **Theoretical** = depends on Google behaviour the harness can't reproduce.

| # | Issue | Severity | Status | Test | Fixed |
|---|---|---|---|---|---|
| 1 | A correction interrupted between its two sheet writes resealed the original without adding the replacement. Every retry then returned `not_found`, so the entry vanished from the dashboard and the app's fix list for good | High | Verified | `test_review.mjs` R1, R2 | Yes |
| 2 | The script lock was released without `SpreadsheetApp.flush()`. Google may hold back sheet writes and apply them in batches, so the next request could read a stale sheet, miss a duplicate, or write into the same "next" row. The app really does send overlapping writes (see 7) | High | Theoretical | Live checks 11, 12 | Yes |
| 3 | A large offline backlog (around 65 or more long notes) made a 100-row batch exceed the 200 KB request limit. The batch was refused on every retry, blocking **all** later events, including clock-ins and clock-outs | Medium | Verified | `test_reliability.py` backlog test | Yes |
| 4 | Every dashboard poll (once a minute) reread and resent the whole sheet: 80,000 cells and 2.7 MB at 10,000 rows, even with no changes | Medium | Verified (measured) | `test_review.mjs` R4, M1 | Yes |
| 5 | About 1 in 64 base64url values starts with `-`, and Sheets may treat such input as a formula or number. Protection relied on column formatting reaching rows added later, which is unconfirmed | Medium | Theoretical | Live check 08; R7 | Yes (defensive) |
| 6 | Privacy side channel: "last reported" was updated by *every* signed request, so private notes and opening "My notes" were visible to the employer as activity times | Low | Verified | R5 | Yes |
| 7 | Two syncs could run at once inside the app (the fix window and the regular sync), sending the same batch up to 4 times | Low | Code-verified | `test_agent.py` audit tests | Yes |
| 8 | Duplicate detection read all 8 columns of every row; only the id column is needed | Low | Verified | R3 | Yes |
| 9 | A busy lock was reported as `bad_request`, which looked like a bug rather than "retry later" | Low | Verified | R6 | Yes |
| 10 | Sync failures were invisible: the tray only said "N waiting to sync", never why | Low | Verified | Reliability tests | Yes |
| 11 | Retrying a failed correction created a new operation with new ids each time | Low | Verified | Reliability tests | Yes |
| 12 | A correction could change the user an entry belongs to (a hardening gap for multi-user use) | Low | Verified | R8 | Yes |
| 13 | A damaged queue file was silently treated as empty and then overwritten, losing unsent events | Low | Code-verified | Audit tests | Yes |
| 14 | `labtracker link` and `revoke-link` left the dashboard link (share key and token) in the terminal scrollback | Low | Code-verified | Not testable (terminal) | Yes |
| 15 | The replay check ran outside the lock, so two identical simultaneous requests could both pass. No integrity impact, because every action is idempotent | Info | Theoretical | Not testable in the harness | Yes |

### Unresolved: documented, not fixed

| Issue | Severity | Why it isn't fixed |
|---|---|---|
| **Revoking a link replaces only the read token, not the share key.** A revoked viewer who later obtains the ciphertext another way could still decrypt it | Medium (design) | A real fix means versioned share keys and re-encrypting history. That's a significant change, and no current threat requires it. Documented in the README |
| One active laptop per user: two laptops with the same keys track clocked-in state separately | Low (design) | Fixing it needs cross-device state, which adds complexity for a rare case. Documented |
| The unauthenticated dashboard endpoint can be called repeatedly by anyone who knows the URL, using up Apps Script executions | Low | It rejects a bad token before reading any data. Rate limiting would also hurt legitimate polling |
| Replay protection depends on CacheService, which may evict entries early | Theoretical | Exploiting it needs a captured request, which TLS prevents, and every action is idempotent |
| Opening the dashboard passes the link (with its key) to the browser as a process argument, visible to *other* user accounts on the same computer | Theoretical | Single-user laptop. The alternatives (temporary files) would put the key on disk |
| A crash in the microseconds between saving the queue and saving the state could leave the tray showing the wrong clocked-in state | Theoretical | No data is lost; the dashboard stays correct |

## 1. Google Sheets scalability

**Volume.** A working day produces about 10 to 15 rows (clock in and out, away periods, the odd note),
so roughly 3,000 rows a year for one person. Measured payload: about 273 bytes per row.

| Rows | Append: cells read (before → after) | Dashboard poll, no changes (before → after) | Dashboard full load |
|---|---|---|---|
| 1,000 | 8,000 → 1,000 | 8,008 cells, 273 KB → **0 cells** | 273 KB |
| 5,000 | 40,000 → 5,000 | 40,008 cells, 1.4 MB → **0 cells** | 1.4 MB |
| 10,000 | 80,000 → 10,000 | 80,008 cells, 2.7 MB → **0 cells** | 2.7 MB |

*Source: `node --test tests/test_review.mjs` (M1), counted by the harness. Real latency must be
measured with `live_integration.py --perf`.*

**Limits** (from Google's published Apps Script quotas, not re-checked in this session; confirm with the live run):
- **Hard limits:** each execution can run for up to 6 minutes; about 30 can run at once per user; a spreadsheet holds up to 10 million cells (about 1.25 million rows here). None of these is close.
- **Daily quota:** Properties reads and writes are limited to about 50,000 a day. After the fix, a dashboard left open all day costs about 1,440 calls a day (one `getProperties` per poll), and the app adds a few hundred.

**Thresholds.** With the fixes:
- **Up to about 20,000 rows (6 to 7 years for one person):** comfortable.
- **Beyond that:** the occasional full refresh after a change starts to take several megabytes.
- **Recommendation:** start a fresh spreadsheet every year or two. Server-side date windowing is possible but not yet justified.
- **Duplicate checks:** they now read one column, so they stay well under a second at that size (to be confirmed live).

**Alternatives considered and rejected:**
- *Caching ids in CacheService:* entries can be evicted, so it can't be relied on for correctness.
- *TextFinder lookups:* one call per id, which is slower for 100-row batches.
- *Incremental row cursors:* corrections rewrite old rows, so this needs extra bookkeeping that the version check makes unnecessary.

## 2. Concurrency and reliability

- **Duplicate prevention is idempotent by row id under the script lock.** It is now also robust to
  Google's batched writes (fix 2). It holds when a reply is lost after the commit (verified: a
  dropped response followed by a retry stores the row once) and when two laptops sync at once
  (verified at the client level; server-side concurrency is live checks 11 and 12).
- **Corrections are crash-safe:** the replacement is written first, then the original is resealed,
  and any retry converges (fix 1, verified with injected crashes at each write).
- **Interrupted clock in or out:** the event is written to the encrypted local queue before anything
  is sent, so a crash or network failure delays it but never loses it.
- **Retries:** the app retries every 15 seconds while there's a queue, with no backoff. That's cheap,
  and errors are now visible in the tray menu.
- **Is idempotency genuinely guaranteed?** Yes for `append` and `adjust`, as long as the lock and
  the flush behave as Google documents. That is exactly what live checks 11 and 12 test, and why
  they should be run.

## 3. Security audit

**Verified sound (with evidence):**
- **AES-256-GCM:** used through OpenSSL and WebCrypto, never hand-written. A fresh 96-bit random
  nonce per message (5,000 sealed in a test with no repeats). At about 3,000 messages a year, the
  chance of a nonce collision is negligible. Associated data binds each ciphertext to its user, row
  and key, so a row can't be swapped or relabelled (tested).
- **Separate random keys:** the share, private and signing keys are independent, and the share key
  can't open private rows (tested).
- **HMAC-SHA256:** covers the version, timestamp, nonce and payload, and is compared in constant time.
  A wrong key, a clock more than 5 minutes off, tampering and replays are all rejected (tested). The
  nonces are remembered for 15 minutes, longer than the 10-minute acceptance window.
- **Private records:** the dashboard receives only shared rows, never private rows or correction
  links (tested). Private activity no longer changes anything the employer can observe (fix 6).
- **No secrets in the repository or its git history** (scanned). The dashboard uses no browser
  storage, cookies, console logging, `eval` or `innerHTML`.
- **Input validation:** a size limit, regex checks on every field, JSON parsing inside a try block,
  and a cap on rows. Sheet values are forced to text (fix 5). Dashboard output uses text nodes only,
  so injection attempts don't execute (tested in Chromium). CSV export contains no user-written text.
- **Local files:** mode 600 in folders with mode 700 (tested), with umask 077. Keys live only in the
  keyring. Queued events are encrypted (tested).

**Limits that remain** (unchanged from the earlier review): malware running as the user can read
the keyring; anyone in the owner's Google or GitHub account can alter data or code; records are
self-reported.

## 4. Live deployment verification

The harness doesn't reproduce how real Sheets converts values, real lock contention, delayed
writes, cache eviction, quotas, time limits, the redirect, or real latency. The full table is in
`docs/LIVE_TESTING.md`.

`tests/live_integration.py` runs 15 checks, plus a 16th with `--perf`, against a **separate**
test spreadsheet and test key. It refuses the real URL and the real key. It writes only random test
rows under a random `itest-…` user, and never deletes anything.

To show the runner itself works, it was run against the harness:
- **Fixed backend:** 16 of 16 passed.
- **Pre-review backend:** it correctly failed 3 checks (version check, concurrent corrections, idempotent retry).

**Status: not yet run against real Google.**

## What was verified, in numbers

- Python: 32 unit and reliability tests pass (crypto, tracker logic, hardening, HTTP retries, concurrent clients, offline backlog).
- Backend: 19 tests pass (authentication, replay, privacy, crash-safety, scaling).
- End to end (real Chromium): 6 checks pass, including the new "unchanged poll" path, with no console or CSP errors.
- GUI: the window test passes.
- Before and after: every *Verified* issue above has a test that failed on the pre-review code and passes now.

## Compatibility and deployment

- **Mixing old and new parts works**, in every direction:
  - The new dashboard with the old script falls back to full loads.
  - The new app with the old script still corrects entries, without the crash-safety.
  - The old app with the new script works.
- **Deploying:** paste the new `backend/Code.gs` and deploy a **New version**, then run `git pull && ./install.sh` on the laptop.
- **Data:** no data migration is needed.
