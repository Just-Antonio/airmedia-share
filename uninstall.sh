#!/usr/bin/env bash
# Remove AirMedia Share for the current user.
#
#   ./uninstall.sh           remove the app, keep your TV list and preferences
#   ./uninstall.sh --purge   also remove ~/.config/airmedia-share and ~/.cache/airmedia-share
#
# install.sh changed nothing outside your home directory, so this is the whole undo.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/airmedia-share"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/airmedia-share"

# Stop a running share first so the TV is not left waiting.
if [[ -x "$HOME/.local/bin/airmedia-share" ]] && pgrep -f "airmedia-share/venv/bin/python -m airmedia_share" >/dev/null; then
    "$HOME/.local/bin/airmedia-share" --stop 2>/dev/null || true
    sleep 2
    pkill -f "airmedia-share/venv/bin/python -m airmedia_share" 2>/dev/null || true
fi

rm -f "$HOME/.local/bin/airmedia-share" "$DATA/applications/airmedia-share.desktop"
rm -rf "$DATA/airmedia-share"
update-desktop-database "$DATA/applications" 2>/dev/null || true
echo "Removed AirMedia Share."

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$CONFIG" "$CACHE"
    echo "Removed settings and logs."
else
    echo "Kept your TV list and preferences in $CONFIG (use --purge to remove them too)."
fi
