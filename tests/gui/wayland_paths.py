"""The Wayland backend through the real window, with the desktop stubbed out.

Needs a GTK display but no portal, PipeWire or sound server: a fake portal answers from a
queue (the way the real one does, later and on the main loop), a test pattern stands in
for the PipeWire stream, and the audio track does not start parec/pactl. Everything else
is the real code: the window, WaylandBackend, ScreenVideoTrack and the whole session,
against tests/fake_receiver.py.

Run by tests/test_gui.py, or by hand: python -m tests.gui.wayland_paths
Covers: cancelling or failing the picker at start and on a switch, sharing the screen,
switching to a window ("Sound from"), picking a different window, the restore token
rules, stopping while a picker is open, and the desktop ending the share.
"""

from tests.gui.harness import Harness

h = Harness("wayland", session="wayland")

from aiortc import VideoStreamTrack  # noqa: E402
from gi.repository import GLib  # noqa: E402

from airmedia_share import media  # noqa: E402
from airmedia_share.backends import pipewire, portal, wayland  # noqa: E402

SCREEN_TYPE = portal.MONITOR


class FakeStream:
    """What portal.PortalStream gives the app, minus the PipeWire fd."""

    def __init__(self, source_type, token=None):
        self.source_type, self.restore_token = source_type, token
        self.on_closed, self.closed = None, False

    def close(self):
        self.closed = True


class TestPatternSource(VideoStreamTrack):
    """Stands in for pipewire.PipeWireVideoSource: a moving test pattern tied to its stream."""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        stream.on_closed = self.stop  # the same wiring as the real source

    def stop(self):
        super().stop()
        self.stream.close()


class FakeScreenCast:
    """Stands in for portal.ScreenCast: answers from a queue, later, on the main loop."""

    def __init__(self):
        self.outcomes, self.calls, self.held = [], [], None

    def answer(self, stream=None, error=None):
        self.outcomes.append((stream, error))

    def hold(self):
        """The next request is left open (the picker stays on screen) until `held` is called."""
        self.outcomes.append("hold")

    def select(self, kind, token, on_done):
        self.calls.append((kind, token))
        outcome = self.outcomes.pop(0)
        if outcome == "hold":
            self.held = on_done
            return
        GLib.idle_add(lambda: (on_done(*outcome), False)[1])


class QuietAudio(media.ComputerAudioTrack):
    """The real audio track, minus parec and pactl, so no sound server is needed."""

    async def _watch_streams(self):
        return

    async def _rescan(self):
        return


portal.ScreenCast = FakeScreenCast
pipewire.PipeWireVideoSource = TestPatternSource
media.ComputerAudioTrack = QuietAudio
media.list_audio_apps = lambda: [("Brave", {5070}), ("mpv", {7})]
wayland.WaylandBackend.problem = classmethod(lambda cls: None)


def scenario(h, app):
    win = app.window
    be = win.backend
    sc = be.screencast
    settings = win.settings
    screen_row, choose_row = win.sources.get_children()
    worker = app.worker

    def status():
        return win.status.get_text()

    def picker_closed():
        return not be.picking

    h.check("the Wayland backend is in use, with two rows",
            type(be) is wayland.WaylandBackend
            and (screen_row.target, choose_row.target) == ("screen", "choose"))
    h.check("the sound row is hidden for the screen row",
            not be.sound_revealer.get_reveal_child())

    # ----- Share, but the picker is cancelled or fails
    sc.answer(None, None)
    win.on_button(None)
    yield h.until(picker_closed, 5, "the cancelled picker returns")
    h.check("cancel at start: stays idle and says so",
            win.state == "idle" and status() == "Nothing shared.", repr(status()))
    h.check("cancel at start: every control is usable again",
            win.button.get_sensitive() and win.tv_combo.get_sensitive()
            and win.sources.get_sensitive())

    sc.answer(None, "The portal could not start the capture.")
    win.on_button(None)
    yield h.until(picker_closed, 5, "the failed request returns")
    h.check("error at start: stays idle and shows the message",
            win.state == "idle" and status() == "The portal could not start the capture.",
            repr(status()))

    # ----- Share the screen
    sc.answer(FakeStream(SCREEN_TYPE, "token-1"))
    win.on_button(None)
    yield from h.enter_code(win)
    yield h.until(lambda: win.state == "sharing", 30, "sharing starts")
    h.check("sharing the entire screen", win.shared_label == "Entire screen", win.shared_label)
    h.check("the screen was asked for as a screen, without a token yet",
            sc.calls[-1] == ("monitor", None), str(sc.calls[-1]))
    h.check("the token the portal returned is remembered",
            settings["portal_restore_token"] == "token-1")
    yield from h.video_flows("the screen")

    # ----- Switching, but the picker is cancelled or fails: the share carries on
    sc.answer(None, None)
    win.sources.select_row(choose_row)
    yield h.until(picker_closed, 5, "the cancelled switch returns")
    h.check("cancel on a switch: still sharing the screen",
            win.state == "sharing" and win.shared_target == "screen")
    h.check("cancel on a switch: the screen row is selected again",
            win.sources.get_selected_row() is screen_row)
    h.check("cancel on a switch: no error is shown", "Still sharing" not in status(),
            repr(status()))

    sc.answer(None, "The portal refused that kind of source.")
    win.sources.select_row(choose_row)
    yield h.until(picker_closed, 5, "the failed switch returns")
    h.check("error on a switch: the message says the share carries on",
            "Still sharing" in status(), repr(status()))
    yield from h.video_flows("after two switches that did not happen")

    # ----- Switch to a window
    first = FakeStream(portal.WINDOW, "window-token")
    sc.answer(first)
    win.sources.select_row(choose_row)
    yield h.until(lambda: win.shared_target == "choose", 5, "the window is shared")
    h.check("the window is called 'Selected window'", win.shared_label == "Selected window",
            win.shared_label)
    h.check("the TV session survived the switch", win.state == "sharing")
    h.check("a window's token is not kept as the screen's",
            settings["portal_restore_token"] == "token-1")
    h.check("the sound row is shown for a window", be.sound_revealer.get_reveal_child())
    h.check("it lists Everything and the apps that are playing",
            list(be.sound_pids) == ["all", "Brave", "mpv"], str(list(be.sound_pids)))
    audio = worker.audio
    h.check("by default the TV gets everything", audio._pids is None)
    yield from h.video_flows("the window")

    be.sound_combo.set_active_id("Brave")
    yield h.until(lambda: audio._pids == {5070}, 5, "the audio follows Brave only")
    h.check("choosing an app sends only that app's sound", audio._pids == {5070},
            str(audio._pids))
    be.sound_combo.set_active_id("all")
    yield h.until(lambda: audio._pids is None, 5, "choosing Everything goes back to it")
    h.check("choosing Everything sends the whole mix again", audio._pids is None)

    # ----- Pick a different window while the window row is already shared
    second = FakeStream(portal.WINDOW)
    sc.answer(second)
    win.on_source_activated(win.sources, choose_row)
    yield h.until(lambda: first.closed, 5, "the first window's stream is released")
    h.check("clicking 'Choose…' again asked the portal for a window again",
            sc.calls[-1][0] == "any", str(sc.calls[-1]))
    h.check("the new window's stream stays open", not second.closed)

    # ----- Back to the screen: the remembered token is offered
    screen = FakeStream(SCREEN_TYPE, "token-2")
    sc.answer(screen)
    win.sources.select_row(screen_row)
    yield h.until(lambda: win.shared_target == "screen", 5, "the screen is shared again")
    h.check("the remembered token was offered", sc.calls[-1] == ("monitor", "token-1"),
            str(sc.calls[-1]))
    h.check("the newest token replaces it", settings["portal_restore_token"] == "token-2")
    h.check("the sound row is hidden again", not be.sound_revealer.get_reveal_child())
    h.check("the sound is everything again", audio._pids is None)
    h.check("the window's stream was released", second.closed)
    yield from h.video_flows("back on the screen")

    # ----- Stop while a picker is still open
    late = FakeStream(portal.WINDOW)
    sc.hold()
    win.sources.select_row(choose_row)
    yield h.until(lambda: be.picking, 5, "the picker is open")
    h.check("controls are locked while the picker is open",
            not win.button.get_sensitive() and not win.sources.get_sensitive())
    app.stop_sharing()
    yield h.until(lambda: win.state == "idle", 20, "the share stops")
    h.check("the TV was told goodbye", h.tv.got_bye)
    sc.held(late, None)  # the portal answers after the share is over
    yield h.until(lambda: late.closed, 5, "a grant that arrives after the share ended is closed")
    h.check("the window is usable again", not be.picking and win.button.get_sensitive())

    # ----- The desktop ends the share (e.g. its "stop sharing" indicator)
    stream = FakeStream(SCREEN_TYPE, "token-3")
    sc.answer(stream)
    win.sources.select_row(screen_row)
    win.on_button(None)
    yield from h.enter_code(win)
    yield h.until(lambda: win.state == "sharing", 30, "sharing starts again")
    h.tv.got_bye = False
    stream.on_closed()  # what portal.PortalStream does when the portal reports "Closed"
    yield h.until(lambda: win.state == "idle", 20, "the share ends when the desktop ends it")
    h.check("the TV was told goodbye", h.tv.got_bye)
    h.check("the stream was released", stream.closed)


h.run(h.make_app(), scenario, limit=150)
