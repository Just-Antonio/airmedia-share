"""The GUI drivers in tests/gui/, as part of the normal test run.

Each driver starts the real app in its own process (the app ends its process with
os._exit, and GTK wants a main thread), so these tests only launch it and check its exit
code and output. They skip themselves where they cannot run: no PyGObject/GTK, no display,
and for the X11 driver no X11 display or Wnck. That is what happens on CI, which has none
of those. Set AIRMEDIA_SKIP_GUI_TESTS=1 to skip them anywhere else; they open a window
for about 20 seconds each.

The checks that need a real desktop and a person live in tests/gui/e2e_wayland.py and are
run by hand (see the README).
"""

import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GTK_PROBE = """
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk
ok, _ = Gtk.init_check()
sys.exit(0 if ok and Gdk.Display.get_default() is not None else 1)
"""

WNCK_PROBE = """
import sys, gi
gi.require_version("Gtk", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Gdk, Gtk, Wnck
ok, _ = Gtk.init_check()
sys.exit(0 if ok and Gdk.Display.get_default() is not None else 1)
"""


def can_run(code, env=None):
    try:
        return subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                              env={**os.environ, **(env or {})}, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def text(output):
    return output.decode(errors="replace") if isinstance(output, bytes) else (output or "")


class DriverMixin:
    module = None     # python -m <module>
    probe = GTK_PROBE  # exits 0 where the driver can run
    probe_env = {}
    unavailable = ""
    timeout = 180

    @classmethod
    def setUpClass(cls):
        if os.environ.get("AIRMEDIA_SKIP_GUI_TESTS"):
            raise unittest.SkipTest("AIRMEDIA_SKIP_GUI_TESTS is set")
        if not can_run(cls.probe, cls.probe_env):
            raise unittest.SkipTest(cls.unavailable)

    def test_every_check_passes(self):
        try:
            proc = subprocess.run([sys.executable, "-m", self.module], cwd=REPO,
                                  capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self.fail(f"{self.module} did not finish within {self.timeout} s:\n{text(e.stdout)}")
        # The driver prints a PASS/FAIL line per check; show them all when it fails.
        self.assertEqual(proc.returncode, 0,
                         f"\n{proc.stdout}\n--- stderr (tail) ---\n{proc.stderr[-3000:]}")
        self.assertIn("checks passed", proc.stdout)


class WaylandBackendGuiTest(DriverMixin, unittest.TestCase):
    """WaylandBackend in the real window, with the portal, PipeWire and audio stubbed."""

    module = "tests.gui.wayland_paths"
    unavailable = "needs PyGObject with GTK 3 and a display"


class X11BackendGuiTest(DriverMixin, unittest.TestCase):
    """X11Backend in the real window, with x11grab stubbed."""

    module = "tests.gui.x11_paths"
    probe = WNCK_PROBE
    probe_env = {"GDK_BACKEND": "x11"}
    unavailable = "needs PyGObject, Wnck and an X11 display (Xorg, Xvfb or XWayland)"
    timeout = 120


if __name__ == "__main__":
    unittest.main()
