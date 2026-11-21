"""The X11 backend through the real window, with the capture stubbed.

Needs an X11 display (a real X session, Xvfb, or XWayland: it forces GDK_BACKEND=x11)
and the Wnck bindings. x11grab is replaced by a test pattern, because PyAV builds do not
all ship it; everything else is the real code: the Wnck window list, start, switching,
per-window sound, and the whole session, against tests/fake_receiver.py.

Run by tests/test_gui.py, or by hand: python -m tests.gui.x11_paths
"""

import types

from tests.gui.harness import Harness

h = Harness("x11", session="x11", env={"GDK_BACKEND": "x11"})

import sys  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from aiortc import VideoStreamTrack  # noqa: E402
from gi.repository import Gtk  # noqa: E402

from airmedia_share import media  # noqa: E402
from airmedia_share.backends import x11  # noqa: E402

opened = []  # what the backend asked the capture for


def fake_open_capture(display, *, window_id=None, screen_size=None, fps=30):
    opened.append({"window_id": window_id, "screen_size": screen_size})
    return types.SimpleNamespace(video=VideoStreamTrack())


class QuietAudio(media.ComputerAudioTrack):
    """The real audio track, minus parec and pactl, so no sound server is needed."""

    async def _watch_streams(self):
        return

    async def _rescan(self):
        return


x11.open_capture = fake_open_capture
media.ComputerAudioTrack = QuietAudio

WINDOW_ID, WINDOW_PID = 12345, 4242


def scenario(h, app):
    win = app.window
    be = win.backend
    worker = app.worker

    rows = win.sources.get_children()
    h.say(f"rows from Wnck: {[(r.target, r.title) for r in rows]}")
    h.check("the X11 backend is in use", type(be) is x11.X11Backend)
    h.check("the Wayland module and GStreamer were never loaded",
            "airmedia_share.backends.wayland" not in sys.modules
            and "gi.repository.Gst" not in sys.modules)
    h.check("the first row is the entire screen",
            rows[0].target is None and rows[0].title == "Entire screen" and rows[0].pids is None)
    h.check("X11 has no extra widget and is never waiting on the desktop",
            be.extra_widget() is None and be.busy is False)
    h.check("there is nothing to complain about at startup", x11.X11Backend.problem() is None)

    # What a Wnck window-opened signal triggers: rebuild the list, keep the selection.
    be.build_sources()
    h.check("rebuilding the list keeps the entire screen selected",
            win.sources.get_selected_row().target is None)

    # Make sure there is a window to switch to, whatever Wnck happens to list.
    win.add_source_row(WINDOW_ID, "Test window", Gtk.Image.new_from_icon_name(
        "video-display", Gtk.IconSize.LARGE_TOOLBAR), None, WINDOW_PID)
    win.sources.show_all()
    window_row = [r for r in win.sources.get_children() if r.target == WINDOW_ID][0]

    win.on_button(None)
    yield from h.enter_code(win)
    yield h.until(lambda: win.state == "sharing", 30, "sharing starts")
    h.check("sharing the whole screen", win.shared_label == "Entire screen")
    h.check("the capture was opened for the screen, at the screen's size",
            opened[0]["window_id"] is None and bool(opened[0]["screen_size"]), str(opened[0]))
    h.check("the sound is everything", worker.audio._pids is None)
    yield from h.video_flows("the screen")

    win.sources.select_row(window_row)
    yield h.until(lambda: win.shared_target == WINDOW_ID, 5, "the window is shared")
    h.check("switched without a new code", win.state == "sharing"
            and win.shared_label == "Test window", win.shared_label)
    h.check("the capture was reopened for that window", opened[-1]["window_id"] == WINDOW_ID,
            str(opened[-1]))
    yield h.until(lambda: worker.audio._pids == {WINDOW_PID}, 5,
                  "the sound follows that window's process")
    h.check("the sound follows only that window's process", worker.audio._pids == {WINDOW_PID})
    yield from h.video_flows("the window")

    win.sources.select_row(win.sources.get_row_at_index(0))
    yield h.until(lambda: win.shared_target is None, 5, "the whole screen is shared again")
    yield h.until(lambda: worker.audio._pids is None, 5, "the sound is everything again")
    h.check("back on the whole screen, with everything's sound",
            win.shared_target is None and worker.audio._pids is None)

    app.stop_sharing()
    yield h.until(lambda: win.state == "idle", 20, "the share stops")
    h.check("the TV was told goodbye", h.tv.got_bye)


h.run(h.make_app(), scenario, limit=90)
