@echo off
REM Builds dist\NelyConnect-Setup.exe  (run on the Windows PC)
cd /d "%~dp0"
title Building NelyConnect installer

where py >nul 2>&1 || (echo Python is not installed. Get it from https://www.python.org/downloads/windows/ & start https://www.python.org/downloads/windows/ & pause & exit /b 1)

echo.
echo [1/3] Installing build tools...
py -m pip install --upgrade pyinstaller tkinterdnd2 pystray pillow || goto fail

echo.
echo [2/3] Building NelyConnect.exe...
py -m PyInstaller --noconfirm --clean --windowed --name NelyConnect --icon assets\icon.ico --add-data "assets;assets" --collect-all tkinterdnd2 --hidden-import nelyinput --hidden-import pystray._win32 nelyconnect.py || goto fail

echo.
echo [3/3] Building the installer...
call :findiscc
if not defined ISCC (
  echo Inno Setup not found - installing it with winget...
  winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
  call :findiscc
)
if not defined ISCC (
  echo Could not find Inno Setup. Install it from https://jrsoftware.org/isdl.php and run this again.
  start https://jrsoftware.org/isdl.php
  goto fail
)
"%ISCC%" installer\NelyConnect.iss || goto fail

echo.
echo ==========================================================
echo  Done!  Your installer is:  dist\NelyConnect-Setup.exe
echo ==========================================================
explorer dist
pause
exit /b 0

:findiscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
exit /b 0

:fail
echo.
echo Build failed - scroll up to see the error, or send a screenshot.
pause
exit /b 1
