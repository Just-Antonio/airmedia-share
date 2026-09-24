"""airmedia-share [--stop | --check [--deep] | --check-report | --version]"""

import logging
import os
import sys

from . import __version__


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print(__version__)
        return 0
    if "--check" in args:
        from .check import main as check
        return check()
    if "--check-report" in args:
        # For the launcher's right-click "Run self-check": no terminal to print to.
        import contextlib
        import subprocess
        from .check import main as check
        path = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                            "airmedia-share", "check.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sys.argv.append("--deep")
        with open(path, "w") as f, contextlib.redirect_stdout(f):
            status = check()
        subprocess.Popen(["xdg-open", path])
        return status

    try:
        import aiortc, av, numpy, websockets  # noqa: F401,E401
    except ImportError as e:
        # The launcher turns exit code 3 into a "run install.sh --repair" dialog.
        print(f"AirMedia Share: missing Python package ({e})", file=sys.stderr)
        return 3

    from .app import LOG_PATH
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logging.basicConfig(filename=LOG_PATH, filemode="w", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # aioice/aiortc chatter at INFO is per-packet noise.
    for noisy in ("aioice", "aiortc", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger(__name__).info("AirMedia Share %s", __version__)

    from gi.repository import GLib

    from .app import ShareApp
    # WM_CLASS, so the dock groups the window with the launcher (StartupWMClass).
    GLib.set_prgname("airmedia-share")
    status = ShareApp().run(sys.argv)
    logging.shutdown()
    # Not sys.exit: aiortc's capture threads are not daemons and would keep a
    # windowless process alive, which then swallows every later launch.
    os._exit(status)


if __name__ == "__main__":
    sys.exit(main())
