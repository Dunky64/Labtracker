# Labtracker — Design (draft v0.1)

Status: **draft, awaiting sign-off on open questions** (see bottom).

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

## Open questions
1. Is the Mint machine work-issued, or does it handle the employer's sensitive data? If yes, IT approval is needed before installing.
2. Is idle time subtracted from worked hours, or only displayed?
3. Should the auto clock-out time be the last activity (recommended) or the moment it fires?
4. Is it OK for the GitHub repo to be public? Free GitHub Pages requires a public repo. No secrets or data are kept in the repo.
