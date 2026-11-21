"""Tray app for Linux Mint (Cinnamon). Uses XApp.StatusIcon, with fallbacks for other desktops."""
import fcntl
import os
import sys
import threading
import time
import webbrowser

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .backend import Backend, BackendError  # noqa: E402
from .cli import dashboard_link  # noqa: E402
from .idle import IdleSource  # noqa: E402
from .keystore import Keyring, KeyringError  # noqa: E402
from .storage import Store, ensure_private_dir  # noqa: E402
from .timeutil import fmt_dt, fmt_time, local_date, parse_local  # noqa: E402
from .tracker import Tracker, TrackerError  # noqa: E402

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")
TICK_S = 15
LABELS = {"clock_in": "Clocked in", "clock_out": "Clocked out", "auto_clock_out": "Auto clock-out"}


def icon_path(name):
    return os.path.join(ICON_DIR, f"labtracker-{name}.svg")


def _dur(seconds):
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


class TrayIcon:
    """Small wrapper so the rest of the code doesn't care which tray API is available."""

    def __init__(self, menu_builder):
        self.menu_builder = menu_builder
        self.kind = None
        try:
            gi.require_version("XApp", "1.0")
            from gi.repository import XApp
            self.icon = XApp.StatusIcon()
            self.icon.set_name("labtracker")
            self.kind = "xapp"
        except (ValueError, ImportError):
            try:
                gi.require_version("AyatanaAppIndicator3", "0.1")
                from gi.repository import AyatanaAppIndicator3 as AI
                self.icon = AI.Indicator.new("labtracker", icon_path("out"),
                                             AI.IndicatorCategory.APPLICATION_STATUS)
                self.icon.set_status(AI.IndicatorStatus.ACTIVE)
                self.kind = "ayatana"
            except (ValueError, ImportError):
                self.icon = Gtk.StatusIcon()
                self.icon.connect("popup-menu", self._popup)
                self.icon.connect("activate", lambda *_: self._popup(None, 1, Gtk.get_current_event_time()))
                self.kind = "gtk"

    def _popup(self, _icon, button, t):
        self.menu.popup(None, None, None, None, button, t)

    def update(self, state_name, tooltip):
        self.menu = self.menu_builder()
        path = icon_path(state_name)
        if self.kind == "xapp":
            self.icon.set_icon_name(path)
            self.icon.set_tooltip_text(tooltip)
            self.icon.set_primary_menu(self.menu)
            self.icon.set_secondary_menu(self.menu)
        elif self.kind == "ayatana":
            self.icon.set_icon_full(path, tooltip)
            self.icon.set_menu(self.menu)
        else:
            self.icon.set_from_file(path)
            self.icon.set_tooltip_text(tooltip)


class App:
    def __init__(self, store, cfg, keys, backend, idle):
        self.store, self.cfg, self.keys = store, cfg, keys
        self.tracker = Tracker(store, cfg, keys, backend, idle)
        self.idle_available = idle.available
        self.synced = True
        self._sync_running = False
        self.prompt_win = None
        self.tray = TrayIcon(self._build_menu)
        self.refresh()
        GLib.idle_add(lambda: (self._on_tick(), False)[1])   # once now (returning False stops repeats)
        GLib.timeout_add_seconds(TICK_S, self._on_tick)

    # ------------------------------------------------------------ helpers --
    def bg(self, work, done):
        """Run `work()` in a thread, then `done(result, error)` back on the GTK thread."""
        def runner():
            try:
                res, err = work(), None
            except Exception as e:  # shown to the user, never silently dropped
                res, err = None, e
            GLib.idle_add(lambda: (done(res, err), False)[1])
        threading.Thread(target=runner, daemon=True).start()

    def kick_sync(self):
        if self._sync_running:
            return
        self._sync_running = True

        def done(ok, err):
            self._sync_running = False
            self.synced = bool(ok) and not err
            self.refresh()
        self.bg(self.tracker.sync, done)

    def error(self, text, parent=None):
        d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.ERROR,
                              buttons=Gtk.ButtonsType.OK, text=str(text))
        d.run()
        d.destroy()

    def info(self, text, parent=None):
        d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=str(text))
        d.run()
        d.destroy()

    # -------------------------------------------------------------- state --
    def status_text(self):
        st = self.tracker.state
        if not st["clocked_in"]:
            return "Clocked out"
        if st.get("idle_since"):
            return f"Away since {fmt_time(st['idle_since'])}"
        return f"Clocked in since {fmt_time(st['session_start'])} ({_dur(time.time() - st['session_start'])})"

    def refresh(self):
        st = self.tracker.state
        name = "out" if not st["clocked_in"] else ("away" if st.get("idle_since") else "in")
        self.tray.update(name, "Labtracker: " + self.status_text())

    def _build_menu(self):
        st = self.tracker.state
        menu = Gtk.Menu()

        def item(label, cb=None, sensitive=True):
            mi = Gtk.MenuItem(label=label)
            mi.set_sensitive(sensitive and cb is not None)
            if cb:
                mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        item(self.status_text())
        menu.append(Gtk.SeparatorMenuItem())
        if st["clocked_in"]:
            item("Clock out", self.on_clock_out)
            item("Clock out at an earlier time…", self.on_clock_out_earlier)
        else:
            item("Clock in", self.on_clock_in)
        item("Add a note…", self.on_note)
        item("Fix an entry…", self.on_fix)
        item("My notes…", self.on_view_notes)
        menu.append(Gtk.SeparatorMenuItem())
        item("Copy employer dashboard link", self.on_copy_link)
        item("Open employer dashboard", self.on_open_dashboard)
        menu.append(Gtk.SeparatorMenuItem())
        pending = self.tracker.pending()
        if pending:
            item(f"{pending} event(s) waiting to sync. Retry now", self.kick_sync)
        else:
            item("All changes synced")
        if not self.idle_available:
            item("Idle detection unavailable (install xprintidle)")
        item("Quit", self.on_quit)
        menu.show_all()
        return menu

    # -------------------------------------------------------------- ticks --
    def _on_tick(self):
        try:
            signals = self.tracker.tick()
        except Exception as e:  # keep the timer alive no matter what
            print(f"labtracker: tick error: {type(e).__name__}", file=sys.stderr)
            return True
        if "prompt" in signals:
            self.show_prompt()
        if "resumed" in signals and self.prompt_win:
            self.prompt_win.update()
        if "auto_out" in signals:
            self.close_prompt()
            self.info_later("You were clocked out automatically at the time you were last active.")
        if self.prompt_win:
            self.prompt_win.update()
        self.refresh()
        self.kick_sync()
        return True

    def info_later(self, text):
        GLib.idle_add(lambda: (self.info(text), False)[1])

    # ------------------------------------------------------------ actions --
    def on_clock_in(self):
        self.tracker.clock_in()
        self.refresh()
        self.kick_sync()

    def on_clock_out(self):
        self.tracker.clock_out()
        self.close_prompt()
        self.refresh()
        self.kick_sync()

    def on_clock_out_earlier(self):
        st = self.tracker.state
        default = st.get("idle_since") or time.time()
        dlg = TimeDialog("Clock out at an earlier time",
                         "Use this if you forgot to clock out. Your employer will see an "
                         "\"adjusted\" marker on this entry.", default)
        while dlg.run() == Gtk.ResponseType.OK:
            try:
                self.tracker.clock_out_earlier(dlg.value())
                break
            except (TrackerError, ValueError) as e:
                self.error(e, dlg)
        dlg.destroy()
        self.close_prompt()
        self.refresh()
        self.kick_sync()

    def on_note(self):
        dlg = NoteDialog()
        while dlg.run() == Gtk.ResponseType.OK:
            try:
                self.tracker.add_note(dlg.text())
                break
            except TrackerError as e:
                self.error(e, dlg)
        dlg.destroy()
        self.kick_sync()

    def on_fix(self):
        FixWindow(self)

    def on_view_notes(self):
        NotesWindow(self)

    def on_copy_link(self):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(dashboard_link(self.cfg, self.keys), -1)
        cb.store()
        self.info("Dashboard link copied. Send it to your employer privately. "
                  "Anyone with this link can see your timeline (not your notes).")

    def on_open_dashboard(self):
        webbrowser.open(dashboard_link(self.cfg, self.keys))

    def on_quit(self):
        if self.tracker.state["clocked_in"]:
            d = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO,
                                  text="You're still clocked in. Quit anyway?\n\n"
                                       "Idle tracking stops while Labtracker isn't running.")
            r = d.run()
            d.destroy()
            if r != Gtk.ResponseType.YES:
                return
        Gtk.main_quit()

    # ------------------------------------------------------------- prompt --
    def show_prompt(self):
        if self.prompt_win:
            self.prompt_win.update()
            self.prompt_win.present()
            return
        self.prompt_win = PromptWindow(self)

    def close_prompt(self):
        if self.prompt_win:
            self.prompt_win.destroy()
            self.prompt_win = None


# ====================================================================== windows

class PromptWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Labtracker: did you forget to clock out?")
        self.app = app
        self.set_keep_above(True)
        self.set_urgency_hint(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(18)
        self.set_resizable(False)
        self.connect("delete-event", lambda *_: True)  # must be answered
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(self.box)
        self.label = Gtk.Label(xalign=0)
        self.label.set_line_wrap(True)
        self.label.set_max_width_chars(52)
        self.box.pack_start(self.label, False, False, 0)
        self.buttons = Gtk.Box(spacing=8)
        self.box.pack_start(self.buttons, False, False, 0)
        self._kind = None
        self.update()
        self.show_all()
        self.present()

    def _answer(self, choice):
        self.app.tracker.respond_prompt(choice)
        self.app.close_prompt()
        self.app.refresh()
        self.app.kick_sync()

    def update(self):
        p = self.app.tracker.state.get("prompt")
        if not p:
            self.app.close_prompt()
            return
        t = fmt_time(p["since"])
        left = max(0, self.app.tracker.prompt_timeout - (time.time() - p["shown_at"]))
        if p["kind"] == "gap":
            text = (f"Your computer was asleep or switched off from {t} while you were clocked in.\n\n"
                    f"If you don't answer, you'll be clocked out at {t} in {_dur(left)}.")
            choices = [(f"Clock out at {t}", "clock_out"),
                       (f"Clock out at {t} and clock in now", "clock_out_and_in"),
                       ("I was working the whole time", "keep")]
        elif p.get("auto"):
            text = (f"No keyboard or mouse activity since {t}. Did you forget to clock out?\n\n"
                    f"If you don't answer, you'll be clocked out at {t} in {_dur(left)}.")
            choices = [("I'm still working", "keep"), (f"Clock me out at {t}", "clock_out")]
        else:
            text = (f"Welcome back. You were away from {t}.\n"
                    "Your timeline shows this as time away; it isn't taken off your hours.")
            choices = [("OK, I'm still working", "keep"), (f"Clock me out at {t}", "clock_out")]
        self.label.set_text(text)
        if self._kind != (p["kind"], p.get("auto")):
            self._kind = (p["kind"], p.get("auto"))
            for c in self.buttons.get_children():
                c.destroy()
            for label, choice in choices:
                b = Gtk.Button(label=label)
                b.connect("clicked", lambda _b, c=choice: self._answer(c))
                self.buttons.pack_start(b, False, False, 0)
            self.buttons.show_all()


class TimeDialog(Gtk.Dialog):
    def __init__(self, title, explanation, default_ts, parent=None):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(14)
        lab = Gtk.Label(label=explanation, xalign=0)
        lab.set_line_wrap(True)
        lab.set_max_width_chars(50)
        box.pack_start(lab, False, False, 0)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.date = Gtk.Entry(text=local_date(default_ts), activates_default=True)
        self.time = Gtk.Entry(text=fmt_time(default_ts), activates_default=True)
        grid.attach(Gtk.Label(label="Date (YYYY-MM-DD)", xalign=0), 0, 0, 1, 1)
        grid.attach(self.date, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Time (HH:MM, UK)", xalign=0), 0, 1, 1, 1)
        grid.attach(self.time, 1, 1, 1, 1)
        box.pack_start(grid, False, False, 0)
        self.show_all()

    def value(self):
        try:
            return parse_local(self.date.get_text(), self.time.get_text())
        except ValueError:
            raise ValueError("Please enter the date as YYYY-MM-DD and the time as HH:MM.")


class NoteDialog(Gtk.Dialog):
    def __init__(self):
        super().__init__(title="Add a private note", modal=True)
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save note", Gtk.ResponseType.OK)
        self.set_default_size(460, 260)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(14)
        lab = Gtk.Label(label="Notes are encrypted with your private key. Your employer can't read them.",
                        xalign=0)
        lab.set_line_wrap(True)
        box.pack_start(lab, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_shadow_type(Gtk.ShadowType.IN)
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        sw.add(self.view)
        box.pack_start(sw, True, True, 0)
        self.show_all()

    def text(self):
        b = self.view.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)


class FixWindow(Gtk.Window):
    """Lists recent clock-ins/outs and lets you correct one."""

    def __init__(self, app):
        super().__init__(title="Labtracker: fix an entry")
        self.app = app
        self.items = []
        self.set_default_size(560, 460)
        self.set_border_width(14)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)
        lab = Gtk.Label(xalign=0, label="Pick an entry and enter the correct time. Your employer only "
                                         "sees an \"adjusted\" marker. The original time and your "
                                         "reason stay private.")
        lab.set_line_wrap(True)
        box.pack_start(lab, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str)  # id, when, what, flag
        self.view = Gtk.TreeView(model=self.store)
        for i, title in ((1, "When"), (2, "Entry"), (3, "")):
            self.view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        self.view.get_selection().connect("changed", self._on_select)
        sw = Gtk.ScrolledWindow()
        sw.set_shadow_type(Gtk.ShadowType.IN)
        sw.add(self.view)
        box.pack_start(sw, True, True, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.date = Gtk.Entry(placeholder_text="YYYY-MM-DD")
        self.time = Gtk.Entry(placeholder_text="HH:MM")
        self.reason = Gtk.Entry(placeholder_text="Optional, private")
        for row, (label, w) in enumerate((("New date", self.date), ("New time (UK)", self.time),
                                          ("Reason", self.reason))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            w.set_hexpand(True)
            grid.attach(w, 1, row, 1, 1)
        box.pack_start(grid, False, False, 0)

        bar = Gtk.Box(spacing=8)
        self.status = Gtk.Label(label="Loading your entries…", xalign=0)
        bar.pack_start(self.status, True, True, 0)
        self.save_btn = Gtk.Button(label="Save change")
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save)
        bar.pack_end(self.save_btn, False, False, 0)
        box.pack_start(bar, False, False, 0)
        self.show_all()
        self._load()

    def _load(self):
        self.app.bg(self.app.tracker.fetch, self._loaded)

    def _loaded(self, items, err):
        if err:
            self.status.set_text(f"Couldn't load: {err}")
            return
        self.items = items
        self.store.clear()
        cutoff = (time.time() - 31 * 86400) * 1000
        for x in reversed(Tracker.clock_events(items)):
            ev = x["ev"]
            if ev["at"] < cutoff:
                continue
            flag = "adjusted" if ev.get("adjusted") else ""
            self.store.append([x["id"], fmt_dt(ev["at"] / 1000), LABELS[ev["type"]], flag])
        self.status.set_text("Select an entry above." if len(self.store) else "No entries in the last 31 days.")

    def _on_select(self, sel):
        model, it = sel.get_selected()
        self.save_btn.set_sensitive(it is not None)
        if it is None:
            return
        self.status.set_text("Enter the correct time, then click Save change.")
        ev = next(x["ev"] for x in self.items if x["id"] == model[it][0])
        self.date.set_text(local_date(ev["at"] / 1000))
        self.time.set_text(fmt_time(ev["at"] / 1000))

    def _on_save(self, _b):
        model, it = self.view.get_selection().get_selected()
        if it is None:
            return
        try:
            new_at = parse_local(self.date.get_text(), self.time.get_text())
        except ValueError:
            self.app.error("Please enter the date as YYYY-MM-DD and the time as HH:MM.", self)
            return
        try:
            op = self.app.tracker.prepare_adjust(self.items, model[it][0], new_at, self.reason.get_text())
        except TrackerError as e:
            self.app.error(e, self)
            return
        self.save_btn.set_sensitive(False)
        self.status.set_text("Saving…")

        def done(_res, err):
            if err:
                self.status.set_text("Not saved.")
                self.save_btn.set_sensitive(True)
                self.app.error(err, self)
                return
            self.app.tracker.finish_adjust(op)
            self.app.refresh()
            self.app.kick_sync()
            self.reason.set_text("")
            self.status.set_text("Saved. Reloading…")
            self._load()
        self.app.bg(lambda: self.app.tracker.send_adjust(op), done)


class NotesWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Labtracker: my notes")
        self.set_default_size(560, 460)
        self.set_border_width(14)
        sw = Gtk.ScrolledWindow()
        sw.set_shadow_type(Gtk.ShadowType.IN)
        self.view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=10, right_margin=10, top_margin=8)
        self.view.get_buffer().set_text("Loading your notes…")
        sw.add(self.view)
        self.add(sw)
        self.show_all()
        app.bg(app.tracker.fetch, self._loaded)

    def _loaded(self, items, err):
        buf = self.view.get_buffer()
        if err:
            buf.set_text(f"Couldn't load notes: {err}")
            return
        notes = list(reversed(Tracker.notes(items)))
        if not notes:
            buf.set_text("No notes yet.")
            return
        buf.set_text("\n\n".join(f"{fmt_dt(n['ev']['at'] / 1000)}\n{n['ev']['text']}" for n in notes))


# ========================================================================= main

def run() -> int:
    store = Store()
    ensure_private_dir(store.st_dir)
    lock = open(store.lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Labtracker is already running (look for the clock icon in your panel).")
        return 0

    cfg = store.load_config()
    try:
        keys = Keyring().load()
    except KeyringError as e:
        _fatal(f"Couldn't open your keyring: {e}")
        return 1
    if not keys or not cfg.get("webapp_url") or not cfg.get("user_id"):
        _fatal("Labtracker isn't set up yet.\n\nOpen a terminal and run:\n\n    labtracker setup")
        return 1
    try:
        backend = Backend(cfg["webapp_url"], keys.auth)
    except BackendError as e:
        _fatal(str(e))
        return 1
    App(store, cfg, keys, backend, IdleSource())
    Gtk.main()
    return 0


def _fatal(text):
    d = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.ERROR,
                          buttons=Gtk.ButtonsType.OK, text=text)
    d.set_title("Labtracker")
    d.run()
    d.destroy()
