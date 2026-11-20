"""Screen and window capture on Wayland: the xdg-desktop-portal ScreenCast API.

A Wayland client cannot read other windows or the screen on its own. It asks the
compositor's portal backend (xdg-desktop-portal-kde, -gnome, ...), the user picks a
screen or window in the compositor's own dialog, and the portal hands back a PipeWire
node to read frames from (see media.PipeWireVideoSource).

Every portal call here is asynchronous and runs on the GLib main loop (the GTK thread):

    CreateSession -> SelectSources -> Start (the user picks) -> OpenPipeWireRemote

Each of the first three answers through a Response signal on a Request object whose
path we can predict, so we subscribe to it *before* making the call. `gi` is imported
lazily so the pure helpers stay usable (and testable) without it.
"""

import logging
import os
import secrets

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"

# SelectSources "types" bits, and the source_type the portal reports back.
MONITOR, WINDOW = 1, 2
# SelectSources "cursor_mode" bits; "embedded" draws the pointer into the frames.
CURSOR_EMBEDDED = 2
# "persist_mode" 2: remember the choice until the user revokes it, via a restore token.
PERSIST_UNTIL_REVOKED = 2


class PortalError(Exception):
    pass


def request_path(unique_name, token):
    """Where the portal will publish the Request for a call made with this token.

    ":1.42" becomes "1_42": the portal derives the path from the caller's bus name.
    """
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{OBJECT_PATH}/request/{sender}/{token}"


def parse_start_results(results):
    """(node_id, source_type, size or None, restore_token or None) from a Start response."""
    streams = results.get("streams") or []
    if not streams:
        raise PortalError("The portal did not share anything.")
    node_id, props = streams[0]
    size = props.get("size")
    return (node_id, props.get("source_type", MONITOR),
            tuple(size) if size else None, results.get("restore_token") or None)


def _token(prefix="airmedia"):
    return f"{prefix}_{secrets.token_hex(4)}"


def _variant(value):
    from gi.repository import GLib
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("u", value)
    return GLib.Variant("s", value)


def _options(**values):
    return {key: _variant(value) for key, value in values.items() if value is not None}


def portal_info():
    """{"version", "source_types", "cursor_modes"} of the ScreenCast portal, or None."""
    try:
        from gi.repository import Gio, GLib
    except ImportError:
        return None
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
            BUS_NAME, OBJECT_PATH, SCREENCAST, None)
    except GLib.Error:
        return None
    info = {}
    for name, key in (("version", "version"), ("AvailableSourceTypes", "source_types"),
                      ("AvailableCursorModes", "cursor_modes")):
        value = proxy.get_cached_property(name)
        info[key] = value.unpack() if value is not None else 0
    return info if info["version"] else None


class PortalStream:
    """One granted screen or window: a PipeWire node, and the session that keeps it alive."""

    def __init__(self, portal, session, fd, node_id, source_type, size, restore_token):
        self._portal = portal
        self.session = session
        self.fd = fd
        self.node_id = node_id
        self.source_type = source_type
        self.size = size
        self.restore_token = restore_token
        # Called (on the GTK thread) if the user or compositor ends the share, such as
        # through the desktop's "stop sharing" indicator.
        self.on_closed = None
        self._closed = False

    def close(self):
        """End the session. Safe from any thread and safe to repeat."""
        if self._closed:
            return
        self._closed = True
        from gi.repository import GLib
        GLib.idle_add(self._portal.close_session, self.session)
        try:
            os.close(self.fd)
        except OSError:
            pass


class ScreenCast:
    def __init__(self):
        from gi.repository import Gio, GLib
        self._Gio, self._GLib = Gio, GLib
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._closed_subs = {}  # session handle -> signal subscription id

    # ----- plumbing ----------------------------------------------------------

    def _request(self, method, leading, options, on_response, on_error):
        """Call a portal method that answers through its Request's Response signal.

        on_response(code, results) with code 0 = ok, 1 = the user cancelled, 2 = failed;
        on_error(message) if the call itself fails (no portal, bad arguments).
        """
        Gio, GLib = self._Gio, self._GLib
        token = _token()
        path = request_path(self._bus.get_unique_name(), token)
        sig = "(" + "".join("o" if isinstance(a, str) and a.startswith("/") else "s"
                            for a in leading) + "a{sv})"
        params = GLib.Variant(sig, (*leading, _options(handle_token=token, **options)))
        sub = None

        def on_signal(_bus, _sender, _path, _iface, _name, parameters, *_):
            self._bus.signal_unsubscribe(sub)
            code, results = parameters.unpack()
            on_response(code, results)

        def on_reply(bus, res):
            try:
                bus.call_finish(res)
            except GLib.Error as e:
                bus.signal_unsubscribe(sub)
                on_error(e.message)

        sub = self._bus.signal_subscribe(BUS_NAME, REQUEST, "Response", path, None,
                                         Gio.DBusSignalFlags.NONE, on_signal)
        self._bus.call(BUS_NAME, OBJECT_PATH, SCREENCAST, method, params, None,
                       Gio.DBusCallFlags.NONE, -1, None, on_reply)

    def close_session(self, session):
        Gio = self._Gio
        sub = self._closed_subs.pop(session, None)
        if sub is not None:
            self._bus.signal_unsubscribe(sub)
        self._bus.call(BUS_NAME, session, SESSION, "Close", None, None,
                       Gio.DBusCallFlags.NONE, -1, None, None)
        return False  # for GLib.idle_add

    # ----- the flow ----------------------------------------------------------

    def select(self, kind, restore_token, on_done):
        """Ask for a screen ("monitor") or a screen-or-window ("any").

        With a restore_token the portal may skip its picker and reuse the earlier
        choice. on_done(stream, error): a PortalStream; or (None, None) if the user
        cancelled; or (None, message) if something failed.
        """
        types = MONITOR if kind == "monitor" else MONITOR | WINDOW
        modes = (portal_info() or {}).get("cursor_modes", 0)
        cursor = CURSOR_EMBEDDED if modes & CURSOR_EMBEDDED else None
        state = {"session": None}

        def fail(message):
            if state["session"]:
                self.close_session(state["session"])
            on_done(None, message)

        def created(code, results):
            if code != 0:
                return fail("The portal could not start a screen-sharing session.")
            session = state["session"] = results["session_handle"]
            self._request("SelectSources", [session], {
                "types": types, "multiple": False, "cursor_mode": cursor,
                "persist_mode": PERSIST_UNTIL_REVOKED,
                "restore_token": restore_token}, selected, fail)

        def selected(code, _results):
            if code != 0:
                return fail("The portal refused that kind of source.")
            self._request("Start", [state["session"], ""], {}, started, fail)

        def started(code, results):
            if code == 1:
                self.close_session(state["session"])
                return on_done(None, None)
            if code != 0:
                return fail("The portal could not start the capture.")
            try:
                node_id, source_type, size, token = parse_start_results(results)
            except PortalError as e:
                return fail(str(e))
            self._open_remote(state["session"], node_id, source_type, size, token,
                              on_done, fail)

        self._request("CreateSession", [], {"session_handle_token": _token("session")},
                      created, fail)

    def _open_remote(self, session, node_id, source_type, size, token, on_done, fail):
        Gio, GLib = self._Gio, self._GLib

        def reply(bus, res):
            try:
                result, fds = bus.call_with_unix_fd_list_finish(res)
                fd = fds.get(result.unpack()[0])
            except GLib.Error as e:
                return fail(e.message)
            stream = PortalStream(self, session, fd, node_id, source_type, size, token)

            def closed(*_):
                self._closed_subs.pop(session, None)
                if not stream._closed and stream.on_closed:
                    stream.on_closed()

            self._closed_subs[session] = self._bus.signal_subscribe(
                BUS_NAME, SESSION, "Closed", session, None, Gio.DBusSignalFlags.NONE, closed)
            log.info("portal: node %s, source type %s, size %s", node_id, source_type, size)
            on_done(stream, None)

        self._bus.call_with_unix_fd_list(
            BUS_NAME, OBJECT_PATH, SCREENCAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})), GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE, -1, None, None, reply)
