#!/bin/bash
# Builds dist/NelyConnect-linux.tar.gz  — run on the Linux computer
# Needs: python3, python3-venv, python3-tk   (Ubuntu/Debian: sudo apt install python3-venv python3-tk)
set -e
cd "$(dirname "$0")"

if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "Missing Tk for Python. Install it first, e.g.:  sudo apt install python3-tk python3-venv"
  exit 1
fi

echo "[1/3] Preparing build environment..."
python3 -m venv --system-site-packages .buildenv
. .buildenv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install --upgrade pyinstaller tkinterdnd2 pystray pillow python-xlib

echo "[2/3] Building NelyConnect..."
python -m PyInstaller --noconfirm --clean --windowed --name NelyConnect \
  --add-data "assets:assets" --collect-all tkinterdnd2 \
  --hidden-import nelyinput --hidden-import pystray._xorg \
  --hidden-import pystray._appindicator --hidden-import pystray._gtk \
  nelyconnect.py
deactivate

echo "[3/3] Packaging..."
PKG=dist/NelyConnect-linux
rm -rf "$PKG" && mkdir -p "$PKG"
cp -r dist/NelyConnect "$PKG/app"
cp assets/icon.png "$PKG/nelyconnect.png"
cp linux/install.sh linux/uninstall.sh "$PKG/"
chmod +x "$PKG/install.sh" "$PKG/uninstall.sh"
tar -C dist -czf dist/NelyConnect-linux.tar.gz NelyConnect-linux
echo ""
echo "=========================================================="
echo " Done!  dist/NelyConnect-linux.tar.gz"
echo " To install:  tar xzf NelyConnect-linux.tar.gz && ./NelyConnect-linux/install.sh"
echo "=========================================================="
