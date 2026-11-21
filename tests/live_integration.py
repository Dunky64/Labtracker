#!/usr/bin/env python3
"""Live integration tests against a SEPARATE, disposable Google Apps Script test deployment.

Never point this at your real Labtracker backend. See docs/LIVE_TESTING.md for the setup.

    python3 tests/live_integration.py init                       # make a test signing key
    python3 tests/live_integration.py run --url <TEST /exec URL>  # run all checks
    python3 tests/live_integration.py run --url <TEST /exec URL> --perf 5000   # + timings

Safety rails:
  * refuses the URL saved in your real Labtracker config, and refuses if the test key equals your
    real signing key (checked when the keyring is available);
  * every row it writes belongs to a random user id "itest-xxxxxx" and is random test data;
  * it never deletes anything. Delete the whole test spreadsheet when you're done.
"""
import argparse
import base64
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from labtracker import crypto  # noqa: E402
from labtracker.backend import Backend, BackendError, valid_webapp_url  # noqa: E402
from labtracker.keystore import Keys  # noqa: E402
from labtracker.storage import Store, config_dir  # noqa: E402
from labtracker.tracker import Tracker  # noqa: E402

KEY_FILE = Path.home() / ".config" / "labtracker-livetest" / "test-signing-key"


def b64(n):
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).decode().rstrip("=")


class Live:
    def __init__(self, url, auth, harness=False):
        self.url, self.auth = url, auth
        self.be = Backend(url, auth, timeout=60, _test_mode=harness)
        self.user = "itest-" + secrets.token_hex(3)
        self.results = []
        self.timings = {}

    # --- transport ------------------------------------------------------------------------
    def raw(self, body: str) -> dict:
        req = urllib.request.Request(self.url, data=body.encode(), method="POST",
                                     headers={"Content-Type": "text/plain;charset=utf-8"})
        with self.be._opener.open(req, timeout=60) as r:          # same TLS + redirect policy
            return json.loads(r.read().decode())

    def signed(self, payload: dict, key=None, ts=None, nonce=None) -> str:
        p = json.dumps(payload, separators=(",", ":"))
        ts = int(time.time() * 1000) if ts is None else ts
        nonce = nonce or b64(18)
        return json.dumps({"v": 1, "ts": ts, "nonce": nonce, "payload": p,
                           "sig": crypto.sign(key or self.auth, ts, nonce, p)})

    def call(self, action, **data):
        t0 = time.perf_counter()
        try:
            return self.be.call(action, **data)
        finally:
            self.timings.setdefault(action, []).append(time.perf_counter() - t0)

    def dash(self, token, ver=None):
        body = {"action": "dashboard", "token": token}
        if ver:
            body["ver"] = ver
        t0 = time.perf_counter()
        out = self.raw(json.dumps(body))
        self.timings.setdefault("dashboard" + ("-unchanged" if ver else ""), []).append(time.perf_counter() - t0)
        return out

    def row(self, key_id="s", **o):
        r = {"id": str(uuid.uuid4()), "user": self.user, "key_id": key_id, "nonce": b64(12), "ct": b64(90)}
        r.update(o)
        return r

    def mine(self):
        return [r for r in self.call("read")["rows"] if r["user"] == self.user]

    # --- reporting ------------------------------------------------------------------------
    def check(self, name, fn):
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            ok = True
        except AssertionError as e:
            ok, detail = False, str(e)
        except Exception as e:  # noqa: BLE001 - every failure must be reported, not crash the run
            ok, detail = False, f"{type(e).__name__}: {e}"
        self.results.append({"check": name, "ok": ok, "detail": detail, "s": round(time.perf_counter() - t0, 2)})
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({time.perf_counter() - t0:.1f}s) {detail}")

    # --- checks ---------------------------------------------------------------------------
    def run(self, perf=0):
        token = crypto.new_token()
        self.check("01 signed ping accepted", lambda: self.call("ping") and None)
        self.check("02 read token installed", lambda: self.call("set_read_token", token=token) and None)
        self.check("03 wrong key -> bad_signature", lambda: self.expect(self.signed({"action": "ping"}, key=crypto.new_key()), "bad_signature"))
        self.check("04 clock 10 min off -> clock", lambda: self.expect(self.signed({"action": "ping"}, ts=int(time.time() * 1000) - 600_000), "clock"))
        self.check("05 replayed request -> replayed", self.replay)
        self.check("06 malformed / oversized -> bad_request", self.malformed)
        self.check("07 append + exact duplicate is ignored", self.duplicate)
        self.check("08 values survive Sheets unchanged (leading '-', digits)", self.fidelity)
        self.check("09 private rows never reach the dashboard", lambda: self.privacy(token))
        self.check("10 dashboard version: unchanged / private / shared", lambda: self.versioning(token))
        self.check("11 concurrent appends: each id stored exactly once", self.concurrent_appends)
        self.check("12 concurrent corrections of one entry: one replacement", lambda: self.concurrent_adjust(token))
        self.check("13 correction retried: idempotent", lambda: self.adjust_retry(token))
        self.check("14 offline queue drains exactly once", self.offline_recovery)
        self.check("15 revoked read token stops working", lambda: self.revocation(token))
        if perf:
            self.check(f"16 performance with {perf} extra rows", lambda: self.performance(perf))
        return self.results

    def expect(self, body, code):
        out = self.raw(body)
        assert out.get("error") == code, f"expected {code}, got {out}"

    def replay(self):
        body = self.signed({"action": "ping"})
        assert self.raw(body).get("ok"), "first copy refused"
        self.expect(body, "replayed")

    def malformed(self):
        self.expect("{not json", "bad_request")
        self.expect(json.dumps({"v": 1}), "bad_request")
        self.expect(self.signed({"action": "append", "rows": [{"id": "x"}]}), "bad_row")
        out = self.raw("x" * 250_000)
        assert out.get("error") == "bad_request", out

    def duplicate(self):
        rows = [self.row(), self.row(), self.row("p")]
        assert self.call("append", rows=rows)["inserted"] == 3
        assert self.call("append", rows=rows)["inserted"] == 0, "duplicates were stored"
        ids = [r["id"] for r in self.mine()]
        assert all(ids.count(r["id"]) == 1 for r in rows)

    def fidelity(self):
        odd = [self.row(nonce="-" + b64(11), ct="-1234567890123456"),
               self.row(nonce="0000000000000000", ct="1e10" + b64(30)),
               self.row(nonce="-_-_-_-_-_-_-_-_", ct="-" + b64(60))]
        self.call("append", rows=odd)
        stored = {r["id"]: r for r in self.mine()}
        for r in odd:
            got = stored[r["id"]]
            assert (got["nonce"], got["ct"]) == (r["nonce"], r["ct"]), f"Sheets changed {r['ct'][:20]!r} to {got['ct']!r}"

    def privacy(self, token):
        s, p = self.row(), self.row("p")
        self.call("append", rows=[s, p])
        ids = {r["id"] for r in self.dash(token)["rows"]}
        assert s["id"] in ids and p["id"] not in ids
        assert "supersedes" not in json.dumps(self.dash(token)), "supersedes link leaked"

    def versioning(self, token):
        first = self.dash(token)
        assert first.get("ver") is not None, "backend is the pre-review version (no 'ver')"
        assert self.dash(token, first["ver"]).get("unchanged"), "unchanged poll still returned data"
        self.call("append", rows=[self.row("p")])
        again = self.dash(token, first["ver"])
        assert again.get("unchanged"), "a private note changed the employer-visible version"
        assert again.get("last_seen") == first.get("last_seen"), "a private note changed last_seen"
        self.call("append", rows=[self.row()])
        assert not self.dash(token, first["ver"]).get("unchanged"), "a shared row didn't change the version"

    def _retrying(self, fn):
        for attempt in range(6):
            try:
                return fn()
            except BackendError as e:
                if "busy" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(1 + attempt)

    def concurrent_appends(self):
        common = [self.row() for _ in range(20)]
        uniques = [[self.row() for _ in range(5)] for _ in range(8)]
        errors = []

        def worker(i):
            try:
                self._retrying(lambda: self.call("append", rows=common + uniques[i]))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, f"errors: {errors}"
        ids = [r["id"] for r in self.mine()]
        want = [r["id"] for r in common] + [r["id"] for u in uniques for r in u]
        dupes = [i for i in want if ids.count(i) != 1]
        assert not dupes, f"{len(dupes)} ids stored 0 or 2+ times"
        return f"{len(want)} ids, 8 parallel writers"

    def _adjust_op(self, orig_id):
        return {"supersedes": orig_id, "reseal": {"nonce": b64(12), "ct": b64(90)}, "row": self.row()}

    def concurrent_adjust(self, token):
        orig = self.row()
        self.call("append", rows=[orig])
        ops = [self._adjust_op(orig["id"]) for _ in range(4)]
        out = []
        ts = [threading.Thread(target=lambda o=o: out.append(self._retrying(lambda: self.call("adjust", **o)))) for o in ops]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        standing = {o["replacement"] for o in out}
        assert len(standing) == 1, f"replacements disagree: {standing}"
        shared = [r["id"] for r in self.dash(token)["rows"]]
        assert orig["id"] not in shared, "original still shared"
        assert sum(1 for o in ops if o["row"]["id"] in shared) == 1, "not exactly one replacement"

    def adjust_retry(self, token):
        orig = self.row()
        self.call("append", rows=[orig])
        op = self._adjust_op(orig["id"])
        first = self.call("adjust", **op)
        second = self.call("adjust", **op)
        assert second.get("already") and second.get("replacement") == first.get("replacement") == op["row"]["id"]

    def offline_recovery(self):
        keys = Keys(crypto.new_key(), crypto.new_key(), self.auth, crypto.new_token())
        tmp = tempfile.mkdtemp()
        dead = (self.url.rsplit("/", 2)[0] + "/AKfycbDOESNOTEXISTDOESNOTEXIST/exec") if not self.be.url.startswith("http://") \
            else "http://127.0.0.1:9/exec"
        tr = Tracker(Store(Path(tmp) / "c", Path(tmp) / "s"), {"user_id": self.user}, keys,
                     Backend(dead, self.auth, timeout=15, _test_mode=self.be.url.startswith("http://")),
                     type("I", (), {"seconds": lambda s: 1.0})())
        for i in range(5):
            tr.add_note(f"offline {i}")
        assert not tr.sync() and tr.pending() == 5, "events should wait while offline"
        tr.backend = self.be
        assert tr.sync() and tr.pending() == 0, f"didn't drain: {tr.last_sync_error}"
        assert tr.sync()
        return "5 queued events delivered once"

    def revocation(self, token):
        new = crypto.new_token()
        self.call("set_read_token", token=new)
        assert self.dash(token).get("error") == "unauthorized", "old token still works"
        assert self.dash(new).get("ok"), "new token rejected"

    def performance(self, n):
        for i in range(0, n, 100):
            self._retrying(lambda: self.call("append", rows=[self.row() for _ in range(min(100, n - i))]))
        token = crypto.new_token()
        self.call("set_read_token", token=token)
        self.timings = {}
        for _ in range(3):
            self.call("append", rows=[self.row()])
            d = self.dash(token)
            self.dash(token, d["ver"])
            self.call("read")
        med = {k: sorted(v)[len(v) // 2] for k, v in self.timings.items()}
        return "median seconds: " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(med.items()))


def guard(url, auth, harness):
    if harness:
        return
    if not valid_webapp_url(url):
        sys.exit("That isn't an Apps Script /exec URL.")
    try:
        prod = json.loads((config_dir() / "config.json").read_text()).get("webapp_url")
    except (OSError, ValueError):
        prod = None
    if prod and prod.strip() == url.strip():
        sys.exit("REFUSED: that is your real Labtracker backend. Use a separate test deployment.")
    try:
        from labtracker.keystore import Keyring
        real = Keyring().load()
        if real and real.auth == auth:
            sys.exit("REFUSED: the test key equals your real signing key. Run `init` for a separate one.")
    except Exception:  # noqa: BLE001 - keyring unavailable is fine; the URL check still applies
        pass
    if input("Type TEST to confirm this URL is a disposable TEST spreadsheet: ").strip() != "TEST":
        sys.exit("Cancelled.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--perf", type=int, default=0)
    r.add_argument("--harness-key", help=argparse.SUPPRESS)   # local self-test against tests/gas_harness.mjs
    a = ap.parse_args()
    if a.cmd == "init":
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = crypto.new_key()
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, crypto.b64e(key).encode())
        os.close(fd)
        print("Test key saved to", KEY_FILE)
        print("In the TEST Apps Script project only, add Script Property LT_AUTH_KEY =\n")
        print("   ", crypto.b64e(key), "\n")
        return
    harness = bool(a.harness_key)
    auth = crypto.b64d(a.harness_key) if harness else crypto.b64d(KEY_FILE.read_text())
    guard(a.url, auth, harness)
    live = Live(a.url, auth, harness=harness)
    print(f"Test user id: {live.user}\n")
    results = live.run(perf=a.perf)
    failed = [r for r in results if not r["ok"]]
    out = Path(tempfile.gettempdir()) / f"labtracker-live-{int(time.time())}.json"
    out.write_text(json.dumps({"url_suffix": a.url[-12:], "results": results}, indent=1))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed. Report: {out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
