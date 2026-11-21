# Labtracker — Design (draft v0.1)

Status: **v0.1 implemented.** The open questions are answered below.

## Goal
A free, minimal-infrastructure clock-in/clock-out tracker for remote work.
The worker owns the records; the employer gets a read-only live dashboard.
Single user first, designed so it can grow to one employer viewing many employees.

## Components
| Part | Tech | Runs where | Cost |
|---|---|---|---|
| Desktop agent | Python 3 + GTK tray icon (apt packages only) | User's Linux Mint 22.3 Cinnamon (X11) | Free |
| Backend | Google Apps Script web app | User's Google account | Free |
| Storage | Google Sheet (append-only event log, ciphertext only) | User's Google account | Free |
| Dashboard | Static HTML/JS (no third-party libraries) | GitHub Pages | Free |
| iPhone (later) | Home-screen web page: clock in/out + notes only | Safari | Free |

## Data model — append-only event log
Every action is a new event. Nothing is overwritten. A fix is recorded as a new `adjust` event that points to the original.

Plaintext event (before encryption):
```json
{ "v": 1, "id": "<uuid4>", "user": "u1", "type": "clock_in",
  "client_ts": "2026-11-20T09:02:11Z", "ref": null }
```
Types: `clock_in`, `clock_out`, `idle_start`, `idle_end`, `idle_prompt`,
`auto_clock_out`, `adjust`, `note`.

Sheet row: `server_ts | user | key_id | nonce | ciphertext`. The Sheet never holds plaintext.

## Security design
- **Transport:** HTTPS/TLS to `script.google.com`, enforced by Google.
- **Content encryption:** AES-256-GCM, done before anything leaves the device.
  - Python uses the `cryptography` library (apt: `python3-cryptography`).
  - The browser uses the built-in WebCrypto API.
- **Three keys,** generated on the device and stored in the GNOME Keyring (Secret Service):
  - `K_share`: encrypts timeline events. The employer gets it inside the dashboard link's `#fragment`, which browsers never send to any server.
  - `K_private`: encrypts notes and the original values of adjusted entries. Only the user has it, so the employer sees an "adjusted" marker and nothing more.
  - `K_auth`: HMAC-SHA256 request signing. It is also stored in the Apps Script's Script Properties.
- **Request authentication:** each POST is signed with `HMAC(K_auth, timestamp|nonce|body)`. The script rejects:
  - a bad signature,
  - a clock skew of more than 5 minutes,
  - a reused nonce (held in CacheService).
- **Agent hardening:**
  - Runs as a normal user, never as root, and listens on no ports.
  - Only connects outbound to Google.
  - Records **only** how long the keyboard and mouse have been idle (via `xprintidle`). No window titles, screenshots, keystrokes or file access.
  - Events waiting to be sent while offline are stored encrypted on disk.
- **Dashboard hardening:** a strict Content-Security-Policy, no external scripts, and decryption happens only in the browser.
- **Key rotation:** a new `K_share` (meaning a new dashboard link) can be issued at any time. The old link then stops working.

### What this does NOT protect against
- Google can see that events happen, when, and from which IP address. It cannot see what they are.
- Someone with full control of the Mint machine or the user's Google account.
- A leaked dashboard link. Anyone holding it can see the timeline until the key is rotated.

## Behaviour
- **Idle:** after 10 minutes with no input, an `idle_start` event is logged, backdated to the last input. A desktop notification then asks "Forgot to clock out?"
- **Auto clock-out:** if there is no response within 30 minutes, an `auto_clock_out` event is recorded (time to be confirmed, see open question 3).
- **Corrections** are made from the tray app as `adjust` events. The dashboard shows an "adjusted" marker only.
- **Dashboard views:** live status, today's full timeline, and week and month totals. Totals are worked out in the browser from the event log.
- **Time zone:** everything is stored in UTC and shown as Europe/London.

## Decisions (answered)
1. The Mint machine is the user's personal laptop, so no IT approval is needed.
2. Idle time is **shown** on the timeline but **not** deducted from hours worked.
3. An auto clock-out uses the **last active** time.
4. The repo is public. It holds no secrets or data.

## Implementation notes (v0.1)
- The user pastes only `LT_AUTH_KEY` into Script Properties. The read token is pushed by a signed `set_read_token` call, which also powers `labtracker revoke-link`.
- `ping` (every 5 min while clocked in) updates `LT_LAST_SEEN`, so the dashboard can warn when the app has stopped reporting while clocked in.
- **Sleep and shutdown gaps:** if a tick arrives late by at least the idle threshold, the gap is treated as away time. The prompt must then be answered: resuming activity does *not* resolve it. With no answer, an auto clock-out happens at the last tick before the gap. Clocking in resets the gap detector.
- **Adjustments:** the original row is re-encrypted with `K_private`, using the same id and AAD key_id `p`, then a new shared row with `adjusted: true` is appended. A private note records the original time and the reason.
- Appends are idempotent by row id, so retries from the offline queue never create duplicates.

## Changes from the November 2026 engineering review
- **Concurrency:** all signed requests run under the script lock. `SpreadsheetApp.flush()` runs before the lock is released. The replay (nonce) check happens inside the lock. A lock timeout returns `busy`, which the client retries.
- **Corrections** write the replacement first, then reseal the original. Any retry completes the missing step and never adds a second replacement; the response names the `replacement` that stands.
- **Duplicate detection** reads only the id column. Each write formats its target range as text before writing values.
- **Dashboard polling** sends the data version (`ver`). `LT_SHARED_VER` changes only when shared data changes, so an unchanged poll reads no rows. `LT_LAST_SEEN` is updated only by `ping` and shared appends, so private activity is invisible to the employer.
- **Agent:** sync batches are limited by size and count, only one sync runs at a time, the last sync error is shown in the tray menu, correction retries reuse the same operation, and a damaged queue file is kept aside instead of being overwritten.
