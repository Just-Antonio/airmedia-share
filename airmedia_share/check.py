"""`airmedia-share --check`: say what works and what does not, without sharing anything.

Meant for after an Ubuntu upgrade or a receiver firmware update: every line is a
pass/fail with the fix next to it.
"""

import asyncio
import json
import os
import shutil
import socket
import sys

from . import __version__, backends

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def line(status, what, detail=""):
    print(f"[{status}] {what}" + (f" — {detail}" if detail else ""))
    return status != BAD


def check_python():
    good = True
    for mod, why in (("aiortc", "WebRTC"), ("av", "capture and encoding"),
                     ("websockets", "talking to the TV"), ("numpy", "audio and letterboxing")):
        try:
            m = __import__(mod)
            line(OK, f"python: {mod} {getattr(m, '__version__', '')}", why)
        except Exception as e:
            good = line(BAD, f"python: {mod}", f"{e}; run install.sh --repair") and good
    try:
        import av
        if backends.session_type() == "x11" and "x11grab" not in av.formats_available:
            good = line(BAD, "PyAV x11grab", "this PyAV build cannot capture X11") and good
    except Exception:
        pass
    return good


def check_wayland():
    """The Wayland capture path: the ScreenCast portal, and GStreamer's PipeWire source."""
    from .backends import pipewire, portal
    info = portal.portal_info()
    if info is None:
        return line(BAD, "ScreenCast portal",
                    "xdg-desktop-portal is not answering; install it and the backend for "
                    "your desktop (xdg-desktop-portal-kde, -gnome or -wlr)")
    good = True
    line(OK, f"ScreenCast portal v{info['version']}")
    if not info["source_types"] & portal.WINDOW:
        line(WARN, "portal cannot share single windows", "only whole screens will work")
    if info["version"] < 4:
        line(WARN, "portal has no restore tokens",
             "the desktop's picker will open for the screen every time")
    problem = pipewire.capture_problem()
    if problem:
        good = line(BAD, "GStreamer PipeWire capture", problem) and good
    else:
        line(OK, "GStreamer pipewiresrc")
    return good


def check_desktop():
    good = True
    wayland = backends.session_type() == "wayland"
    line(OK, "Wayland session" if wayland else "X11 session",
         os.environ.get("XDG_CURRENT_DESKTOP", ""))
    try:
        import gi
        # Wnck lists X11 windows only; on Wayland the portal's picker replaces it.
        for ns, ver in (("Gtk", "3.0"), ("Notify", "0.7")) + (() if wayland else (("Wnck", "3.0"),)):
            gi.require_version(ns, ver)
            __import__("gi.repository", fromlist=[ns])
        line(OK, "GTK, libnotify bindings" if wayland else "GTK, Wnck, libnotify bindings")
    except Exception as e:
        good = line(BAD, "GTK/libnotify bindings" if wayland else "GTK/Wnck/libnotify bindings",
                    f"{e}; sudo apt install python3-gi gir1.2-notify-0.7"
                    + ("" if wayland else " gir1.2-wnck-3.0")) and good
    if wayland:
        good = check_wayland() and good
    for tool in ("parec", "pactl"):
        if shutil.which(tool):
            line(OK, f"{tool}")
        else:
            good = line(BAD, tool, "sudo apt install pulseaudio-utils") and good
    return good


async def probe(host):
    """Open the session handshake and close it again (the TV may flash a code)."""
    import websockets

    from .receiver import API, CLIENT_VERSION, PORT, SESSION_ID, USER_AGENT
    async with websockets.connect(f"ws://{host}:{PORT}",
                                  subprotocols=["com.splashtop.webrtc2"],
                                  open_timeout=5, ping_interval=None) as ws:
        await ws.send(json.dumps({"id": "getsession", "type": API, "req": {
            "product": "smx", "version": CLIENT_VERSION, "platform": "linux", "oem": None,
            "uuid": "0" * 32, "useragent": USER_AGENT, "value": SESSION_ID}}))
        return json.loads(await asyncio.wait_for(ws.recv(), 5)).get("ack", {})


def check_tvs(deep):
    from .receiver import PORT
    from .settings import Settings
    settings = Settings()
    if not settings["tvs"]:
        return line(WARN, "no TVs saved yet", "add one in the app")
    good = True
    for tv in settings["tvs"]:
        what = f"TV {tv['label']} ({tv['host']})" + \
               (" [default]" if tv["host"] == settings["default"] else "")
        try:
            socket.create_connection((tv["host"], PORT), timeout=3).close()
        except OSError as e:
            good = line(BAD, what, f"port {PORT} unreachable: {e}") and good
            continue
        if not deep:
            line(OK, what, f"port {PORT} answers")
            continue
        try:
            ack = asyncio.run(probe(tv["host"]))
            line(OK, what, f"receiver {ack.get('name')}, wants {ack.get('res')}, "
                           f"code {'required' if str(ack.get('security')) == '1' else 'off'}")
        except Exception as e:
            good = line(BAD, what, f"handshake failed ({e!r}); firmware change?") and good
    return good


def main():
    print(f"AirMedia Share {__version__} self-check (python {sys.version.split()[0]})")
    deep = "--deep" in sys.argv
    results = [check_python(), check_desktop(), check_tvs(deep)]
    if not deep:
        print("(add --deep to also run the receiver handshake; the TV may briefly show a code)")
    return 0 if all(results) else 1
