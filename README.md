# AirMedia Share

Present a Linux desktop on a Crestron AirMedia receiver: the whole screen or one
window, with sound, from a small desktop app. No browser, no browser extension.

Crestron ships AirMedia apps for Windows, macOS, iOS and Android, and a Chrome
extension for Chromebooks, but nothing for Linux. This app speaks the same protocol as
that extension (a WebSocket on port 7300, then WebRTC) using
[aiortc](https://github.com/aiortc/aiortc). See [docs/protocol.md](docs/protocol.md).

Verified with an **AM-3200** (AirMedia Series 3) from **Ubuntu 22.04** on X11.

Not affiliated with or endorsed by Crestron. AirMedia is a Crestron trademark.

## What it does

- Shares the **entire screen** or **one window**. You can switch between them while
  sharing, without a new code.
- Sends **sound**: everything the computer plays for the whole screen, or only the
  shared app's sound for a window. There's a volume slider for the level sent to the
  TV. It's independent of your speakers, and the TV's remote still works. Optionally,
  the keyboard volume keys can control it too.
- Remembers **several TVs** by name, with one default, so the usual room is one
  click away.
- Keeps a steady 1920×1080 picture: windows are letterboxed, so switching sources or
  resizing a window doesn't make the TV re-sync.

Tabs: pick the browser window. The TV shows whichever tab is in front, with its sound.

## Requirements

- Ubuntu 22.04 or similar, logged in to an **X11** session ("Ubuntu on Xorg" on the
  login screen's gear menu). Screen capture here uses X11, so Wayland won't work.
- PulseAudio or PipeWire-Pulse (`parec`, `pactl`)
- These system packages (the installer checks for them and prints the command if
  any are missing):

  ```
  sudo apt install python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 \
                   gir1.2-notify-0.7 pulseaudio-utils zenity
  ```

- The computer must be able to reach the receiver on TCP 7300, plus UDP for the
  video. On a campus network that usually means being on the same network as the
  TV, as its idle screen suggests.

## Install

```
git clone https://github.com/Just-Antonio/airmedia-share.git
cd airmedia-share
./install.sh
```

Everything goes into your home directory. Nothing is installed system-wide and sudo is
never used:

| Path | What |
|---|---|
| `~/.local/share/airmedia-share/` | the app (`lib/`), its private Python environment (`venv/`), and copies of `install.sh`/`uninstall.sh` |
| `~/.local/bin/airmedia-share` | launcher |
| `~/.local/share/applications/airmedia-share.desktop` | menu entry: "AirMedia Share" |
| `~/.config/airmedia-share/settings.json` | your TVs and preferences |
| `~/.cache/airmedia-share/sender.log` | log of the last run |

Python packages are pinned in `requirements.txt` and live in the app's own venv. The
launcher also ignores `~/.local/lib/python3.*`, so upgrading other Python software
can't break the app.

### Site TVs (optional)

To pre-load a room's TVs, put a `site.json` next to `install.sh` before installing:

```json
{
  "tvs": [{"host": "10.0.0.5", "label": "Office TV"}],
  "default": "10.0.0.5"
}
```

The installer adds any TVs you don't already have and never overwrites your names or
default. `site.json` is git-ignored.

## Use

1. Open **AirMedia Share** from the app menu (searching "air" or "share" finds it).
   Pin it to the dock if you like.
2. Pick the **TV**. Your default is preselected.
3. Pick **what to share**, then click **Share**.
4. The TV shows a 4-digit code. Type it in the box that appears and press Enter.
   A new code comes up for every connection, and it only appears once you've
   clicked Share.
5. While sharing, click another item in the list to switch. Use the switch and slider
   for sound.
6. To stop, do any of these:
   - click **Stop sharing**
   - close the window
   - right-click the launcher in the dock and choose **Stop sharing**
   - run `airmedia-share --stop`

### Managing TVs

Use the **⋮** menu next to the TV picker:

- **Add a TV…**: the address is on the TV's idle screen ("To present visit:
  http://…"). An IP address or hostname works.
- **Rename this TV…**, **Make this TV the default**, **Remove this TV**.
- **Computer volume keys also control the TV**: off by default, so the TV's level
  doesn't depend on your speakers.

The name the receiver reports about itself (e.g. `ROOM-AM3200`) is saved on the first
connection and shown in the TV picker's tooltip.

## Troubleshooting

Start with the self-check. Run it from the menu (right-click the launcher → **Run
self-check**) or in a terminal:

```
airmedia-share --check          # packages, X11, audio tools, can each TV be reached
airmedia-share --check --deep   # also does the receiver handshake (the TV briefly shows a code)
```

| Symptom | Likely cause and fix |
|---|---|
| A dialog says the Python environment needs a repair | Usually after an Ubuntu upgrade replaced the system Python. Run `~/.local/share/airmedia-share/install.sh --repair`. |
| "Could not reach …" | TV off, or this computer is on a different network or VLAN. `--check` shows whether port 7300 answers. |
| Connected, but the TV shows nothing | Another presenter may have the screen, or a receiver firmware update changed something. Run `--check --deep`, then look at the log. |
| No code appears on the TV | The code only appears after you click Share. If it still doesn't appear, the receiver may have its code turned off. The app then connects without asking. |
| "The TV rejected that code" | Codes change for every connection. Use the one shown after clicking Share. You get three tries. |
| No sound on the TV | Check the Sound switch and slider. For a window, only that app's sound is sent: is it the app that's actually playing? Some apps play sound from a helper process, which is also matched. |
| Picture is soft, or stutters on slow Wi-Fi | In `settings.json`, set `"max_mbps"` lower (e.g. 8) or higher (up to ~24). Try `"video_codec": "H264"` if a firmware update stops accepting VP8. |
| App won't open, and nothing happens | Check `~/.cache/airmedia-share/sender.log`. A run from a terminal (`airmedia-share`) prints errors too. |
| Wayland session | Log out, choose "Ubuntu on Xorg" from the login screen's gear menu. |

## Update, repair, uninstall

```
git pull && ./install.sh                       # update
~/.local/share/airmedia-share/install.sh --repair    # rebuild the Python environment
./uninstall.sh                                 # remove the app, keep your TVs and settings
./uninstall.sh --purge                         # remove everything, settings and logs too
```

`uninstall.sh` is also installed at `~/.local/share/airmedia-share/uninstall.sh`, so
you don't need the clone to remove it. The installer changes nothing outside your home
directory, so uninstalling undoes it completely. It doesn't touch any AirMedia Chrome
extension you may have installed yourself; remove that from Chrome's extensions page if
you no longer want it.

## Development

```
python3 -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v   # includes a loopback test against a fake receiver
PYTHONPATH=. python -m airmedia_share          # run from the checkout
```

`tests/fake_receiver.py` plays the TV's side of the protocol on aiortc, and enforces the
receiver behaviors that were learned the hard way (see
[docs/protocol.md](docs/protocol.md)). If a dependency upgrade breaks sharing, it
should fail there first. Bump the pins in `requirements.txt` deliberately, then test
against a real receiver.

## License

MIT. See [LICENSE](LICENSE).
