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

from . import __version__

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
        if "x11grab" not in av.formats_available:
            good = line(BAD, "PyAV x11grab", "this PyAV build cannot capture X11") and good
    except Exception:
        pass
    return good


def check_desktop():
    good = True
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    if session == "x11":
        line(OK, "X11 session")
    else:
        good = line(BAD, f"session is {session}",
                    "screen capture needs X11: pick “Ubuntu on Xorg” at login") and good
    try:
        import gi
        for ns, ver in (("Gtk", "3.0"), ("Wnck", "3.0"), ("Notify", "0.7")):
            gi.require_version(ns, ver)
            __import__("gi.repository", fromlist=[ns])
        line(OK, "GTK, Wnck, libnotify bindings")
    except Exception as e:
        good = line(BAD, "GTK/Wnck/libnotify bindings",
                    f"{e}; sudo apt install python3-gi gir1.2-wnck-3.0 gir1.2-notify-0.7") and good
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
