"""Open every tray-app window on a virtual display and save screenshots.
    xvfb-run python3 tests/gui_smoke.py <harness_port> <auth_key_b64> <out_dir>
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from labtracker import crypto, gui  # noqa: E402
from labtracker.backend import Backend  # noqa: E402
from labtracker.keystore import Keys  # noqa: E402
from labtracker.storage import Store  # noqa: E402


class Idle:
    available = True
    value = 1.0

    def seconds(self):
        return self.value


port, auth, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
keys = Keys(crypto.new_key(), crypto.new_key(), crypto.b64d(auth), crypto.new_token())
tmp = Path(tempfile.mkdtemp())
store = Store(tmp / "c", tmp / "s")
cfg = dict(store.load_config(), user_id="u1", name="Test", webapp_url="x")
idle = Idle()
app = gui.App(store, cfg, keys, Backend(f"http://127.0.0.1:{port}/exec", keys.auth, _test_mode=True), idle)
errors = []


def shot(win, name):
    while Gtk.events_pending():
        Gtk.main_iteration()
    w = win.get_window()
    pb = Gdk.pixbuf_get_from_window(w, 0, 0, w.get_width(), w.get_height())
    pb.savev(str(out / f"gui-{name}.png"), "png", [], [])


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        Gtk.main_iteration_do(False)
        time.sleep(0.02)


def script():
    try:
        assert app.tray.kind == "xapp", app.tray.kind
        app.on_clock_in()
        pump(1.5)
        assert app.tracker.state["clocked_in"]
        labels = [m.get_label() for m in app.tray.menu.get_children() if isinstance(m, Gtk.MenuItem) and m.get_label()]
        print("menu:", labels)
        app.tracker.add_note("Reviewed the sample data.")
        # Force an away prompt by pretending 11 idle minutes.
        idle.value = 11 * 60
        app._on_tick()
        assert app.prompt_win is not None
        shot(app.prompt_win, "prompt")
        app.prompt_win._answer("keep")
        idle.value = 1.0
        pump(2)
        fix = gui.FixWindow(app)
        pump(3)
        assert len(fix.store) >= 1, fix.status.get_text()
        fix.view.get_selection().select_path(Gtk.TreePath.new_first())
        shot(fix, "fix")
        fix.destroy()
        notes = gui.NotesWindow(app)
        pump(3)
        buf = notes.view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        assert "Reviewed the sample data." in text, text
        shot(notes, "notes")
        notes.destroy()
        nd = gui.NoteDialog()
        shot(nd, "note-dialog")
        nd.destroy()
        print("GUI smoke test: PASS")
    except Exception as e:  # report and exit non-zero
        import traceback
        traceback.print_exc()
        errors.append(e)
    Gtk.main_quit()
    return False


GLib.timeout_add(500, script)
Gtk.main()
sys.exit(1 if errors else 0)
