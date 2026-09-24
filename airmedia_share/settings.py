"""Remembered TVs and preferences, in ~/.config/airmedia-share/settings.json.

    {
      "tvs": [{"host": "10.0.0.5", "label": "Office TV", "name": "ROOM-AM3200"}],
      "default": "10.0.0.5",
      "sound": true, "volume": 100, "follow_system_volume": false
    }

"host" is how to reach the receiver (IP or hostname, as shown on the TV's idle screen),
"label" is what you call it, "name" is what the receiver calls itself (filled in on
the first successful connection).
"""

import json
import logging
import os

log = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                          "airmedia-share")
PATH = os.path.join(CONFIG_DIR, "settings.json")
DEFAULTS = {
    "tvs": [],
    "default": None,
    "sound": True,
    "volume": 100,
    "follow_system_volume": False,
    # Escape hatches for a future receiver firmware or a slow network.
    "video_codec": None,  # None lets the TV pick (it picks VP8); or "H264"
    "max_mbps": 20,
}


class Settings(dict):
    def __init__(self, path=PATH):
        super().__init__(json.loads(json.dumps(DEFAULTS)))
        self.path = path
        try:
            with open(path) as f:
                self.update(json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.exception("unreadable settings at %s; starting fresh", path)
        self._tidy()

    def _tidy(self):
        seen, tvs = set(), []
        for tv in self["tvs"]:
            if isinstance(tv, dict) and tv.get("host") and tv["host"] not in seen:
                seen.add(tv["host"])
                tvs.append({"host": tv["host"], "label": tv.get("label") or tv["host"],
                            "name": tv.get("name")})
        self["tvs"] = tvs
        if self["default"] not in seen:
            self["default"] = tvs[0]["host"] if tvs else None

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(self), f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not save settings")

    # ----- TVs ---------------------------------------------------------------

    def tv(self, host):
        return next((tv for tv in self["tvs"] if tv["host"] == host), None)

    def add_tv(self, host, label=None):
        host = host.strip()
        for prefix in ("http://", "https://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        host = host.strip("/")
        if not self.tv(host):
            self["tvs"].append({"host": host, "label": (label or "").strip() or host,
                                "name": None})
        elif label:
            self.tv(host)["label"] = label.strip()
        if self["default"] is None:
            self["default"] = host
        return host

    def remove_tv(self, host):
        self["tvs"] = [tv for tv in self["tvs"] if tv["host"] != host]
        self._tidy()

    def rename_tv(self, host, label):
        if self.tv(host) and label.strip():
            self.tv(host)["label"] = label.strip()

    def note_receiver_name(self, host, name):
        tv = self.tv(host)
        if tv and name and tv.get("name") != name:
            tv["name"] = name
            return True
        return False


def merge_site_file(settings, path):
    """Add TVs from a site file (same shape as settings.json; used by install.sh).

    Lets a private deployment ship its rooms' TVs without putting them in the code.
    Existing entries and the user's own default win.
    """
    try:
        with open(path) as f:
            site = json.load(f)
    except (OSError, ValueError):
        log.exception("unreadable site file %s", path)
        return 0
    added = 0
    for tv in site.get("tvs", []):
        if tv.get("host") and not settings.tv(tv["host"]):
            settings.add_tv(tv["host"], tv.get("label"))
            settings.note_receiver_name(tv["host"], tv.get("name"))
            added += 1
    if site.get("default") and settings.tv(site["default"]) and added and \
            settings["default"] in (None, settings["tvs"][0]["host"]):
        settings["default"] = site["default"]
    return added


def migrate_from_share_to_office_tv(settings):
    """One-time import of sound preferences from this app's first version."""
    old = os.path.expanduser("~/.config/share-to-office-tv/settings.json")
    try:
        with open(old) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        return False
    for key in ("sound", "volume"):
        if key in previous:
            settings[key] = previous[key]
    return True
