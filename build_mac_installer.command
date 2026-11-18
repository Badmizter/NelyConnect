#!/bin/bash
# Builds dist/NelyConnect.dmg (and NelyConnect.app)  — run on the iMac
cd "$(dirname "$0")"
set -e
pause() { [ -n "$CI" ] || read -p "Press Enter to close"; }
trap 'echo ""; echo "Build failed — scroll up to see the error, or send a screenshot."; pause' ERR

# Pick a python.org Python that works with drag & drop.
# Intel Macs need Tcl/Tk 8 (the drag & drop add-on has no Tk 9 build for Intel yet).
ARCH=$(uname -m)
PY=""
for v in 3.13 3.12 3.11 3.10 3.14 Current; do
  c="/Library/Frameworks/Python.framework/Versions/$v/bin/python3"
  [ -x "$c" ] || continue
  tkv=$("$c" -c 'import tkinter; print(int(tkinter.TkVersion))' 2>/dev/null)
  [ -n "$tkv" ] || continue
  if [ "$tkv" = "8" ] || [ "$ARCH" = "arm64" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo ""
  echo "This Mac needs Python 3.13 from python.org."
  echo "(Newer Python 3.14 uses Tk 9, and drag & drop doesn't support Tk 9 on Intel Macs yet.)"
  echo ""
  echo "On the page that opens, choose the latest Python 3.13.x -> 'macOS 64-bit universal2 installer'."
  echo "Install it, then run this script again. Your other Python can stay installed."
  [ -n "$CI" ] || open "https://www.python.org/downloads/macos/"
  pause; exit 1
fi
echo "Using $PY  (Tk $("$PY" -c 'import tkinter; print(tkinter.TkVersion)'))"

echo "[1/4] Installing build tools..."
"$PY" -m pip install --upgrade pyinstaller tkinterdnd2 pystray pillow pyobjc-framework-Quartz

echo "[2/4] Building NelyConnect.app..."
"$PY" -m PyInstaller --noconfirm --clean --windowed --name NelyConnect \
  --icon assets/icon.icns --add-data "assets:assets" --collect-all tkinterdnd2 --hidden-import nelyinput --hidden-import pystray._darwin \
  --osx-bundle-identifier com.nelsonmaita.nelyconnect nelyconnect.py

APP="dist/NelyConnect.app"
PLIST="$APP/Contents/Info.plist"
# runs quietly in the background: no Dock icon
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 2.2.2" "$PLIST" 2>/dev/null || true
codesign --force --deep --sign - "$APP"

echo "[3/4] Building NelyConnect.dmg..."
rm -rf dist/dmg dist/NelyConnect.dmg
mkdir dist/dmg
cp -R "$APP" dist/dmg/
ln -s /Applications dist/dmg/Applications
hdiutil create -volname "NelyConnect" -srcfolder dist/dmg -ov -format UDZO dist/NelyConnect.dmg >/dev/null
rm -rf dist/dmg

echo "[4/4] Done!"
echo ""
echo "=========================================================="
echo " Your installer is: dist/NelyConnect.dmg"
echo " Open it and drag NelyConnect into Applications."
echo "=========================================================="
[ -n "$CI" ] || open dist
pause
