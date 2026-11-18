#!/bin/bash
pkill -f "$HOME/.local/share/NelyConnect/app/NelyConnect" 2>/dev/null || true
rm -rf "$HOME/.local/share/NelyConnect" "$HOME/.local/bin/nelyconnect" \
       "$HOME/.local/share/applications/nelyconnect.desktop" "$HOME/.config/autostart/nelyconnect.desktop"
echo "NelyConnect removed. (Your settings in ~/.config/NelyConnect were kept.)"
