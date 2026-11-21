"""Shared plumbing for the GUI drivers in this folder.

A driver runs the real GTK app (app.ShareApp) against tests/fake_receiver.py (a stand-in
TV on loopback) and scripts it. The script is a plain generator: every `yield` waits for
something, so a scenario reads top to bottom:

    def scenario(h, app):
        win = app.window
        win.on_button(None)                                  # click Share
        yield from h.enter_code(win)
        yield h.until(lambda: win.state == "sharing", 30, "sharing starts")
        yield from h.video_flows("the screen")

Drivers run in their own process (tests/test_gui.py starts them, or run one by hand with
`python -m tests.gui.<name>`), because the app ends its process with os._exit.

Create the Harness *before* importing anything from airmedia_share: where settings and
logs go is read from the environment at import time. The app runs with a throwaway
config and its own application id, so it never touches your real settings or hands over
to a copy of AirMedia Share that is already open.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

APP_ID = "io.github.just_antonio.AirMediaShare.GuiTest"


class Harness:
    def __init__(self, name, *, session, env=None, config_dir=None, keep_token=False,
                 settings=None, lock=True):
        """session: "wayland" or "x11", what the app should believe it is running in.

        config_dir: where the throwaway config and cache live (default: a fresh temp
        folder). keep_token: keep the portal restore token already saved there.
        lock: make the window insensitive so a stray click cannot disturb the script.
        """
        self.name, self.lock = name, lock
        self.t0 = time.monotonic()
        self.results, self.skipped = [], 0
        self._finishing = False
        self.config_dir = config_dir or tempfile.mkdtemp(prefix=f"airmedia-{name}-")
        os.environ.update(env or {})
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.config_dir, "config")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.config_dir, "cache")
        os.environ["XDG_SESSION_TYPE"] = session

        logging.basicConfig(level=logging.WARNING, stream=sys.stdout,
                            format="    log %(name)s: %(message)s")
        for noisy in ("aioice", "aiortc", "websockets"):
            logging.getLogger(noisy).setLevel(logging.CRITICAL)

        self._write_settings(settings or {}, keep_token)
        self._start_tv()

    # ----- setup -----------------------------------------------------------------

    def _write_settings(self, extra, keep_token):
        folder = os.path.join(os.environ["XDG_CONFIG_HOME"], "airmedia-share")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "settings.json")
        data = {"tvs": [{"host": "127.0.0.1", "label": "Fake TV", "name": None}],
                "default": "127.0.0.1", **extra}
        if keep_token:
            try:
                with open(path) as f:
                    data["portal_restore_token"] = json.load(f).get("portal_restore_token")
            except (OSError, ValueError):
                pass
        with open(path, "w") as f:
            json.dump(data, f)

    def _start_tv(self):
        from tests.fake_receiver import FakeReceiver
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        self.tv = asyncio.run_coroutine_threadsafe(FakeReceiver(code="1234").start(),
                                                   loop).result()
        from airmedia_share import receiver
        receiver.PORT = self.tv.port

    # ----- reporting -------------------------------------------------------------

    def say(self, message):
        print(f"[{self.name} {time.monotonic() - self.t0:5.1f}s] {message}", flush=True)

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.results.append(ok)
        self.say(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        return ok

    def skip(self, name, reason):
        """Something that could not be checked here. Reported, but not a failure."""
        self.skipped += 1
        self.say(f"SKIP  {name}  ({reason})")

    def finish(self):
        """Print the summary and end the process, without leaving anything running.

        A share that is still going (a failed check can end the run mid-share) is
        stopped first, so the app's helper processes (parec, pactl) stop with it instead
        of being orphaned by os._exit.
        """
        if self._finishing:
            return
        self._finishing = True
        passed = sum(self.results)
        extra = f", {self.skipped} skipped" if self.skipped else ""
        self.say(f"{passed}/{len(self.results)} checks passed{extra}")
        sys.stdout.flush()
        self._code = 0 if self.results and all(self.results) else 1
        win = self._app.window if getattr(self, "_app", None) else None
        if win is not None and win.state != "idle":
            from gi.repository import GLib
            self._app.stop_sharing()
            GLib.timeout_add(100, self._exit_when_stopped, time.monotonic())
        else:
            os._exit(self._code)

    def _exit_when_stopped(self, since):
        if self._app.window.state == "idle" or time.monotonic() - since > 10:
            os._exit(self._code)
        return True

    # ----- waiting (what a scenario yields) ----------------------------------------

    @staticmethod
    def sleep(seconds):
        return ("sleep", seconds)

    @staticmethod
    def until(predicate, timeout, what):
        """Wait until predicate() is true; a timeout fails the run (there is no point going on)."""
        return ("until", predicate, timeout, what)

    # ----- steps most scenarios share (use with `yield from`) ------------------------

    def enter_code(self, win, code="1234", timeout=60):
        yield self.until(lambda: win.code_future is not None, timeout,
                         "the TV asks for its code")
        win.code_entry.set_text(code)
        win.on_code_submit()

    def video_flows(self, what, seconds=3, minimum=20):
        before = self.tv.video_frames
        yield self.sleep(seconds)
        got = self.tv.video_frames - before
        self.check(f"video reaches the TV: {what}", got >= minimum,
                   f"{got} frames in {seconds} s")

    # ----- running ---------------------------------------------------------------

    def make_app(self):
        from airmedia_share.app import ShareApp
        app = ShareApp()
        app.set_application_id(APP_ID)
        return app

    def run(self, app, scenario, limit=120):
        """Run the app's main loop and drive `scenario(harness, app)` from it. Never returns."""
        from gi.repository import GLib
        self._app, self._scenario, self._gen = app, scenario, None
        GLib.timeout_add(100, self._tick)
        GLib.timeout_add_seconds(limit, self._watchdog, limit)
        sys.exit(app.run(["airmedia-share"]))

    def _watchdog(self, limit):
        self.check(f"finished within {limit} s", False, "overall timeout")
        self.finish()

    def _advance(self, value):
        try:
            self._wait = self._gen.send(value)
            self._since = time.monotonic()
        except StopIteration:
            self.finish()

    def _tick(self):
        if self._finishing:
            return True  # winding down: _exit_when_stopped ends the process
        try:
            win = self._app.window
            if win is None:
                return True  # the window is still being created
            if self._gen is None:
                if self.lock:
                    win.set_sensitive(False)
                self._gen = self._scenario(self, self._app)
                self._advance(None)
                return True
            kind, *args = self._wait
            elapsed = time.monotonic() - self._since
            if kind == "sleep":
                if elapsed >= args[0]:
                    self._advance(None)
            else:
                predicate, timeout, what = args
                if predicate():
                    self._advance(True)
                elif elapsed > timeout:
                    self.check(f"within {timeout} s: {what}", False, "timed out")
                    self.finish()
        except Exception:
            traceback.print_exc()
            self.check("the scenario ran without an exception", False)
            self.finish()
        return True
