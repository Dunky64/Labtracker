"""Reliability and concurrency tests: the real agent code talking HTTP to the local Apps Script harness.

    python3 -m unittest tests.test_reliability     (needs Node 18+)

Honest scope: the harness is single-threaded JavaScript, so these tests exercise concurrent
*clients* and real HTTP retries, not concurrent Apps Script executions. Server-side concurrency
under real LockService must be verified against a live test deployment (tests/live_integration.py).
"""
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from labtracker import crypto  # noqa: E402
from labtracker.backend import Backend, BackendError  # noqa: E402
from labtracker.keystore import Keys  # noqa: E402
from labtracker.storage import Store  # noqa: E402
from labtracker.tracker import Tracker  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self):
        self.auth = secrets.token_bytes(32)
        self.port = free_port()
        self.proc = subprocess.Popen(
            ["node", str(ROOT / "tests" / "gas_harness.mjs"), str(self.port),
             base64.urlsafe_b64encode(self.auth).decode()],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("harness did not start")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/exec"

    def sheet(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/__sheet") as r:
            return json.loads(r.read())[1:]

    def drop_next_response(self):
        urllib.request.urlopen(f"http://127.0.0.1:{self.port}/__drop_next_response").read()

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class Idle:
    available = True

    def seconds(self):
        return 1.0


class ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def tracker(self, keys, user):
        tmp = tempfile.mkdtemp()
        be = Backend(self.h.url, keys.auth, timeout=10, _test_mode=True)
        return Tracker(Store(Path(tmp) / "c", Path(tmp) / "s"),
                       {"user_id": user, "idle_minutes": 10, "prompt_minutes": 30},
                       keys, be, Idle(), clock=Clock())

    def keys(self):
        return Keys(crypto.new_key(), crypto.new_key(), self.h.auth, crypto.new_token())

    def ids_for(self, user):
        return [r[1] for r in self.h.sheet() if r[2] == user]

    def test_lost_response_after_commit_is_not_duplicated(self):
        tr = self.tracker(self.keys(), "lost1")
        tr.clock_in()
        self.h.drop_next_response()          # the server commits, the client sees a network error
        self.assertFalse(tr.sync())
        self.assertEqual(tr.pending(), 1)
        self.assertTrue(tr.sync())           # retry
        self.assertEqual(tr.pending(), 0)
        self.assertEqual(len(self.ids_for("lost1")), 1)

    def test_two_desktop_clients_syncing_concurrently(self):
        keys = self.keys()
        a, b = self.tracker(keys, "dual"), self.tracker(keys, "dual")
        for i in range(40):
            (a if i % 2 else b).add_note(f"note {i}")
        errors = []

        def hammer(tr):
            for _ in range(5):
                try:
                    tr.sync()
                except Exception as e:  # noqa: BLE001 - surface anything unexpected
                    errors.append(e)

        threads = [threading.Thread(target=hammer, args=(t,)) for t in (a, b, a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = self.ids_for("dual")
        self.assertEqual(len(ids), 40, "every event stored")
        self.assertEqual(len(set(ids)), 40, "no event stored twice")

    def test_large_offline_backlog_is_not_stuck(self):
        tr = self.tracker(self.keys(), "backlog")
        for i in range(100):                 # 100 long notes written while offline
            tr.add_note(("long note %d " % i) * 150)
        self.assertTrue(tr.sync(), f"sync failed: {getattr(tr, 'last_sync_error', None)}")
        self.assertEqual(tr.pending(), 0)
        self.assertEqual(len(self.ids_for("backlog")), 100)

    def test_sync_error_is_reported_then_cleared(self):
        keys = self.keys()
        tr = self.tracker(keys, "errs")
        tr.backend = Backend(self.h.url, crypto.new_key(), timeout=10, _test_mode=True)  # wrong key
        tr.clock_in()
        self.assertFalse(tr.sync())
        self.assertIn("doesn't match", tr.last_sync_error or "")
        tr.backend = Backend(self.h.url, keys.auth, timeout=10, _test_mode=True)
        self.assertTrue(tr.sync())
        self.assertIsNone(tr.last_sync_error)

    def test_correction_retry_after_network_failure_reuses_the_same_operation(self):
        tr = self.tracker(self.keys(), "fix1")
        tr.clock_in()
        tr.clock.t += 3600
        tr.clock_out()
        items = tr.fetch()
        out = Tracker.clock_events(items)[-1]
        new_at = tr.clock.t - 600
        op1 = tr.prepare_adjust(items, out["id"], new_at, "")
        op2 = tr.prepare_adjust(items, out["id"], new_at, "")   # user clicks Save again
        self.assertEqual(op1["row"]["id"], op2["row"]["id"])


if __name__ == "__main__":
    unittest.main()
