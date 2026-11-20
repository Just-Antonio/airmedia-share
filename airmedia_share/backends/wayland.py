"""Wayland: the desktop portal's own picker chooses the screen or window.

A Wayland client cannot list or read other windows. It asks xdg-desktop-portal (portal.py),
the person picks in the compositor's dialog, and the granted PipeWire node is read by
pipewire.py. Two things follow from that, and this module is where they are handled:

* There is no window list to show, only "Entire screen" and "Choose a screen or window…".
  Switching while sharing may open the picker again; the TV session and its code stay.
* The picker does not say which process owns a window, so a window cannot carry its
  own app's sound the way it does on X11. For a window the person picks whose sound to
  send instead: everything, or one of the apps playing right now.
"""

import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import media  # noqa: E402
from . import Backend, pipewire, portal  # noqa: E402

log = logging.getLogger(__name__)

# Source rows: the portal's picker decides what a window is, so there are only these two.
SCREEN, CHOOSE = "screen", "choose"


class WaylandBackend(Backend):
    hint = ("<small>“Choose a screen or window…” opens your desktop's own picker. "
            "To share a browser tab, pick the browser window; the TV shows whichever "
            "tab is in front.</small>")

    def __init__(self, window):
        super().__init__(window)
        self.screencast = portal.ScreenCast()
        self.picking = False  # the portal's picker is open
        self.shared_is_window = False
        self.sound_revealer = None
        self.sound_combo = None
        self.sound_pids = {"all": None}  # combo id -> process ids (None: everything)
        self.updating_sound = False
        self.timer = None

    @property
    def busy(self):
        return self.picking

    @classmethod
    def problem(cls):
        if portal.portal_info() is None:
            return ("The screen-sharing portal is not available",
                    "AirMedia Share asks your desktop for the screen through "
                    "xdg-desktop-portal. Install it and the backend for your desktop "
                    "(xdg-desktop-portal-kde, -gnome or -wlr), log out and in again, "
                    "and try again.")
        problem = pipewire.capture_problem()
        if problem:
            return ("Screen capture is not set up", problem)
        return None

    # ----- the list ----------------------------------------------------------

    def extra_widget(self):
        """The "Sound from" row, shown only while a window is (about to be) shared."""
        self.sound_revealer = Gtk.Revealer()
        row = Gtk.Box(spacing=12)
        row.pack_start(Gtk.Label(label="Sound from", xalign=0), False, False, 0)
        self.sound_combo = Gtk.ComboBoxText()
        self.sound_combo.set_tooltip_text(
            "For a window: send everything this computer plays, or only one app's sound. "
            "An app appears here once it is playing something.")
        self.sound_combo.connect("changed", self.on_sound_source_changed)
        row.pack_start(self.sound_combo, True, True, 0)
        self.sound_revealer.add(row)
        return self.sound_revealer

    def build_sources(self):
        window = self.window
        for row in window.sources.get_children():
            window.sources.remove(row)
        window.add_source_row(SCREEN, "Entire screen", Gtk.Image.new_from_icon_name(
            "video-display", Gtk.IconSize.LARGE_TOOLBAR))
        window.add_source_row(CHOOSE, "Choose a screen or window…",
                              Gtk.Image.new_from_icon_name("window-new",
                                                           Gtk.IconSize.LARGE_TOOLBAR))
        window.sources.show_all()
        window.sources.select_row(window.sources.get_row_at_index(0))
        if self.timer is None:
            self.timer = GLib.timeout_add_seconds(2, self.refresh_sound_apps)
        return False

    @staticmethod
    def label(target, stream):
        """What to call a granted source: the portal gives no window title, only its kind."""
        if target == SCREEN:
            return "Entire screen"
        return "Selected window" if stream.source_type == portal.WINDOW else "Selected screen"

    # ----- asking the portal -------------------------------------------------

    def set_picking(self, picking):
        self.picking = picking
        self.window.sources.set_sensitive(not picking)
        self.window.update_tv_widgets()

    def request_stream(self, target, on_result):
        """Ask the portal for the screen or for a window: on_result(stream, error).

        Both are None if the person cancelled the picker. For the screen, the portal
        is given the token from last time, which usually lets it skip the dialog.
        """
        if self.picking:
            return
        settings = self.window.settings
        kind, token = (("monitor", settings["portal_restore_token"])
                       if target == SCREEN else ("any", None))
        self.set_picking(True)

        def done(stream, error):
            self.set_picking(False)
            if error:
                log.error("portal: %s", error)
            if stream is not None and stream.source_type != portal.WINDOW \
                    and stream.restore_token:
                # A screen was granted (whichever row asked): remember it, so the next
                # "Entire screen" needs no dialog. Tokens are single-use; this is the new one.
                settings["portal_restore_token"] = stream.restore_token
                settings.save()
            on_result(stream, error)

        self.screencast.select(kind, token, done)

    def open_source(self, stream):
        try:
            return pipewire.PipeWireVideoSource(stream)
        except Exception as e:
            log.exception("could not read the PipeWire stream")
            stream.close()
            self.window.set_status(f"Could not read the screen capture: {e}")
            return None

    # ----- starting and switching --------------------------------------------

    def start(self, host):
        # Choose what to share first, so the TV only shows its code once there is
        # something to send.
        window = self.window
        target = window.selected_row_attr("target")
        window.set_status("Asking the desktop what to share…")

        def got(stream, error):
            if stream is None:
                window.set_status(error or "Nothing shared.")
                return
            source = self.open_source(stream)
            if source is not None:
                self.shared_is_window = stream.source_type == portal.WINDOW
                window.begin(host, target, source, self.label(target, stream),
                             self.sound_pids_for_share())

        self.request_stream(target, got)

    def switch(self, row):
        """Share another screen or window; if the picker is cancelled or fails, keep going."""
        window = self.window

        def got(stream, error):
            if window.state != "sharing":  # the share ended while the picker was open
                if stream is not None:
                    stream.close()
                return
            if stream is None:
                window.sources.select_row(window.shared_row)
                if error:
                    window.set_status(f"{error} Still sharing “{window.shared_label}”.")
                return
            source = self.open_source(stream)
            if source is None:
                window.sources.select_row(window.shared_row)
                return
            self.shared_is_window = stream.source_type == portal.WINDOW
            self.update_sound_row()
            window.switch_source(row, self.label(row.target, stream), source,
                                 self.sound_pids_for_share())

        self.request_stream(row.target, got)

    def row_activated(self, row):
        # Choosing "Choose…" again while it is already shared means "a different window";
        # the row is already selected, so there is no row-selected signal for it.
        window = self.window
        if window.state == "sharing" and row.target == CHOOSE \
                and window.shared_target == CHOOSE:
            self.switch(row)

    def selection_changed(self, row):
        self.update_sound_row()

    def state_changed(self):
        self.update_sound_row()

    def session_ended(self):
        self.shared_is_window = False

    # ----- whose sound -------------------------------------------------------

    def update_sound_row(self):
        if self.sound_revealer is None:
            return
        window = self.window
        if window.state == "idle":
            show = window.selected_row_attr("target") == CHOOSE
        else:
            show = self.shared_is_window
        self.sound_revealer.set_reveal_child(show)
        if show:
            self.refresh_sound_apps()

    def refresh_sound_apps(self):
        """Keep the "Sound from" list to the apps playing now, plus the current choice.

        Also the 2-second timer's callback, so it returns True to keep running.
        """
        if self.sound_revealer is None or not self.sound_revealer.get_reveal_child() \
                or self.sound_combo.get_property("popup-shown"):
            return True
        apps = dict(media.list_audio_apps())
        current = self.sound_combo.get_active_id() or "all"
        if current != "all" and current not in apps and current in self.sound_pids:
            apps[current] = self.sound_pids[current]  # chosen earlier, quiet for the moment
        fresh = {"all": None, **apps}
        if fresh == self.sound_pids and self.sound_combo.get_active_id():
            return True
        self.updating_sound = True
        self.sound_pids = fresh
        self.sound_combo.remove_all()
        self.sound_combo.append("all", "Everything the computer plays")
        for name in apps:
            self.sound_combo.append(name, name)
        self.sound_combo.set_active_id(current if current in fresh else "all")
        self.updating_sound = False
        return True

    def sound_pids_for_share(self):
        """Whose sound goes with what is shared: the chosen app for a window, else all."""
        if self.shared_is_window:
            return self.sound_pids.get(self.sound_combo.get_active_id() or "all")
        return None

    def on_sound_source_changed(self, combo):
        worker = self.window.app.worker
        if self.updating_sound or worker is None or worker.audio is None \
                or not self.shared_is_window:
            return
        pids = self.sound_pids.get(combo.get_active_id() or "all")
        worker.loop.call_soon_threadsafe(worker.audio.follow_pids, pids)
        log.info("sound from %s (pids %s)", combo.get_active_id(), pids)
