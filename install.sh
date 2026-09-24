#!/usr/bin/env bash
# Install (or repair) AirMedia Share for the current user. No sudo; nothing system-wide.
#
#   ./install.sh            install or update from this folder
#   ./install.sh --repair   rebuild the Python environment (e.g. after an Ubuntu upgrade);
#                           works from the installed copy too:
#                           ~/.local/share/airmedia-share/install.sh --repair
#
# Installs:
#   ~/.local/share/airmedia-share/     app code (lib/), private venv (venv/), these scripts
#   ~/.local/bin/airmedia-share        launcher
#   ~/.local/share/applications/airmedia-share.desktop
# Settings live in ~/.config/airmedia-share/, the log in ~/.cache/airmedia-share/.
# Undo with ./uninstall.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_HOME="$DATA/airmedia-share"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/airmedia-share"
DESKTOP="$DATA/applications/airmedia-share.desktop"
REPAIR=0
[[ "${1:-}" == "--repair" ]] && REPAIR=1

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# Where the sources are: a checkout (./airmedia_share) or the installed copy (./lib).
if [[ -d "$HERE/airmedia_share" ]]; then
    SRC_PKG="$HERE/airmedia_share"
elif [[ -d "$HERE/lib/airmedia_share" ]]; then
    SRC_PKG="$HERE/lib/airmedia_share"
else
    echo "Cannot find airmedia_share next to $0" >&2; exit 1
fi

# ---- system requirements (reported, never installed with sudo behind your back)
missing=()
for pkg in python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 gir1.2-notify-0.7 \
           pulseaudio-utils zenity; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" \
        || missing+=("$pkg")
done
if ((${#missing[@]})); then
    echo "Missing system packages. Install them, then run this again:" >&2
    echo "    sudo apt install ${missing[*]}" >&2
    exit 1
fi
if [[ "${XDG_SESSION_TYPE:-x11}" == "wayland" ]]; then
    echo "Note: this session is Wayland. AirMedia Share captures the screen through X11;"
    echo "      choose “Ubuntu on Xorg” at the login screen to use it."
fi

# ---- private Python environment
# --system-site-packages only for the distro's GTK bindings (python3-gi);
# PYTHONNOUSERSITE keeps ~/.local/lib/python3.* out of it entirely.
export PYTHONNOUSERSITE=1
mkdir -p "$APP_HOME"
if ((REPAIR)) || [[ ! -x "$APP_HOME/venv/bin/python" ]] || ! "$APP_HOME/venv/bin/python" -c "" 2>/dev/null; then
    say "Creating Python environment"
    rm -rf "$APP_HOME/venv"
    /usr/bin/python3 -m venv --system-site-packages "$APP_HOME/venv"
fi
REQS="$HERE/requirements.txt"
say "Installing pinned Python packages"
"$APP_HOME/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --upgrade pip
"$APP_HOME/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --ignore-installed -r "$REQS"

# ---- app files (skip when repairing from the installed copy itself)
if [[ "$SRC_PKG" != "$APP_HOME/lib/airmedia_share" ]]; then
    say "Copying the app"
    rm -rf "$APP_HOME/lib"
    mkdir -p "$APP_HOME/lib"
    cp -r "$SRC_PKG" "$APP_HOME/lib/"
    find "$APP_HOME/lib" -name __pycache__ -prune -exec rm -rf {} +
    cp "$HERE/install.sh" "$HERE/uninstall.sh" "$REQS" "$APP_HOME/"
    mkdir -p "$APP_HOME/data" "$APP_HOME/bin"
    cp "$HERE/data/airmedia-share.desktop.in" "$APP_HOME/data/"
    cp "$HERE/bin/airmedia-share" "$APP_HOME/bin/"
fi
mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP")"
install -m 755 "$APP_HOME/bin/airmedia-share" "$BIN"
sed "s|@BIN@|$BIN|g" "$APP_HOME/data/airmedia-share.desktop.in" > "$DESKTOP"
update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null || true

# ---- settings: carry over the first version's preferences, add site TVs
PYTHONPATH="$APP_HOME/lib" "$APP_HOME/venv/bin/python" - "$HERE/site.json" <<'PY'
import os, sys
from airmedia_share.settings import Settings, merge_site_file, migrate_from_share_to_office_tv
s = Settings()
fresh = not os.path.exists(s.path)
if fresh and migrate_from_share_to_office_tv(s):
    print("==> Carried over sound settings from Share to Office TV")
site = sys.argv[1]
if os.path.exists(site):
    n = merge_site_file(s, site)
    if n:
        print(f"==> Added {n} TV(s) from site.json")
s.save()
PY

# ---- retire the first version (Share to Office TV), if present
old_found=0
for old in "$HOME/.local/bin/share-to-office-tv" \
           "$DATA/applications/share-to-office-tv.desktop" \
           "$DATA/share-to-office-tv" \
           "$HOME/.cache/share-to-office-tv" \
           "$HOME/.config/share-to-office-tv"; do
    if [[ -e "$old" ]]; then
        old_found=1
        rm -rf "$old"
    fi
done
((old_found)) && say "Removed the old Share to Office TV install (settings were carried over)"
update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null || true

say "Installed. Self-check:"
"$BIN" --check || true
echo
echo "Open “AirMedia Share” from the app menu, or run: airmedia-share"
