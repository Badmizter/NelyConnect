; NelyConnect - Windows installer (Inno Setup 6)
; Built automatically by build_windows_installer.bat

#define AppName "NelyConnect"
#ifndef AppVersion
  #define AppVersion "2.3.0"
#endif
#define AppExe "NelyConnect.exe"

[Setup]
AppId={{FFA0CE69-1587-46DF-8289-5DC121FC7633}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Nelson Maita
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardImageFile=wizard.bmp,wizard_2x.bmp
WizardSmallImageFile=wizard_small.bmp,wizard_small_2x.bmp
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=NelyConnect-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start NelyConnect automatically when I sign in"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\NelyConnect\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
; open the firewall for NelyConnect (files, copy & paste, keyboard & mouse) on every network type
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""NelyConnect"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""NelyConnect"" dir=in action=allow protocol=TCP localport=24860,24870,24880 profile=any"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Deskflow (NelyConnect)"""; Flags: runhidden
Filename: "{app}\{#AppExe}"; Description: "Start NelyConnect now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""NelyConnect"""; Flags: runhidden; RunOnceId: "FwApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Deskflow (NelyConnect)"""; Flags: runhidden; RunOnceId: "FwDeskflow"
