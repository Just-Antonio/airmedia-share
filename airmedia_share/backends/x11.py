"""X11: PyAV's x11grab captures the screen or one window, and Wnck lists the windows.

Wnck walks X11/EWMH windows directly. That also tells us each window's process id, so a
window row carries its own app's pids and only that app's sound goes with it.
"""

import os

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Wnck", "3.0")
from aiortc.contrib.media import MediaPlayer  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, Wnck  # noqa: E402

from . import Backend  # noqa: E402


def open_capture(display, *, window_id=None, screen_size=None, fps=30):
    """x11grab of the whole screen, or of one window by X id (follows it when moved)."""
    options = {"framerate": str(fps), "draw_mouse": "1"}
    if window_id is not None:
        options["window_id"] = str(window_id)
    else:
        options["video_size"] = "%dx%d" % screen_size
    return MediaPlayer(display, format="x11grab", options=options)


def make_capture(target, screen_size):
    """A video track for the whole screen (target None) or the window with this X id."""
    display = os.environ.get("DISPLAY", ":0")
    if target is None:
        player = open_capture(display, screen_size=screen_size)
    else:
        player = open_capture(display, window_id=target)
    return player.video


def screen_size():
    screen = Gdk.Screen.get_default()
    return screen.get_width(), screen.get_height()


class X11Backend(Backend):
    hint = ("<small>A window shares only that app's sound. To share a browser tab, "
            "pick the browser window; the TV shows whichever tab is in front.</small>")

    def __init__(self, window):
        super().__init__(window)
        self.wnck = None

    def build_sources(self):
        """Rebuild the list: the whole screen, then every normal window (also an idle callback)."""
        window = self.window
        if self.wnck is None:
            self.wnck = Wnck.Screen.get_default()
            self.wnck.force_update()
            for signal in ("window-opened", "window-closed"):
                self.wnck.connect(signal, lambda *a: GLib.idle_add(self.build_sources))
        selected = window.selected_row_attr("target")
        for row in window.sources.get_children():
            window.sources.remove(row)
        window.add_source_row(None, "Entire screen", Gtk.Image.new_from_icon_name(
            "video-display", Gtk.IconSize.LARGE_TOOLBAR))
        own = window.get_window().get_xid() if window.get_window() else None
        for win in self.wnck.get_windows():
            if win.get_window_type() != Wnck.WindowType.NORMAL or win.get_xid() == own:
                continue
            icon = Gtk.Image.new_from_pixbuf(win.get_mini_icon())
            window.add_source_row(win.get_xid(), win.get_name(), icon, win, win.get_pid())
        window.sources.show_all()
        for row in window.sources.get_children():
            if row.target == selected:
                window.sources.select_row(row)
                break
        else:
            window.sources.select_row(window.sources.get_row_at_index(0))
        return False

    def start(self, host):
        window = self.window
        target = window.selected_row_attr("target")
        window.begin(host, target, make_capture(target, screen_size()),
                     window.selected_row_attr("title", "Entire screen"),
                     window.selected_row_attr("pids"))

    def switch(self, row):
        # Switch what is shared without asking the TV for a new code.
        self.window.switch_source(row, row.title, make_capture(row.target, screen_size()),
                                  row.pids)
