#!/usr/bin/env bash
# Removes Labtracker from this computer. Your records in your Google Sheet are NOT touched.
set -euo pipefail
APP="$HOME/.local/share/labtracker/app"
pkill -u "$USER" -f "$APP/run.py" 2>/dev/null || true

read -r -p "Also delete your encryption keys from this computer? Only do this if you have your recovery code. (y/N) " KEYS
if [[ "$KEYS" =~ ^[Yy] ]]; then
  echo FORGET | "$HOME/.local/bin/labtracker" forget || true
fi
rm -rf "$HOME/.local/share/labtracker" "$HOME/.local/bin/labtracker" \
       "$HOME/.local/share/applications/labtracker.desktop" "$HOME/.config/autostart/labtracker.desktop"
read -r -p "Delete local settings and history (~/.config/labtracker, ~/.local/state/labtracker)? (y/N) " ST
if [[ "$ST" =~ ^[Yy] ]]; then
  rm -rf "$HOME/.config/labtracker" "$HOME/.local/state/labtracker"
fi
echo "Labtracker removed. Your Google Sheet still holds your (encrypted) records."
