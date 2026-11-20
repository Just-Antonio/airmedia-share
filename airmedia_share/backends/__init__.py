"""Capture backends: everything that differs between an X11 session and a Wayland one.

The window in app.py knows neither. It asks `load()` for the backend that fits the
running session and talks to it through the small `Backend` interface below.

    x11.py       x11grab capture; the list of windows comes from Wnck
    wayland.py   the desktop portal's own picker; the picked source is read via PipeWire
    pipewire.py  (Wayland) frames from a PipeWire node through GStreamer; no GTK
    portal.py    (Wayland) the xdg-desktop-portal ScreenCast client; no GTK

Only the module for the running session is ever imported, so an X11 session never needs
the GStreamer bindings and a Wayland one never needs Wnck. pipewire.py and portal.py
stay free of GTK so their pure parts can be tested without a display.
"""

import os


def session_type():
    return "wayland" if os.environ.get("XDG_SESSION_TYPE") == "wayland" else "x11"


def backend_class():
    if session_type() == "wayland":
        from .wayland import WaylandBackend
        return WaylandBackend
    from .x11 import X11Backend
    return X11Backend


def load(window):
    return backend_class()(window)


class Backend:
    """What a backend gives the window. `window` is the app.ShareWindow it serves."""

    hint = ""     # Pango markup shown under the list of things to share
    busy = False  # True while waiting on the person: disables the TV and Share controls

    def __init__(self, window):
        self.window = window

    @classmethod
    def problem(cls):
        """None if capture can work in this session, else (title, what to do) for a dialog."""
        return None

    def extra_widget(self):
        """A widget to show under the sound row, or None."""
        return None

    def build_sources(self):
        """Fill window.sources with rows (ShareWindow.add_source_row) and select one."""
        raise NotImplementedError

    def start(self, host):
        """Open the selected source, then call window.begin(...) to connect to the TV."""
        raise NotImplementedError

    def selection_changed(self, row):
        """The selected row changed, in any state (`row` can be None)."""

    def switch(self, row):
        """While sharing, another row was picked: swap the feed (ShareWindow.switch_source)."""
        raise NotImplementedError

    def row_activated(self, row):
        """A row was clicked, including one that was already selected."""

    def state_changed(self):
        """The window's state (idle, connecting, sharing) changed."""

    def session_ended(self):
        """The share ended, for any reason."""
