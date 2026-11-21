"""The Wayland backend on a real desktop: real portal, real PipeWire, real pactl.

Run by hand, never as part of the automatic suite: it needs a Wayland session with
xdg-desktop-portal, and a person to answer the desktop's share dialog. The TV is still a
stand-in (tests/fake_receiver.py on loopback), so no receiver is needed.

    python -m tests.gui.e2e_wayland        (on NixOS: nix-shell --run "python -m tests.gui.e2e_wayland")

What happens, and what you do:
  1. It shares the entire screen. The first time (or if the desktop forgot) a dialog asks
     what to share: pick a SCREEN. The portal's restore token is kept in
     ~/.cache/airmedia-share/e2e/, so later runs skip this dialog where the desktop allows.
  2. It switches to "Choose a screen or window…". The dialog now offers WINDOWS ONLY:
     pick one. For the "Sound from" checks, pick a window of an app that is playing sound.
  3. It switches back to the screen (no dialog), then stops.

The app window is locked while this runs: leave it alone and only answer the dialog.
Your own settings are never touched; the app runs with its own config and application id.
"""

import os
import sys

if os.environ.get("XDG_SESSION_TYPE") != "wayland":
    sys.exit("This check is for a real Wayland session (XDG_SESSION_TYPE=wayland).")

from tests.gui.harness import Harness  # noqa: E402

CACHE = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
h = Harness("e2e", session="wayland", config_dir=os.path.join(CACHE, "airmedia-share", "e2e"),
            keep_token=True)

HUMAN = 180  # seconds a person gets to answer a dialog


def scenario(h, app):
    win = app.window
    be = win.backend
    worker = app.worker
    screen_row, choose_row = win.sources.get_children()
    requests = []

    # Tell the person what to do at each dialog, and make the "Choose…" dialog offer
    # windows only, so a screen can't be picked by mistake.
    real_select = be.screencast.select

    def select(kind, token, on_done):
        if kind == "any":
            kind = "window"
        requests.append(kind)
        if kind == "window":
            h.say(">>> The desktop is asking: pick a WINDOW (an app that is playing sound, "
                  "for the 'Sound from' checks).")
        elif token:
            h.say(">>> Asking for the screen with the remembered token (no dialog expected; "
                  "if one appears, pick a SCREEN).")
        else:
            h.say(">>> The desktop is asking: pick a SCREEN.")
        return real_select(kind, token, on_done)

    be.screencast.select = select

    h.check("the Wayland backend is in use, with two rows",
            type(be).__name__ == "WaylandBackend"
            and (screen_row.target, choose_row.target) == ("screen", "choose"))
    h.check("the sound row is hidden for the screen row", not be.sound_revealer.get_reveal_child())

    # ----- the screen
    win.on_button(None)
    yield from h.enter_code(win, timeout=HUMAN)
    yield h.until(lambda: win.state == "sharing", 30, "sharing starts")
    h.check("sharing the entire screen", win.shared_label == "Entire screen", win.shared_label)
    h.check("the desktop's restore token is kept", bool(win.settings["portal_restore_token"]))
    yield from h.video_flows("the screen", seconds=4, minimum=30)

    # ----- a window
    win.sources.select_row(choose_row)
    yield h.until(lambda: win.shared_target == "choose", HUMAN, "a window is chosen and shared")
    h.check("the TV session survived the switch", win.state == "sharing")
    h.check("the picked source is a window", be.shared_is_window, win.shared_label)
    h.check("the sound row is shown for a window", be.sound_revealer.get_reveal_child())
    yield from h.video_flows("the window", seconds=4, minimum=30)

    audio = worker.audio
    apps = [name for name in be.sound_pids if name != "all"]
    h.say(f"'Sound from' lists: {list(be.sound_pids)}")
    if not apps:
        h.skip("'Sound from' checks", "no app is playing sound; start some and run it again")
    else:
        app_name = apps[0]
        be.sound_combo.set_active_id(app_name)
        yield h.until(lambda: audio._pids == be.sound_pids[app_name], 5,
                      f"the audio follows {app_name}")
        h.check(f"choosing {app_name} sends only its sound", audio._pids == be.sound_pids[app_name],
                str(audio._pids))
        yield h.until(lambda: list(audio._streams) and
                      all(k.startswith("stream") for k in list(audio._streams)), 8,
                      f"parec monitors {app_name}'s own stream")
        h.check("parec listens to that app's stream, not the whole mix",
                all(k.startswith("stream") for k in list(audio._streams)),
                str(list(audio._streams)))
        be.sound_combo.set_active_id("all")
        yield h.until(lambda: audio._pids is None and "all" in list(audio._streams), 8,
                      "choosing Everything goes back to the whole mix")
        h.check("choosing Everything sends the whole mix again", audio._pids is None)

    # ----- back to the screen
    win.sources.select_row(screen_row)
    yield h.until(lambda: win.shared_target == "screen", HUMAN, "the screen is shared again")
    h.check("back on the entire screen", win.shared_label == "Entire screen")
    h.check("the sound row is hidden again", not be.sound_revealer.get_reveal_child())
    yield from h.video_flows("back on the screen", seconds=4, minimum=30)
    h.check("exactly three portal requests: screen, window, screen",
            requests == ["monitor", "window", "monitor"], str(requests))

    app.stop_sharing()
    yield h.until(lambda: win.state == "idle", 20, "the share stops")
    h.check("the TV was told goodbye", h.tv.got_bye)
    h.check("no picker is left open", not be.picking)


h.run(h.make_app(), scenario, limit=600)
