# Testing against a live Google deployment

The automated tests run `backend/Code.gs` inside a **local harness** (`tests/gas_harness.mjs`) with
fake Google services. That proves the logic, but not how real Google behaves. This guide sets up a
**separate, disposable test deployment** and runs `tests/live_integration.py` against it.

> Never run these checks against your real Labtracker backend. The runner refuses your real
> URL and your real signing key, but the safest rule is a brand-new spreadsheet just for testing.

## What the harness cannot reproduce

| Real Google behaviour | Why it matters | Live check |
|---|---|---|
| Sheets may convert typed-in strings (e.g. a value starting with `-` can become a formula or number) | Ciphertext is base64url; about 1 in 64 values starts with `-` | 08 |
| `LockService` contention between truly simultaneous executions | Duplicate prevention relies on the lock | 11, 12 |
| Sheet writes being batched until `SpreadsheetApp.flush()` | A second request could read a stale sheet | 11, 12 |
| `CacheService` (replay protection) is best-effort and shared across executions | Replay rejection | 05 |
| `PropertiesService` consistency and daily quotas | Read token, version, last-seen | 10, 15 |
| The `302` redirect to `script.googleusercontent.com` and its headers | Every request goes through it | 01 |
| Real `Utilities` byte and base64 semantics | Signature checking | 01, 03 |
| Execution time limit (6 min), concurrency limit (about 30), cold starts, latency | Performance and timeouts | 11, 16 |
| Deployment versions, scopes (`@OnlyCurrentDoc`), "Anyone" access | Setup and authorisation | 01 |

The harness is single-threaded JavaScript, so it can't run two Apps Script executions at the
same time. The concurrency tests in `tests/test_reliability.py` exercise concurrent *clients*
only. Server-side concurrency is only verified by checks 11 and 12 below.

## One-time setup (about 10 minutes)

1. Create a **new** Google Sheet named `Labtracker TEST`. Don't reuse your real one.
2. In it, open **Extensions → Apps Script** and paste in the current `backend/Code.gs`. Save.
3. Click **Deploy → New deployment → Web app**. Set Execute as: **Me**, Who has access: **Anyone**, then click **Deploy**. Copy the URL.
4. On your laptop, run:
   ```bash
   cd ~/Labtracker
   python3 tests/live_integration.py init
   ```
   This creates a separate **test** signing key, stored in `~/.config/labtracker-livetest/`.
5. In the **TEST** Apps Script project only, add the Script Property `LT_AUTH_KEY` with the value printed by `init`.

## Run

```bash
python3 tests/live_integration.py run --url "<TEST web app URL>"
python3 tests/live_integration.py run --url "<TEST web app URL>" --perf 5000   # adds timings
```

It asks you to type `TEST` to confirm, prints PASS or FAIL for each check, and saves a JSON report.

| # | Verifies |
|---|---|
| 01–02 | Real signing and authentication; installing a read token |
| 03–06 | Rejection of a wrong key, a clock 10 minutes off, a replayed request, malformed input and an oversized body |
| 07 | Appending, and a duplicate append is ignored |
| 08 | Ciphertext with a leading `-` or made of digits is stored exactly, with no coercion by Sheets |
| 09 | Private rows and correction links never reach the dashboard |
| 10 | The dashboard's version check: private activity changes neither the version nor last-seen |
| 11 | 8 parallel writers with overlapping rows: each id stored exactly once |
| 12 | 4 simultaneous corrections of one entry: exactly one replacement |
| 13 | A repeated correction is idempotent |
| 14 | The offline queue delivers exactly once after an outage |
| 15 | Revocation: the old read token stops working immediately |
| 16 | With `--perf N`: median latency of append, dashboard (full and unchanged) and read, after N extra rows |

**Cleanup:** the runner never deletes anything. Delete the `Labtracker TEST` spreadsheet when you're
done. Your real data is never touched.

## What a pass does and doesn't prove

A pass shows that the deployed script behaved correctly *for these runs*. Concurrency problems are
probabilistic, so run checks 11 and 12 a few times. Timings vary with Google's load and the size
of the sheet.
