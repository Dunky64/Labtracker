"""Core clock-in/out and idle logic. No GUI code here, so it can be tested directly."""
import threading
import time
import uuid

from . import crypto
from .backend import BackendError

SHARED, PRIVATE = "s", "p"
CLOCK_TYPES = ("clock_in", "clock_out", "auto_clock_out")
SLEEP_GAP_S = 90          # a tick this late means the computer was asleep or off
PING_EVERY_S = 300
MAX_NOTE = 2000

DEFAULT_STATE = {
    "clocked_in": False,
    "session_start": None,   # epoch seconds
    "idle_since": None,      # epoch seconds, while an away period is open
    "prompt": None,          # {"kind": "idle"|"gap", "since": s, "shown_at": s, "auto": bool}
    "last_tick": None,
    "seq": 0,
}


class TrackerError(RuntimeError):
    pass


class Tracker:
    def __init__(self, store, cfg, keys, backend, idle_source, clock=time.time):
        self.store = store
        self.user = cfg["user_id"]
        self.idle_threshold = float(cfg.get("idle_minutes", 10)) * 60
        self.prompt_timeout = float(cfg.get("prompt_minutes", 30)) * 60
        self.keys = keys
        self.backend = backend
        self.idle = idle_source
        self.clock = clock
        self.state = dict(DEFAULT_STATE)
        self.state.update(store.load_state())
        self._qlock = threading.Lock()
        self._last_ping = 0.0

    # ------------------------------------------------------------ events --
    def _seal_event(self, etype, at, key_id=SHARED, **extra):
        self.state["seq"] += 1
        ev = {"v": 1, "type": etype, "at": int(round(at * 1000)), "seq": self.state["seq"]}
        ev.update(extra)
        row_id = str(uuid.uuid4())
        key = self.keys.share if key_id == SHARED else self.keys.private
        nonce, ct = crypto.seal(key, ev, self.user, row_id, key_id)
        return {"id": row_id, "user": self.user, "key_id": key_id, "nonce": nonce, "ct": ct}

    def _emit(self, etype, at, key_id=SHARED, **extra):
        row = self._seal_event(etype, at, key_id, **extra)
        with self._qlock:
            q = self.store.load_queue()
            q.append(row)
            self.store.save_queue(q)
        self._save()
        return row

    def _save(self):
        self.store.save_state(self.state)

    def pending(self) -> int:
        with self._qlock:
            return len(self.store.load_queue())

    def sync(self) -> bool:
        """Send queued events (safe to call from a worker thread). Returns True if fully synced."""
        with self._qlock:
            snapshot = list(self.store.load_queue())
        try:
            for i in range(0, len(snapshot), 100):
                chunk = snapshot[i:i + 100]
                self.backend.call("append", rows=chunk)
                sent = {r["id"] for r in chunk}
                with self._qlock:
                    self.store.save_queue([r for r in self.store.load_queue() if r["id"] not in sent])
            now = self.clock()
            if self.state.get("clocked_in") and now - self._last_ping >= PING_EVERY_S:
                self.backend.call("ping")
                self._last_ping = now
            return True
        except BackendError:
            return False

    # ----------------------------------------------------------- actions --
    def clock_in(self):
        if self.state["clocked_in"]:
            return
        now = self.clock()
        # Reset the sleep detector: time before clocking in never counts as a gap in the shift.
        self.state.update(clocked_in=True, session_start=now, idle_since=None, prompt=None, last_tick=now)
        self._emit("clock_in", now)
        self._last_ping = now  # the append itself counts as a ping

    def clock_out(self, at=None, adjusted=False, via=None):
        if not self.state["clocked_in"]:
            return
        now = self.clock()
        at = now if at is None else at
        extra = {}
        if adjusted:
            extra["adjusted"] = True
        if via:
            extra["via"] = via
        self.state.update(clocked_in=False, session_start=None, idle_since=None, prompt=None)
        self._emit("clock_out", at, **extra)

    def clock_out_earlier(self, at):
        """Forgot to clock out but still marked as in: clock out at an earlier time (shown as adjusted)."""
        if not self.state["clocked_in"]:
            raise TrackerError("You're not clocked in.")
        now = self.clock()
        if not (self.state["session_start"] < at <= now):
            raise TrackerError("The time must be after you clocked in and not in the future.")
        self.clock_out(at=at, adjusted=True)

    def add_note(self, text: str):
        text = (text or "").strip()
        if not text:
            raise TrackerError("The note is empty.")
        if len(text) > MAX_NOTE:
            raise TrackerError(f"Notes are limited to {MAX_NOTE} characters.")
        self._emit("note", self.clock(), key_id=PRIVATE, text=text)

    def respond_prompt(self, choice: str):
        """choice: 'keep' | 'clock_out' | 'clock_out_and_in'."""
        p = self.state.get("prompt")
        if not p:
            return
        now = self.clock()
        if choice == "keep":
            if self.state["idle_since"] is not None:
                self._emit("idle_end", now)
                self.state["idle_since"] = None
            self.state["prompt"] = None
            self._save()
        elif choice in ("clock_out", "clock_out_and_in"):
            since = p["since"]
            self.clock_out(at=since, via="prompt")
            if choice == "clock_out_and_in":
                self.clock_in()
        else:
            raise ValueError(choice)

    # -------------------------------------------------------------- tick --
    def tick(self):
        """Call every ~15 s. Returns a set of signals for the GUI:
        'prompt' (show the forgot-to-clock-out question), 'resumed', 'auto_out'."""
        st = self.state
        signals = set()
        now = self.clock()
        last_tick = st.get("last_tick") or now
        gap = now - last_tick
        st["last_tick"] = now

        idle_s = self.idle.seconds()
        last_active = now - (idle_s if idle_s is not None else 0.0)
        if gap > SLEEP_GAP_S:
            # Asleep/off: you can't have been active after the last tick we saw.
            last_active = min(last_active, last_tick)
        hard_gap = gap >= self.idle_threshold

        if st["clocked_in"]:
            if st["idle_since"] is None and now - last_active >= self.idle_threshold:
                since = max(last_active, st["session_start"])
                st["idle_since"] = since
                st["prompt"] = {"kind": "gap" if hard_gap else "idle", "since": since,
                                "shown_at": now, "auto": True}
                self._emit("idle_start", since)
                signals.add("prompt")
            elif st["idle_since"] is not None:
                p = st.get("prompt")
                gap_prompt = bool(p and p["kind"] == "gap")
                active_again = now - last_active < self.idle_threshold and last_active > st["idle_since"]
                if active_again and not gap_prompt:
                    self._emit("idle_end", last_active)
                    st["idle_since"] = None
                    if p:
                        p["auto"] = False   # keep the question visible, but stop the countdown
                    signals.add("resumed")
            p = st.get("prompt")
            if st["clocked_in"] and p and p.get("auto") and now - p["shown_at"] >= self.prompt_timeout:
                since = p["since"]
                st.update(clocked_in=False, session_start=None, idle_since=None, prompt=None)
                self._emit("auto_clock_out", since)
                signals.add("auto_out")
        self._save()
        return signals

    # ------------------------------------------------- history & fixing --
    def fetch(self):
        """Download and decrypt all of your rows. Returns list of dicts sorted by time."""
        if not self.sync():
            raise TrackerError("Couldn't sync with Google. Check your connection and try again.")
        try:
            res = self.backend.call("read")
        except BackendError as e:
            raise TrackerError(str(e)) from e
        out, seen = [], set()
        for r in res.get("rows", []):
            if r.get("user") != self.user or r.get("id") in seen:
                continue   # a row copied inside the Sheet must not count twice
            seen.add(r.get("id"))
            key = self.keys.share if r["key_id"] == SHARED else self.keys.private
            try:
                ev = crypto.unseal(key, r["nonce"], r["ct"], r["user"], r["id"], r["key_id"])
            except Exception:
                continue
            out.append({"id": r["id"], "key_id": r["key_id"], "server_ts": r.get("server_ts"), "ev": ev})
        out.sort(key=lambda x: (x["ev"]["at"], x["ev"]["seq"]))
        return out

    @staticmethod
    def clock_events(items):
        return [x for x in items if x["key_id"] == SHARED and x["ev"]["type"] in CLOCK_TYPES]

    @staticmethod
    def notes(items):
        return [x for x in items if x["key_id"] == PRIVATE and x["ev"]["type"] == "note"]

    def prepare_adjust(self, items, event_id, new_at, reason=""):
        """Validate and encrypt a correction (main thread). Returns an op for send_adjust()."""
        clocks = self.clock_events(items)
        idx = next((i for i, x in enumerate(clocks) if x["id"] == event_id), None)
        if idx is None:
            raise TrackerError("That entry can't be found (was it already changed?).")
        orig = clocks[idx]
        now = self.clock()
        new_ms = int(round(new_at * 1000))
        prev_ms = clocks[idx - 1]["ev"]["at"] if idx > 0 else None
        nxt_ms = clocks[idx + 1]["ev"]["at"] if idx + 1 < len(clocks) else int(now * 1000) + 1
        if new_at > now:
            raise TrackerError("The new time can't be in the future.")
        if (prev_ms is not None and new_ms <= prev_ms) or new_ms >= nxt_ms:
            raise TrackerError("The new time must stay between the entries before and after it.")

        ev = dict(orig["ev"])
        etype = "clock_out" if ev["type"] == "auto_clock_out" else ev["type"]
        row = self._seal_event(etype, new_at, adjusted=True)
        rn, rc = crypto.seal(self.keys.private, ev, self.user, event_id, PRIVATE)
        self._save()
        return {"supersedes": event_id, "reseal": {"nonce": rn, "ct": rc}, "row": row,
                "orig": ev, "etype": etype, "new_at": new_at, "reason": reason}

    def send_adjust(self, op):
        """Network part (safe in a worker thread)."""
        try:
            self.backend.call("adjust", supersedes=op["supersedes"], reseal=op["reseal"], row=op["row"])
        except BackendError as e:
            raise TrackerError(str(e)) from e

    def finish_adjust(self, op):
        """Bookkeeping after a successful send (main thread)."""
        from .timeutil import fmt_dt
        orig, etype, new_at = op["orig"], op["etype"], op["new_at"]
        if (self.state["clocked_in"] and etype == "clock_in"
                and abs((self.state["session_start"] or 0) * 1000 - orig["at"]) < 1000):
            self.state["session_start"] = new_at
            self._save()
        note = f"Adjusted {etype.replace('_', ' ')} from {fmt_dt(orig['at'] / 1000)} to {fmt_dt(new_at)}."
        if op["reason"].strip():
            note += " Reason: " + op["reason"].strip()
        self.add_note(note[:MAX_NOTE])

    def adjust(self, items, event_id, new_at, reason=""):
        op = self.prepare_adjust(items, event_id, new_at, reason)
        self.send_adjust(op)
        self.finish_adjust(op)
        self.sync()
