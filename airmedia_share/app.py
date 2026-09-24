"""AirMedia Share: the GTK window.

GTK runs on the main thread; the receiver session (asyncio + aiortc) runs on a second
thread. The two talk through GLib.idle_add (session -> UI) and
asyncio.run_coroutine_threadsafe / call_soon_threadsafe (UI -> session).

Single instance via Gtk.Application: launching again raises the window, and
`airmedia-share --stop` stops a running share. Closing the window ends the process
outright (os._exit, in __main__): aiortc's capture threads are not daemons, and a
lingering instance would silently absorb every later launch, running stale code.
"""

import asyncio
import concurrent.futures
import logging
import os
import socket
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Wnck", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gdk, Gio, GLib, Gtk, Notify, Wnck  # noqa: E402

from . import media, receiver  # noqa: E402
from .settings import Settings  # noqa: E402

APP_ID = "io.github.just_antonio.AirMediaShare"
APP_NAME = "AirMedia Share"
LOG_PATH = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                        "airmedia-share", "sender.log")

log = logging.getLogger(__name__)


def volume_gain(percent):
    # Same cubic curve PulseAudio uses, so the slider feels like the system one.
    return (percent / 100) ** 3


def reachable(host, timeout=3):
    try:
        with socket.create_connection((host, receiver.PORT), timeout=timeout):
            return True
    except OSError:
        return False


class SessionThread:
    """Owns the asyncio loop and at most one receiver.Session."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.session = None
        self.audio = None

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


def make_capture(target, screen_size):
    display = os.environ.get("DISPLAY", ":0")
    if target is None:
        player = media.open_capture(display, screen_size=screen_size)
    else:
        player = media.open_capture(display, window_id=target)
    return player.video


class ShareWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.settings = app.settings
        self.set_default_size(460, 600)
        self.set_icon_name("video-display")

        self.header = Gtk.HeaderBar(show_close_button=True, title=APP_NAME)
        self.set_titlebar(self.header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        self.add(outer)

        # ----- which TV
        tv_row = Gtk.Box(spacing=8)
        tv_label = Gtk.Label(xalign=0)
        tv_label.set_markup("<b>TV</b>")
        tv_row.pack_start(tv_label, False, False, 0)
        self.tv_combo = Gtk.ComboBoxText()
        self.tv_combo.connect("changed", self.on_tv_changed)
        tv_row.pack_start(self.tv_combo, True, True, 0)
        self.tv_menu_button = Gtk.MenuButton(popup=self.build_tv_menu(),
                                             tooltip_text="Manage TVs")
        self.tv_menu_button.add(Gtk.Image.new_from_icon_name("view-more-symbolic",
                                                             Gtk.IconSize.BUTTON))
        tv_row.pack_start(self.tv_menu_button, False, False, 0)
        outer.pack_start(tv_row, False, False, 0)

        # ----- what to share
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>What to share</b>")
        outer.pack_start(heading, False, False, 0)

        self.sources = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sources.connect("row-selected", self.on_source_selected)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.sources)
        outer.pack_start(scroller, True, True, 0)

        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_markup(
            "<small>A window shares only that app's sound. To share a browser tab, "
            "pick the browser window; the TV shows whichever tab is in front.</small>")
        hint.get_style_context().add_class("dim-label")
        outer.pack_start(hint, False, False, 0)

        # ----- sound
        audio_row = Gtk.Box(spacing=12)
        audio_row.pack_start(Gtk.Label(label="Sound on the TV", xalign=0), False, False, 0)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_value(self.settings["volume"])
        self.volume.set_draw_value(False)
        self.volume.set_tooltip_text(
            "Level sent to the TV, independent of this computer's speakers. "
            "The TV's own remote still sets its overall volume.")
        self.volume.connect("value-changed", self.on_volume_changed)
        audio_row.pack_start(self.volume, True, True, 0)
        self.audio_switch = Gtk.Switch(active=self.settings["sound"], valign=Gtk.Align.CENTER)
        self.audio_switch.connect("notify::active", self.on_audio_toggled)
        audio_row.pack_end(self.audio_switch, False, False, 0)
        outer.pack_start(audio_row, False, False, 0)
        self.volume.set_sensitive(self.settings["sound"])

        # ----- code entry, revealed only once the TV is showing a code
        self.code_revealer = Gtk.Revealer()
        code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.code_prompt = Gtk.Label(xalign=0)
        code_box.pack_start(self.code_prompt, False, False, 0)
        code_row = Gtk.Box(spacing=8)
        self.code_entry = Gtk.Entry(max_length=8, width_chars=8,
                                    input_purpose=Gtk.InputPurpose.DIGITS,
                                    placeholder_text="0000")
        self.code_entry.connect("activate", self.on_code_submit)
        code_row.pack_start(self.code_entry, True, True, 0)
        code_ok = Gtk.Button(label="Connect")
        code_ok.connect("clicked", self.on_code_submit)
        code_row.pack_start(code_ok, False, False, 0)
        code_box.pack_start(code_row, False, False, 0)
        self.code_revealer.add(code_box)
        outer.pack_start(self.code_revealer, False, False, 0)
        self.code_future = None

        self.status = Gtk.Label(xalign=0, wrap=True)
        outer.pack_start(self.status, False, False, 0)

        self.button = Gtk.Button(label="Share")
        self.button.get_style_context().add_class("suggested-action")
        self.button.connect("clicked", self.on_button)
        outer.pack_start(self.button, False, False, 0)

        self.state = "idle"
        self.closing = False
        self.shared_label = ""
        self.shared_target = None
        self.connect("delete-event", self.on_close)

        self.wnck = Wnck.Screen.get_default()
        self.wnck.force_update()
        for signal in ("window-opened", "window-closed"):
            self.wnck.connect(signal, lambda *a: GLib.idle_add(self.refresh_sources))
        self.refresh_sources()
        self.refresh_tvs(self.settings["default"])
        self.set_status("Ready." if self.settings["tvs"] else
                        "Add a TV to get started (⋮ menu next to TV).")

    # ----- TVs ---------------------------------------------------------------

    def build_tv_menu(self):
        menu = Gtk.Menu()

        def item(label, handler):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: handler())
            menu.append(mi)
            return mi

        item("Add a TV…", self.add_tv_dialog)
        self.rename_item = item("Rename this TV…", self.rename_tv_dialog)
        self.default_item = item("Make this TV the default", self.make_default)
        self.remove_item = item("Remove this TV", self.remove_tv)
        menu.append(Gtk.SeparatorMenuItem())
        self.follow_item = Gtk.CheckMenuItem(
            label="Computer volume keys also control the TV",
            active=self.settings["follow_system_volume"])
        self.follow_item.connect("toggled", self.on_follow_toggled)
        menu.append(self.follow_item)
        menu.show_all()
        return menu

    def current_host(self):
        return self.tv_combo.get_active_id()

    def refresh_tvs(self, select=None):
        select = select or self.current_host() or self.settings["default"]
        self.tv_combo.remove_all()
        for tv in self.settings["tvs"]:
            suffix = "  (default)" if tv["host"] == self.settings["default"] else ""
            self.tv_combo.append(tv["host"], tv["label"] + suffix)
        if select and self.settings.tv(select):
            self.tv_combo.set_active_id(select)
        elif self.settings["tvs"]:
            self.tv_combo.set_active(0)
        self.update_tv_widgets()

    def update_tv_widgets(self):
        host = self.current_host()
        tv = self.settings.tv(host) if host else None
        self.header.set_subtitle(tv["label"] if tv else "No TV yet")
        self.tv_combo.set_tooltip_text(
            f"{tv['name'] or 'AirMedia receiver'} at {tv['host']}" if tv else None)
        for mi in (self.rename_item, self.remove_item):
            mi.set_sensitive(tv is not None)
        self.default_item.set_sensitive(tv is not None and host != self.settings["default"])
        idle = self.state == "idle"
        self.tv_combo.set_sensitive(idle)
        self.tv_menu_button.set_sensitive(idle)
        self.button.set_sensitive(tv is not None or not idle)

    def on_tv_changed(self, _combo):
        self.update_tv_widgets()

    def ask_text(self, title, fields, ok_label="Save"):
        """Small form dialog; fields = [(label, initial, hint)]. Returns values or None."""
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, ok_label, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        entries = []
        for label, initial, hint in fields:
            box.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
            entry = Gtk.Entry(text=initial or "", activates_default=True)
            box.pack_start(entry, False, False, 0)
            if hint:
                small = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
                small.set_markup(f"<small>{GLib.markup_escape_text(hint)}</small>")
                small.get_style_context().add_class("dim-label")
                box.pack_start(small, False, False, 0)
            entries.append(entry)
        dialog.show_all()
        response = dialog.run()
        values = [e.get_text().strip() for e in entries]
        dialog.destroy()
        return values if response == Gtk.ResponseType.OK else None

    def confirm(self, text, secondary, ok_label):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=text,
                                   secondary_text=secondary)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, ok_label, Gtk.ResponseType.OK)
        ok = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return ok

    def add_tv_dialog(self):
        values = self.ask_text("Add a TV", [
            ("Address", "", "Shown on the TV's idle screen, e.g. “To present visit: "
                            "http://10.0.0.5”. An IP address or hostname."),
            ("Name", "", "What you call it, e.g. “Office TV”. Optional."),
        ], ok_label="Add")
        if not values or not values[0]:
            return
        address, label = values
        host = address.strip().removeprefix("http://").removeprefix("https://").strip("/")
        if not reachable(host) and not self.confirm(
                f"Can't reach {host}",
                f"Nothing answered on port {receiver.PORT}. The TV may be off or on "
                "another network. Add it anyway?", "Add anyway"):
            return
        host = self.settings.add_tv(address, label)
        self.settings.save()
        self.refresh_tvs(host)
        self.set_status(f"Added {self.settings.tv(host)['label']}.")

    def rename_tv_dialog(self):
        tv = self.settings.tv(self.current_host())
        values = self.ask_text("Rename TV", [("Name", tv["label"],
                                              f"{tv['name'] or 'AirMedia receiver'} at "
                                              f"{tv['host']}")])
        if values and values[0]:
            self.settings.rename_tv(tv["host"], values[0])
            self.settings.save()
            self.refresh_tvs(tv["host"])

    def make_default(self):
        self.settings["default"] = self.current_host()
        self.settings.save()
        self.refresh_tvs()

    def remove_tv(self):
        tv = self.settings.tv(self.current_host())
        if self.confirm(f"Remove {tv['label']}?", "You can add it again later.", "Remove"):
            self.settings.remove_tv(tv["host"])
            self.settings.save()
            self.refresh_tvs(self.settings["default"])

    def on_follow_toggled(self, item):
        self.settings["follow_system_volume"] = item.get_active()
        self.settings.save()
        if self.app.worker.audio is not None:
            self.app.worker.audio.follow_system_volume = item.get_active()

    # ----- sources -----------------------------------------------------------

    def refresh_sources(self):
        selected = self.selected_row_attr("target")
        for row in self.sources.get_children():
            self.sources.remove(row)
        self.add_source_row(None, "Entire screen", Gtk.Image.new_from_icon_name(
            "video-display", Gtk.IconSize.LARGE_TOOLBAR))
        own = self.get_window().get_xid() if self.get_window() else None
        for win in self.wnck.get_windows():
            if win.get_window_type() != Wnck.WindowType.NORMAL or win.get_xid() == own:
                continue
            icon = Gtk.Image.new_from_pixbuf(win.get_mini_icon())
            self.add_source_row(win.get_xid(), win.get_name(), icon, win, win.get_pid())
        self.sources.show_all()
        for row in self.sources.get_children():
            if row.target == selected:
                self.sources.select_row(row)
                break
        else:
            self.sources.select_row(self.sources.get_row_at_index(0))
        return False

    def add_source_row(self, target, title, icon, win=None, pid=0):
        row = Gtk.ListBoxRow()
        row.target = target
        row.title = title
        # Whose sound goes with it: None = everything (entire screen).
        row.pids = {pid} if pid else None
        box = Gtk.Box(spacing=10, margin=8)
        box.pack_start(icon, False, False, 0)
        label = Gtk.Label(label=title, xalign=0, ellipsize=3)  # PANGO_ELLIPSIZE_END
        box.pack_start(label, True, True, 0)
        row.add(box)
        if win is not None:
            win.connect("name-changed", lambda w, l=label, r=row: (
                l.set_text(w.get_name()), setattr(r, "title", w.get_name())))
        self.sources.add(row)

    def selected_row_attr(self, name, default=None):
        row = self.sources.get_selected_row()
        return getattr(row, name) if row else default

    def screen_size(self):
        screen = Gdk.Screen.get_default()
        return screen.get_width(), screen.get_height()

    def on_source_selected(self, _box, row):
        if row is None or self.state != "sharing" or row.target == self.shared_target:
            return
        # Switch what is shared without asking the TV for a new code.
        source = make_capture(row.target, self.screen_size())
        self.shared_label, self.shared_target = row.title, row.target
        worker = self.app.worker
        worker.loop.call_soon_threadsafe(worker.session.switch, source, row.pids)
        self.set_status(f"Sharing “{row.title}” on {self.tv_label()}.")
        log.info("switched to %s (pids %s)", row.title, row.pids)

    def on_audio_toggled(self, switch, _param):
        self.settings["sound"] = switch.get_active()
        self.volume.set_sensitive(switch.get_active())
        self.settings.save()
        if self.app.worker.audio is not None:
            self.app.worker.audio.muted = not switch.get_active()

    def on_volume_changed(self, scale):
        self.settings["volume"] = round(scale.get_value())
        if self.app.worker.audio is not None:
            self.app.worker.audio.volume = volume_gain(scale.get_value())

    # ----- sharing -----------------------------------------------------------

    def tv_label(self, host=None):
        tv = self.settings.tv(host or self.current_host())
        return tv["label"] if tv else "the TV"

    def on_button(self, _button):
        if self.state == "idle":
            self.start()
        else:
            self.app.stop_sharing()

    def start(self):
        host = self.current_host()
        if not host:
            self.add_tv_dialog()
            return
        worker = self.app.worker
        target = self.selected_row_attr("target")
        self.shared_label = self.selected_row_attr("title", "Entire screen")
        self.shared_target = target
        mbps = max(2, int(self.settings["max_mbps"]))
        receiver.set_bitrate_range(2_000_000, min(10, mbps) * 1_000_000, mbps * 1_000_000)

        audio = media.ComputerAudioTrack()
        audio.muted = not self.audio_switch.get_active()
        audio.volume = volume_gain(self.volume.get_value())
        audio.follow_system_volume = self.settings["follow_system_volume"]
        audio.follow_pids(self.selected_row_attr("pids"))
        worker.audio = audio

        def on_state(state, detail=""):
            GLib.idle_add(self.on_session_state, state, detail)

        video = media.ScreenVideoTrack(make_capture(target, self.screen_size()), *media.CANVAS)
        session = receiver.Session(host, self.ask_code, video, audio, on_state=on_state,
                                   video_codec=self.settings["video_codec"])
        worker.session = session
        self.session_host = host

        async def run():
            try:
                await session.run()
                return None
            except receiver.Cancelled:
                return None
            except receiver.AirMediaError as e:
                return str(e)
            except (OSError, asyncio.TimeoutError) as e:
                log.exception("connection failed")
                return f"Could not reach {self.tv_label(host)} at {host} " \
                       f"({e.__class__.__name__}). Is it on, and on this network?"
            except Exception as e:
                log.exception("session crashed")
                return f"Something went wrong: {e}. Details in {LOG_PATH}"

        fut = worker.submit(run())
        fut.add_done_callback(lambda f: GLib.idle_add(self.on_session_done, f.result()))
        self.set_state("connecting")

    async def ask_code(self, retry):
        fut = concurrent.futures.Future()
        GLib.idle_add(self.show_code_entry, fut, retry)
        return await asyncio.wrap_future(fut)

    def show_code_entry(self, fut, retry):
        self.code_future = fut
        self.code_prompt.set_markup(
            "<b>That code did not work.</b> Type the code now shown on the TV:"
            if retry else "Type the code shown on the TV:")
        self.code_entry.set_text("")
        self.code_revealer.set_reveal_child(True)
        self.present()
        self.code_entry.grab_focus()
        return False

    def on_code_submit(self, *_):
        code = self.code_entry.get_text().strip()
        if not code or self.code_future is None:
            return
        self.code_revealer.set_reveal_child(False)
        self.code_future.set_result(code)
        self.code_future = None
        self.set_status("Checking the code…")

    def cancel_code(self):
        if self.code_future is not None:
            self.code_future.set_result(None)
            self.code_future = None
        self.code_revealer.set_reveal_child(False)

    def on_session_state(self, state, detail):
        if state == "connecting":
            self.set_status(f"Connecting to {self.tv_label(self.session_host)}…")
        elif state == "code":
            self.set_status("The TV is showing a code.")
        elif state == "sharing":
            session = self.app.worker.session
            if session and self.settings.note_receiver_name(self.session_host,
                                                             session.receiver_name):
                self.settings.save()
                self.update_tv_widgets()
            self.set_state("sharing")
            label = self.tv_label(self.session_host)
            self.set_status(f"Sharing “{self.shared_label}” on {label}.")
            self.app.notify(f"Sharing to {label}",
                            f"“{self.shared_label}”. Open {APP_NAME} to stop.")
        return False

    def on_session_done(self, error):
        was_sharing = self.state == "sharing"
        self.cancel_code()
        self.app.worker.session = None
        self.app.worker.audio = None
        self.set_state("idle")
        if error:
            self.set_status(error)
        else:
            self.set_status("Stopped." if was_sharing else "Ready.")
        if was_sharing:
            self.app.notify("Stopped sharing",
                            error or f"{self.tv_label(self.session_host)} is no longer "
                                     "showing this computer.")
        if self.closing:
            self.app.quit()
        return False

    def set_state(self, state):
        self.state = state
        ctx = self.button.get_style_context()
        ctx.remove_class("suggested-action")
        ctx.remove_class("destructive-action")
        if state == "idle":
            self.button.set_label("Share")
            ctx.add_class("suggested-action")
        elif state == "connecting":
            self.button.set_label("Cancel")
        else:
            self.button.set_label("Stop sharing")
            ctx.add_class("destructive-action")
        self.update_tv_widgets()

    def set_status(self, text):
        self.status.set_text(text)

    def on_close(self, *_):
        self.settings.save()
        if self.state == "idle":
            self.app.quit()
            return False
        # Say goodbye to the TV first, then quit (on_session_done).
        self.closing = True
        self.hide()
        self.app.stop_sharing()
        return True


class ShareApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.worker = None
        self.settings = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Notify.init(APP_NAME)
        self.settings = Settings()
        self.worker = SessionThread()

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        if "--stop" in args:
            if self.window is not None and self.window.state != "idle":
                self.stop_sharing()
            elif self.window is None:
                self.quit()  # launched only to stop, and nothing was running
            return 0
        if self.window is None:
            if os.environ.get("XDG_SESSION_TYPE") == "wayland":
                self.fatal("AirMedia Share needs an X11 session",
                           "Screen capture here uses X11. Log out, pick “Ubuntu on Xorg” "
                           "from the gear on the login screen, and try again.")
                return 1
            self.window = ShareWindow(self)
            self.window.show_all()
            if not self.settings["tvs"]:
                GLib.idle_add(lambda: self.window.add_tv_dialog() and False)
        self.window.present()
        return 0

    def fatal(self, text, secondary):
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=text,
                                   secondary_text=secondary)
        dialog.run()
        dialog.destroy()
        self.quit()

    def stop_sharing(self):
        if self.window is not None:
            self.window.cancel_code()
            self.window.set_status("Stopping…")
        if self.worker.session is not None:
            self.worker.submit(self.worker.session.stop())

    def notify(self, title, body):
        try:
            Notify.Notification.new(title, body, "video-display").show()
        except GLib.Error:
            pass
