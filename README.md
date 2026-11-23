# NelyConnect 2.3.0

One app for Windows, macOS and Linux. It lives in the **system tray**: the Windows notification area,
the Mac menu bar, or the Linux panel. Each feature can be turned on or off:

- **One keyboard & mouse:** move the mouse past the screen edge and it controls the other computer. You don't need Deskflow anymore.
- **Drag files to the edge:** they're copied to the other computer, with a progress bar.
- **Copy & paste text** between the two computers.
- **Shared folders:** pick disks, folders or files the other computer may browse, open, download from and upload to.
- **Phone (iPhone & Android):** scan a QR code to send photos, videos and files from the phone to either computer,
  or download files from the computer. You don't need to install an app on the phone.

It uses the **Ethernet cable first** and switches to **Wi-Fi** automatically.


## ⬇ Download (for friends)

Go to the **Releases** page of this repository (right side of the GitHub page) and download:

| Computer | File |
|---|---|
| Windows 10/11 | `NelyConnect-Setup.exe` |
| Mac with Intel processor | `NelyConnect-Mac-Intel.dmg` |
| Mac with Apple M1/M2/M3/M4 | `NelyConnect-Mac-AppleSilicon.dmg` |
| Linux (X11) | `NelyConnect-linux.tar.gz` |

Install NelyConnect on **both** computers, then follow "Use it" below. On a Mac, open it the first time with
right-click → **Open**, and turn on **Accessibility** for NelyConnect (System Settings → Privacy & Security).

## 🛠 For the developer: publishing a new version

Every version tag builds all the installers on GitHub automatically and publishes them as a Release:

```
git add -A
git commit -m "Describe what changed"
git tag v2.3.0
git push && git push --tags
```

Watch progress in the **Actions** tab. It takes about 10–15 minutes, and then the Release shows up with all
four files. To build without publishing, open Actions → **Build installers** → **Run workflow**. The files then appear
as downloadable "artifacts" on that run.

Tests: `python tests/test_transfer.py`, `python tests/test_share.py`, `python tests/test_input.py`, `python tests/test_phone.py`.
`tests/test_linux_input.py` needs an X11 display.

Project layout: `nelyconnect.py` (app, file transfer, sharing, tray), `nelyinput.py` (keyboard & mouse), `nelyphone.py` (phone page),
`assets/` (logo/icons), `installer/` (Windows installer), `linux/` (Linux install scripts),
`build_*` (local build scripts), `.github/workflows/build.yml` (automatic builds).

You can also build the installers yourself on each computer with the `build_*` scripts, as described below.

---

## 1. Remove Deskflow

NelyConnect does Deskflow's job now, and running both at once would fight over the mouse.
Quit Deskflow and uninstall it on both computers:
- PC: Settings → Apps → Deskflow → Uninstall
- Mac: drag Deskflow from Applications to the Trash

## 2. Build the installers yourself (optional, if you don't use the Releases)

- **PC:** needs Python from python.org. Double-click **`build_windows_installer.bat`**, which makes **`dist\NelyConnect-Setup.exe`**.
- **iMac:** needs Python **3.13** from python.org on Intel Macs (Python 3.14 uses Tk 9, which drag & drop doesn't support on Intel yet). Right-click **`build_mac_installer.command`** → Open, which makes **`dist/NelyConnect.dmg`**.
- **Linux:** run `sudo apt install python3-venv python3-tk` once (Ubuntu/Debian/Mint), then
  `./build_linux_installer.sh`, which makes **`dist/NelyConnect-linux.tar.gz`**.

## 3. Install

**PC:** run `NelyConnect-Setup.exe` and tick **"Start NelyConnect automatically when I sign in"**.
The installer also opens the firewall ports that NelyConnect uses.

**iMac:** open `NelyConnect.dmg`, drag **NelyConnect** into **Applications**, and open it from there.
- The first time, right-click it → **Open** → Open.
- Click **Allow** if macOS asks about incoming network connections.
- **Accessibility permission (required for keyboard & mouse):** NelyConnect asks for it on first start.
  You can also go to System Settings → Privacy & Security → **Accessibility** → turn on **NelyConnect**.
  Then quit NelyConnect and open it again.
  *Every time you rebuild and reinstall the Mac app, remove NelyConnect from that list with the "–" button and add it again.*
- Start at login: System Settings → General → Login Items → **+** → NelyConnect.

## 4. Use it

**Keyboard & mouse**
- Move the mouse past the **right edge of the PC screen**. The cursor appears on the iMac, and everything you type goes to the iMac.
- To come back, move the mouse past the **left edge of the iMac screen**.
- **Scroll Lock** key:
  - While you're on the iMac, it jumps straight back to the PC.
  - While you're on the PC, it locks the mouse to the PC screen. Press it again to unlock.
- **Ctrl** on the PC keyboard works as **⌘ Command** on the Mac, so Ctrl+C / Ctrl+V / Ctrl+Z work as usual.
  The Windows key works as Mac Control. You can change this in Settings on the Mac.
- If the cable is unplugged or the Mac stops answering, you get the mouse back on the PC within a few seconds.
- While you control the Mac, the PC's cursor waits in the middle of the PC screen.

**Files:** drag files or folders to the edge and hold them there. Holding the mouse button keeps the cursor on the current screen.
The blue line opens into **"Drop to send to iMac"**. Let go, and the files arrive in **Desktop → NelyConnect Inbox**.

**Copy & paste:** copy text on one computer and paste it on the other.

**Tray icon** (Windows notification area / Mac menu bar / Linux panel). Click it for:
- the connection status, Status…, Browse…, Open inbox
- ☑ Share keyboard & mouse · ☑ Share copy & paste · ☑ Share my folders
- Settings…, Quit

On Windows, if the icon is hidden, click **^** next to the clock and drag it onto the taskbar.

**Phone (iPhone / Android)**
1. On the computer, open the tray icon → **Phone…** and tick **"Allow my phone to connect"**. A QR code appears.
2. Scan it with the phone's camera. The phone must be on the **same Wi-Fi**. The NelyConnect page opens in the browser.
   Tip: on iPhone, tap Share → **Add to Home Screen** to get a NelyConnect icon.
3. On the phone:
   - **Choose photos, videos or files** sends them to this computer, or to the **other computer**. Pick which one with the switch at the top.
   - **From PC / From iMac** lists files the computer sent to the phone. Tap **Download**.
   - **Shared folders** lets you browse and download the computer's shared items. Folders download as .zip.
4. On the computer, use **Add files… / Add folder…** in the Phone window, or drag files onto it, to send things to the phone.

Security: phone access is **off** until you turn it on. It only works on your own network, and only with the secret
code built into the QR code. **New code** locks out every phone that used the old one.

**Right-click the blue line** also opens the same menu:
- ☑ **Share keyboard & mouse**
- ☑ **Share copy & paste (text)**
- Settings…, Status / connection, Open inbox, Quit

**Shared folders**
1. On the computer that has the files, go to Settings → **Shared folders**.
   Tick **"Let the iMac (or PC) browse…"**, then use **Add folder…**, **Add files…** or **Add disk…**.
   Each item is **Read only** by default. Double-click an item, or use **Read / write**, to allow uploads and new folders.
   You can set an optional **password**. If you do, type the same one on both computers.
2. On the other computer, right-click the blue line → **Browse iMac…** (or **Browse PC…**).
   - Double-click a folder to open it. Double-click a file to open it, which copies it temporarily.
   - **Download** copies the selection to your NelyConnect Inbox. **Download to…** lets you choose the folder.
   - **Upload…**, or drag files onto the window, copies into the current folder (Read & write shares only).
   - **New folder**, **⬆ Up**, **⟳ Refresh**
3. Only computers in the Connection list can browse. Sharing is **off** until you turn it on.

## Settings (right-click the line → Settings…)

- **Other computer → IP addresses:** Add / Edit / Remove / Move up / Move down / Test all. The list is tried from top to bottom.
- **Files:** which edge sends files, the folder for received files, whether to open that folder when files arrive, and copy & paste on/off.
- Tabs: **Connection · Files & clipboard · Keyboard & mouse · Shared folders**
- **Keyboard & mouse:** on/off.
  On the Mac there are extra options: Ctrl as ⌘, reverse scrolling, **mouse speed**, and **keyboard type**. Use ISO for Spanish / Latin American keyboards and ANSI for US keyboards. If the key left of "1" and the **< >** key come out swapped, change the keyboard type.

Changes apply as soon as you click **Save**.

## Connection (already set up for your network)

| Priority | PC → iMac | iMac → PC | Link |
|---|---|---|---|
| 1 | 10.77.0.2 | 10.77.0.1 | Ethernet, fixed address (optional) |
| 2 | 169.254.93.113 | 169.254.18.27 | Ethernet, automatic address |
| 3 | 10.0.10.125 | 10.0.10.156 | Wi-Fi |
| 4 | iMac-de-Nelson.local | — | Wi-Fi, by name |

The 169.254 addresses can change after a restart. To make the cable permanent:
- On the PC: Settings → Network → Ethernet → IP assignment → Manual → `10.77.0.1`, mask `255.255.255.0`, no gateway.
- On the Mac: System Settings → Network → Ethernet → Details → TCP/IP → Manually → `10.77.0.2`, mask `255.255.255.0`.

Ports: 24860 (files, copy & paste), 24870 (keyboard & mouse) and 24880 (phone page).

## Linux notes

- Everything works on Linux. Keyboard & mouse **control from the PC** needs an **X11 (Xorg)** session.
  On Ubuntu, choose "Ubuntu on Xorg" on the login screen. It doesn't work under Wayland yet.
- A Linux computer can be *controlled* by the PC. It can't be the computer that controls others yet.
- Tray icon: KDE, Cinnamon, XFCE and MATE show it automatically. On GNOME, install the
  "AppIndicator and KStatusNotifierItem Support" extension.
- Settings and log: `~/.config/NelyConnect/`

## Troubleshooting

- **The mouse doesn't cross over to the Mac:** check Status (right-click the line). It should say "Keyboard & mouse: ready". Make sure NelyConnect is running on the Mac and has Accessibility permission. Also make sure Scroll Lock hasn't locked the mouse to the PC.
- **On the Mac, the cursor moves but clicks and typing do nothing:** the Accessibility permission is missing or out of date. Remove NelyConnect from the Accessibility list, add it again, and restart the app.
- **Log files:** PC `%APPDATA%\NelyConnect\`, Mac `~/Library/Application Support/NelyConnect/`, Linux `~/.config/NelyConnect/`.
  Look at `nelyconnect.log`, plus `crash.log` if the app closed by itself.
- **The app closes right after opening:** run it from Terminal to see the error:
  `/Applications/NelyConnect.app/Contents/MacOS/NelyConnect`
