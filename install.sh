#!/usr/bin/env bash
# Labtracker installer for Linux Mint 22 (Cinnamon). Also works on Ubuntu 24.04+.
# Run it from the downloaded folder as your normal user:   ./install.sh
# Re-run it any time to update.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Please run this as your normal user (not with sudo). It asks for your password when needed."
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/.local/share/labtracker/app"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/labtracker"
PKGS="python3 python3-gi gir1.2-gtk-3.0 gir1.2-xapp-1.0 gir1.2-secret-1 python3-cryptography xprintidle"

echo "==> Installing system packages from your distribution (password may be requested)"
# shellcheck disable=SC2086
if ! sudo apt-get install -y $PKGS; then
  sudo apt-get update
  # shellcheck disable=SC2086
  sudo apt-get install -y $PKGS
fi

WAS_RUNNING=0
if pgrep -u "$USER" -f "$APP/run.py" >/dev/null 2>&1; then
  WAS_RUNNING=1
  pkill -u "$USER" -f "$APP/run.py" || true
fi

echo "==> Copying Labtracker to $APP"
rm -rf "$APP"
mkdir -p "$APP" "$BIN" "$HOME/.local/share/applications" "$HOME/.config/autostart"
cp -r "$HERE/agent/labtracker" "$HERE/agent/icons" "$HERE/agent/run.py" "$APP/"
find "$APP" -name '__pycache__' -prune -exec rm -rf {} +
chmod -R u=rwX,go=rX "$APP"

# -I = isolated mode: ignores PYTHON* environment variables and the current folder,
# so nothing outside the installed files can inject code into Labtracker.
cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec /usr/bin/python3 -I "$APP/run.py" "\$@"
EOF
chmod 755 "$LAUNCHER"

DESKTOP="[Desktop Entry]
Type=Application
Name=Labtracker
Comment=Clock in and out, with idle detection
Exec=$LAUNCHER run
Icon=$APP/icons/labtracker-in.svg
Terminal=false
Categories=Office;Utility;"
printf '%s\n' "$DESKTOP" > "$HOME/.local/share/applications/labtracker.desktop"
printf '%s\nX-GNOME-Autostart-enabled=true\n' "$DESKTOP" > "$HOME/.config/autostart/labtracker.desktop"

echo
echo "==> Installed."
if [ "$WAS_RUNNING" -eq 1 ]; then
  nohup "$LAUNCHER" run >/dev/null 2>&1 &
  echo "Labtracker was running and has been restarted with the new version."
else
  echo "Next step (first install only):   $LAUNCHER setup"
fi
