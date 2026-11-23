#!/bin/bash
# Installs NelyConnect for the current user (no root needed)
set -e
cd "$(dirname "$0")"
DEST="$HOME/.local/share/NelyConnect"
APPS="$HOME/.local/share/applications"
mkdir -p "$DEST" "$APPS" "$HOME/.local/bin"
pkill -f "$DEST/app/NelyConnect" 2>/dev/null || true
rm -rf "$DEST/app"
cp -r app "$DEST/app"
cp nelyconnect.png "$DEST/nelyconnect.png"
ln -sf "$DEST/app/NelyConnect" "$HOME/.local/bin/nelyconnect"

cat > "$APPS/nelyconnect.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=NelyConnect
Comment=One keyboard & mouse, files and copy & paste between your computers
Exec=$DEST/app/NelyConnect
Icon=$DEST/nelyconnect.png
Terminal=false
Categories=Utility;Network;
DESK

read -r -p "Start NelyConnect automatically when you log in? [Y/n] " ans
if [[ ! "$ans" =~ ^[Nn] ]]; then
  mkdir -p "$HOME/.config/autostart"
  cp "$APPS/nelyconnect.desktop" "$HOME/.config/autostart/nelyconnect.desktop"
fi

if command -v ufw >/dev/null && sudo -n true 2>/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 24860,24870,24880/tcp >/dev/null && echo "Firewall: opened ports 24860, 24870 and 24880."
elif command -v ufw >/dev/null; then
  echo "If your firewall is on, run:  sudo ufw allow 24860,24870,24880/tcp"
fi

echo "Installed. Find NelyConnect in your applications menu, or run: nelyconnect"
nohup "$DEST/app/NelyConnect" >/dev/null 2>&1 &
