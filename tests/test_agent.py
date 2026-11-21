"""python3 -m unittest discover -s tests  (run from the repo root)"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from labtracker import crypto  # noqa: E402
from labtracker.backend import BackendError, valid_webapp_url  # noqa: E402
from labtracker.keystore import Keys  # noqa: E402
from labtracker.storage import Store  # noqa: E402
from labtracker.tracker import Tracker, TrackerError  # noqa: E402

T0 = 1_763_629_200.0  # a Thursday morning


class FakeBackend:
    """Mimics backend/Code.gs semantics in-process."""

    def __init__(self):
        self.rows = []
        self.online = True
        self.calls = []

    def call(self, action, **data):
        self.calls.append(action)
        if not self.online:
            raise BackendError("offline")
        if action == "append":
            ids = {r["id"] for r in self.rows}
            new = [dict(r) for r in data["rows"] if r["id"] not in ids]
            self.rows.extend(new)
            return {"ok": True, "inserted": len(new)}
        if action == "adjust":
            orig = next(r for r in self.rows if r["id"] == data["supersedes"])
            assert orig["key_id"] == "s"
            orig.update(key_id="p", nonce=data["reseal"]["nonce"], ct=data["reseal"]["ct"])
            self.rows.append(dict(data["row"]))
            return {"ok": True}
        if action == "read":
            return {"ok": True, "rows": [dict(r, server_ts=0) for r in self.rows]}
        return {"ok": True}

    def shared(self, keys):
        out = []
        for r in self.rows:
            if r["key_id"] == "s":
                out.append(crypto.unseal(keys.share, r["nonce"], r["ct"], r["user"], r["id"], "s"))
        return sorted(out, key=lambda e: (e["at"], e["seq"]))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Idle:
    available = True

    def __init__(self):
        self.value = 0.0

    def seconds(self):
        return self.value


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "cfg", Path(self.tmp.name) / "st")
        self.keys = Keys.generate()
        self.cfg = {"user_id": "u1", "idle_minutes": 10, "prompt_minutes": 30}
        self.be = FakeBackend()
        self.clock = Clock(T0)
        self.idle = Idle()
        self.tr = self.make()

    def make(self):
        return Tracker(self.store, self.cfg, self.keys, self.be, self.idle, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def advance(self, seconds, idle=None, step=15):
        """Move time forward in 15 s ticks. idle=None means the user is active."""
        signals = set()
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + step)
            if idle is None:
                self.idle.value = 1.0
            else:
                self.idle.value += step
            signals |= self.tr.tick()
        return signals

    def types(self):
        self.tr.sync()
        return [(e["type"], e["at"] / 1000) for e in self.be.shared(self.keys)]


class CryptoTests(unittest.TestCase):
    def test_roundtrip_and_binding(self):
        k = crypto.new_key()
        n, c = crypto.seal(k, {"a": 1}, "u1", "id1", "s")
        self.assertEqual(crypto.unseal(k, n, c, "u1", "id1", "s"), {"a": 1})
        with self.assertRaises(Exception):  # moved to another row -> rejected
            crypto.unseal(k, n, c, "u1", "id2", "s")
        with self.assertRaises(Exception):  # relabelled as private -> rejected
            crypto.unseal(k, n, c, "u1", "id1", "p")
        with self.assertRaises(Exception):  # wrong key
            crypto.unseal(crypto.new_key(), n, c, "u1", "id1", "s")

    def test_recovery_code(self):
        keys = Keys.generate()
        k2, user = Keys.from_recovery_code(keys.recovery_code("u42"))
        self.assertEqual((k2.share, k2.private, k2.auth, k2.read_token, user),
                         (keys.share, keys.private, keys.auth, keys.read_token, "u42"))
        with self.assertRaises(ValueError):
            Keys.from_recovery_code("hello")

    def test_url_validation(self):
        self.assertTrue(valid_webapp_url("https://script.google.com/macros/s/AKfycbx1234567890abcdefXYZ/exec"))
        self.assertFalse(valid_webapp_url("http://script.google.com/macros/s/AKfycbx1234567890abcdefXYZ/exec"))
        self.assertFalse(valid_webapp_url("https://evil.example/macros/s/AKfycbx1234567890abcdefXYZ/exec"))
        self.assertFalse(valid_webapp_url("https://script.google.com/macros/s/AKfy/../x/exec"))


class HardeningTests(unittest.TestCase):
    def test_idle_tool_not_taken_from_path(self):
        from labtracker import idle
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "xprintidle"
            fake.write_text("#!/bin/sh\necho 0\n")
            fake.chmod(0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = f"{d}:{old}"
            try:
                self.assertNotEqual(idle.IdleSource().path, str(fake))
            finally:
                os.environ["PATH"] = old

    def test_dashboard_url_checks(self):
        from labtracker.cli import check_dashboard_url, dashboard_link
        self.assertEqual(check_dashboard_url("https://dunky64.github.io/Labtracker/"), (True, ""))
        self.assertFalse(check_dashboard_url("http://dunky64.github.io/Labtracker/")[0])
        self.assertFalse(check_dashboard_url("https://user:pw@dunky64.github.io/")[0])
        self.assertFalse(check_dashboard_url("javascript:alert(1)")[0])
        ok, warning = check_dashboard_url("https://evil.example/")
        self.assertTrue(ok)
        self.assertIn("not a GitHub Pages site", warning)
        cfg = {"webapp_url": "https://script.google.com/macros/s/X/exec", "user_id": "u1", "name": "T",
               "dashboard_url": "http://evil.example/"}
        with self.assertRaises(ValueError):
            dashboard_link(cfg, Keys.generate())


class TrackerTests(Base):
    def test_clock_in_out(self):
        self.tr.clock_in()
        self.advance(3600)
        self.tr.clock_out()
        self.assertEqual([t for t, _ in self.types()], ["clock_in", "clock_out"])

    def test_files_are_private(self):
        self.tr.clock_in()
        mode = os.stat(self.store.state_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(os.stat(self.store.st_dir).st_mode & 0o777, 0o700)

    def test_idle_then_back(self):
        self.tr.clock_in()
        self.advance(600)                       # active 10 min
        last_active = self.clock.t
        sig = self.advance(15 * 60, idle=True)  # away 15 min
        self.assertIn("prompt", sig)
        self.assertEqual(self.tr.state["prompt"]["kind"], "idle")
        sig = self.advance(60)                  # back
        self.assertIn("resumed", sig)
        self.assertFalse(self.tr.state["prompt"]["auto"])
        ev = self.types()
        self.assertEqual([t for t, _ in ev], ["clock_in", "idle_start", "idle_end"])
        self.assertAlmostEqual(ev[1][1], last_active, delta=16)  # backdated to last input
        self.advance(3600)                      # no auto clock-out after coming back
        self.assertTrue(self.tr.state["clocked_in"])

    def test_auto_clock_out_after_no_answer(self):
        self.tr.clock_in()
        self.advance(1800)
        last_active = self.clock.t
        sig = self.advance(45 * 60, idle=True)
        self.assertIn("auto_out", sig)
        self.assertFalse(self.tr.state["clocked_in"])
        ev = self.types()
        self.assertEqual(ev[-1][0], "auto_clock_out")
        self.assertAlmostEqual(ev[-1][1], last_active, delta=16)  # at the time last active

    def test_prompt_answer_clock_out(self):
        self.tr.clock_in()
        self.advance(600)
        last_active = self.clock.t
        self.advance(11 * 60, idle=True)
        self.tr.respond_prompt("clock_out")
        ev = self.types()
        self.assertEqual(ev[-1][0], "clock_out")
        self.assertAlmostEqual(ev[-1][1], last_active, delta=16)

    def test_sleep_gap_needs_answer(self):
        self.tr.clock_in()
        self.advance(600)
        before = self.clock.t
        self.clock.t += 8 * 3600                 # laptop asleep overnight
        self.idle.value = 2.0                    # user is active right after waking
        sig = self.tr.tick()
        self.assertIn("prompt", sig)
        self.assertEqual(self.tr.state["prompt"]["kind"], "gap")
        self.advance(300)                        # activity doesn't silently resolve a gap
        self.assertIsNotNone(self.tr.state["prompt"])
        self.tr.respond_prompt("clock_out_and_in")
        ev = self.types()
        self.assertEqual([t for t, _ in ev], ["clock_in", "idle_start", "clock_out", "clock_in"])
        self.assertAlmostEqual(ev[2][1], before, delta=1)

    def test_sleep_gap_auto(self):
        self.tr.clock_in()
        self.advance(600)
        before = self.clock.t
        self.clock.t += 3 * 3600
        self.tr.tick()
        sig = self.advance(31 * 60)              # active but never answers
        self.assertIn("auto_out", sig)
        self.assertAlmostEqual(self.types()[-1][1], before, delta=1)

    def test_clock_in_right_after_wake(self):
        self.advance(60)
        self.clock.t += 14 * 3600            # asleep overnight while clocked OUT
        self.tr.clock_in()                   # clicked before the first tick after waking
        sig = self.advance(120)
        self.assertNotIn("prompt", sig)
        self.assertTrue(self.tr.state["clocked_in"])

    def test_restart_keeps_state(self):
        self.tr.clock_in()
        self.advance(120)
        tr2 = self.make()
        self.assertTrue(tr2.state["clocked_in"])

    def test_offline_queue(self):
        self.be.online = False
        self.tr.clock_in()
        self.assertFalse(self.tr.sync())
        self.assertEqual(self.tr.pending(), 1)
        self.be.online = True
        self.assertTrue(self.tr.sync())
        self.assertEqual(self.tr.pending(), 0)
        self.assertTrue(self.tr.sync())
        self.assertEqual(len(self.be.rows), 1)  # no duplicates on retry

    def test_queue_is_encrypted(self):
        self.be.online = False
        self.tr.clock_in()
        self.tr.add_note("secret project codename")
        raw = self.store.queue_path.read_text()
        self.assertNotIn("secret", raw)
        self.assertNotIn("clock_in", raw)

    def test_notes_are_private(self):
        self.tr.clock_in()
        self.tr.add_note("Worked on report")
        self.tr.sync()
        self.assertEqual([t for t, _ in self.types()], ["clock_in"])
        notes = Tracker.notes(self.tr.fetch())
        self.assertEqual(notes[0]["ev"]["text"], "Worked on report")
        with self.assertRaises(TrackerError):
            self.tr.add_note("   ")

    def test_adjust_hides_original(self):
        self.tr.clock_in()
        self.advance(8 * 3600)
        self.tr.clock_out()
        items = self.tr.fetch()
        out = Tracker.clock_events(items)[-1]
        new_at = T0 + 7 * 3600
        self.tr.adjust(items, out["id"], new_at, reason="forgot")
        ev = self.be.shared(self.keys)
        self.assertEqual([e["type"] for e in ev], ["clock_in", "clock_out"])
        self.assertTrue(ev[-1]["adjusted"])
        self.assertAlmostEqual(ev[-1]["at"] / 1000, new_at, delta=0.01)
        priv = [x for x in self.tr.fetch() if x["key_id"] == "p"]
        self.assertEqual(len(priv), 2)  # the resealed original + the automatic private note
        self.assertIn("forgot", Tracker.notes(self.tr.fetch())[0]["ev"]["text"])

    def test_duplicated_rows_ignored(self):
        self.tr.clock_in()
        self.tr.sync()
        self.be.rows.append(dict(self.be.rows[0]))   # someone copies a row inside the Sheet
        self.assertEqual(len(Tracker.clock_events(self.tr.fetch())), 1)

    def test_adjust_validation(self):
        self.tr.clock_in()
        self.advance(3600)
        self.tr.clock_out()
        items = self.tr.fetch()
        cin, cout = Tracker.clock_events(items)
        with self.assertRaises(TrackerError):
            self.tr.prepare_adjust(items, cout["id"], T0 - 60)            # before its clock-in
        with self.assertRaises(TrackerError):
            self.tr.prepare_adjust(items, cin["id"], T0 + 7200)           # after its clock-out
        with self.assertRaises(TrackerError):
            self.tr.prepare_adjust(items, cout["id"], self.clock.t + 60)  # future

    def test_auto_out_can_be_fixed(self):
        self.tr.clock_in()
        self.advance(600)
        self.advance(45 * 60, idle=True)
        items = self.tr.fetch()
        auto = Tracker.clock_events(items)[-1]
        self.assertEqual(auto["ev"]["type"], "auto_clock_out")
        self.tr.adjust(items, auto["id"], T0 + 900)
        self.assertEqual(self.be.shared(self.keys)[-1]["type"], "clock_out")

    def test_clock_out_earlier(self):
        self.tr.clock_in()
        self.advance(5 * 3600)
        with self.assertRaises(TrackerError):
            self.tr.clock_out_earlier(T0 - 1)
        self.tr.clock_out_earlier(T0 + 3600)
        ev = self.be.shared(self.keys) if self.tr.sync() else []
        self.assertTrue(ev[-1]["adjusted"])
        self.assertFalse(self.tr.state["clocked_in"])


if __name__ == "__main__":
    unittest.main()
