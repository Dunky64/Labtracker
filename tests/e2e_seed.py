"""Seed a locally running backend harness with a realistic fortnight of data through the
real agent code (real signing, encryption and HTTP). Prints the dashboard fragment.

    python3 tests/e2e_seed.py <port> <auth_key_b64> <share_key_out_file>
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from labtracker import crypto  # noqa: E402
from labtracker.backend import Backend  # noqa: E402
from labtracker.keystore import Keys  # noqa: E402
from labtracker.storage import Store  # noqa: E402
from labtracker.timeutil import UK  # noqa: E402
from labtracker.tracker import Tracker  # noqa: E402


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class Idle:
    available = True
    value = 1.0

    def seconds(self):
        return self.value


def main():
    port, auth_b64 = sys.argv[1], sys.argv[2]
    keys = Keys(crypto.new_key(), crypto.new_key(), crypto.b64d(auth_b64), crypto.new_token())
    be = Backend(f"http://127.0.0.1:{port}/exec", keys.auth, _test_mode=True)
    be.call("set_read_token", token=keys.read_token)
    tmp = tempfile.mkdtemp()
    clock, idle = Clock(), Idle()
    tr = Tracker(Store(Path(tmp) / "c", Path(tmp) / "s"),
                 {"user_id": "u7f3a", "idle_minutes": 10, "prompt_minutes": 30}, keys, be, idle, clock)

    def at(day, hh, mm):
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=UK).timestamp()

    def run(until, idle_from=None):
        while clock.t < until:
            clock.t += 15
            idle.value = (clock.t - idle_from) if idle_from is not None else 1.0
            tr.tick()

    now = time.time()
    today = datetime.fromtimestamp(now, UK).date()
    adjust_target = None
    for back in range(13, 0, -1):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        clock.t = at(d, 8 + back % 2, 50 + back % 9)
        tr.clock_in()
        run(at(d, 10, 30))
        start_idle = clock.t
        run(at(d, 10, 30) + (12 + back) * 60, idle_from=start_idle)   # coffee
        run(at(d, 12, 30))
        start_idle = clock.t
        run(at(d, 13, 5), idle_from=start_idle)                        # lunch
        if back == 3:                                                  # forgot to clock out
            run(at(d, 16, 40))
            start_idle = clock.t
            run(at(d, 17, 30), idle_from=start_idle)
            assert not tr.state["clocked_in"], "expected auto clock-out"
            tr.sync()
            continue
        run(at(d, 17, 5 + back * 3))
        tr.clock_out()
        if adjust_target is None and back <= 8:
            adjust_target = d
    tr.sync()
    # Correct the clock-out on one day (shown as "adjusted").
    clock.t = now - 3600
    items = tr.fetch()
    ev = [x for x in Tracker.clock_events(items)
          if x["ev"]["type"] == "clock_out"
          and datetime.fromtimestamp(x["ev"]["at"] / 1000, UK).date() == adjust_target][0]
    tr.adjust(items, ev["id"], at(adjust_target, 17, 45), reason="stayed late on a call")

    # Today: in since 09:02, currently away for the last few minutes.
    clock.t = min(at(today, 9, 2), now - 3 * 3600)
    tr.clock_in()
    run(now - 14 * 60)
    start_idle = clock.t
    run(now, idle_from=start_idle)
    tr._last_ping = 0
    tr.sync()
    print(json.dumps({"share": crypto.b64e(keys.share), "token": keys.read_token, "user": "u7f3a"}))


if __name__ == "__main__":
    main()
