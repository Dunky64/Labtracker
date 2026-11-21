---
name: labtracker
description: Context, rules and procedures for the Labtracker project (free, end-to-end encrypted clock-in/out tracker - Linux Mint tray app, Google Apps Script + Sheet backend, GitHub Pages dashboard). Use this skill whenever the user mentions Labtracker, clocking in/out, the tray app, the employer dashboard, the Apps Script backend or Code.gs, LT_AUTH_KEY, setup or deployment errors, live/integration tests, security reviews, new features (iPhone, multi-user) or maintenance of this repository - even if they don't name the project.
---

# Labtracker

A free clock-in/out tracker for one remote worker (multi-user later). The owner's laptop encrypts and
signs every event; Google stores only ciphertext; the employer's browser decrypts with the key in a
private link. Repo: `Dunky64/Labtracker` (public; `main` deploys the dashboard via GitHub Pages /docs).

## Working with the owner (read first)

- The owner is **not a coder** and follows steps on a phone or the Mint desktop. Give short, numbered
  steps that name the exact buttons. Explain *why* briefly; skip jargon.
- They want **low token usage**: be brief, read only the files a task needs, run only the relevant
  tests, and don't restate context they already know.
- Give honest pushback and recommendations (they asked not to be "yes-manned"); ask before anything
  hard to undo.
- UK time (Europe/London) for everything user-facing.

## Rules that protect the owner

- **Never ask for, display, or repeat keys, tokens, recovery codes or the web-app URL.** If a key appears
  in a screenshot, tell them to rotate it (`labtracker setup` → n → DELETE, then replace `LT_AUTH_KEY`).
  Ask them to cover keys in future screenshots.
- Keep personal details out of the public repo. No secrets in code, tests or docs.
- Open pull requests only when asked; never force-push; merge only when the owner says so.
- Never run tests or experiments against the owner's real backend — use a separate test deployment
  (`docs/LIVE_TESTING.md`).
- Never weaken encryption, privacy, authentication or integrity for convenience or performance.

## Architecture in one screen

| Part | Path | Notes |
|---|---|---|
| Tray app (Python/GTK, XApp.StatusIcon) | `agent/labtracker/` | `tracker.py` = all logic, no GUI (test here); `gui.py` windows; `cli.py` commands; `crypto.py`, `keystore.py` (GNOME keyring), `backend.py` (signed HTTPS), `storage.py` (600/700 files) |
| Backend (Apps Script bound to a Sheet) | `backend/Code.gs` | Actions: ping, append, adjust, read, set_read_token (signed); `dashboard` (read token) |
| Dashboard (static, no deps) | `docs/index.html`, `app.js`, `model.js`, `crypto.js` | Strict CSP; text nodes only |
| Installer | `install.sh` | apt packages only; launcher runs `python3 -I` |

Keys (all in the keyring; backed up by the recovery code): **share** (AES-256-GCM, timeline; employer
gets it in the link's `#fragment`), **private** (notes + originals of corrected entries; owner only),
**auth** (HMAC-SHA256; also Script Property `LT_AUTH_KEY`), **read token** (dashboard access; revocable).

## Invariants — preserve these in any change

- AAD = `labtracker/v1|user|row_id|key_id`; random 12-byte nonce per seal. Signed string =
  `v1\nts\nnonce\npayload`; ±5 min clock window; nonce checked **inside** the script lock.
- Sheet `events`: `server_ts,id,user,key_id(s|p),nonce,ct,supersedes,resealed_at`. Append-only;
  appends idempotent by `id` (read the id column only).
- Corrections: write the replacement first, then reseal the original with the private key; any retry
  converges and reports the standing `replacement`.
- `SpreadsheetApp.flush()` before releasing the lock; lock timeout returns `busy`.
- Each write formats its target range as text first (base64url values can start with `-`).
- `LT_SHARED_VER` changes only on shared changes; `LT_LAST_SEEN` only on ping/shared appends — so
  private activity stays invisible to the employer. The dashboard never receives `p` rows or `supersedes`.
- Agent: one sync at a time; batches ≤100 rows and ≤120k chars; events queued (encrypted) before
  sending; idle tool only from `/usr/bin` or `/usr/local/bin`.
- Product rules agreed with the owner: away = 10 min no input, shown but **not deducted**; prompt
  "forgot to clock out?", auto clock-out after 30 min at **last active** time; corrections show only an
  "adjusted" marker; notes private.

## Making a change

1. Read only the files involved, plus the relevant section of `docs/ENGINEERING_REVIEW.md` if touching
   sync, corrections or the backend.
2. Write or update a test that fails first, then fix.
3. Run the relevant tests (in the build container use `python3.12`; `/usr/bin/python3` is 3.13 and lacks GTK bindings):
   ```bash
   python3.12 -m unittest discover -s tests           # agent + reliability (spawns node harness)
   node --test tests/test_backend.mjs tests/test_review.mjs
   ```
   UI or dashboard changes: also the e2e (`tests/e2e_seed.py` + `tests/e2e_dashboard.cjs`, see README)
   and `xvfb-run python3.12 tests/gui_smoke.py <port> <key> <outdir>`.
4. The build container cannot reach `script.google.com` or `github.io`; say so rather than claiming
   live verification. Live checks: `tests/live_integration.py` (owner runs it, test deployment only).
5. Update README/docs if behaviour changes. Commit to the working branch; PR when asked.
6. Tell the owner the deploy steps: if `Code.gs` changed → paste it in Apps Script, **Deploy → Manage
   deployments → ✏️ → New version → Deploy** (URL unchanged); laptop → `cd ~/Labtracker && git pull && ./install.sh`.

## Fast troubleshooting

| Symptom | Cause / fix |
|---|---|
| 403 or Google sign-in page | Deployment must be Execute as **Me**, access **Anyone**; work/school accounts may block it |
| `bad_signature` | `LT_AUTH_KEY` mismatch: clear value, re-paste; run `labtrackerSelfTest` in the editor |
| `not_configured` / `clock` | Key missing or not 44 chars / laptop clock off → enable network time |
| Dashboard 404 | Pages path is case-sensitive: `https://dunky64.github.io/Labtracker/` |
| "must start with https://" in setup | A bad saved dashboard address; type the URL above |
| No tray icon | Add the XApp Status Applet to the panel |
| Events waiting to sync | Tray → "Why aren't they syncing?" |

## Open backlog (owner's priorities)

1. Owner: deploy the review release (Code.gs New version + `git pull && ./install.sh`), run
   `tests/live_integration.py` against a test deployment, enable 2FA on Google and GitHub, protect `main`.
2. iPhone: home-screen web page for clock in/out + notes (no idle detection on iOS); needs a design for
   holding the signing key safely on the phone.
3. Multi-user employer view (rows already carry `user`).
4. Share-key rotation so revocation also replaces the key (currently token-only — documented limitation).
5. Optional: tamper-evident hash chain; "recorded late" marker; tagged releases.

## Deeper references (read only when needed)

- `README.md` — user guide, setup, security summary, troubleshooting
- `docs/DESIGN.md` — original decisions and later implementation notes
- `docs/ENGINEERING_REVIEW.md` — verified issues, fixes, remaining risks, scalability numbers
- `docs/LIVE_TESTING.md` — what the harness can't reproduce; safe live test procedure
