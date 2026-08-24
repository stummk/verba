; Inno Setup script for the Verba Windows installer.
; Built in CI after PyInstaller:  iscc /DAppVersion=1.0.0 packaging\verba.iss
; Expects the PyInstaller output at dist\verba\ (one-dir build).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A2F52B4-9D14-4E7C-B1F3-3C9A51E2D604}}
AppName=Verba
AppVersion={#AppVersion}
AppPublisher=Verba
DefaultDirName={autopf}\Verba
DefaultGroupName=Verba
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Verba-Setup-{#AppVersion}
SetupIconFile=verba.ico
UninstallDisplayIcon={app}\Verba.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
; User data lives in %LOCALAPPDATA%\Verba and survives uninstall/updates.

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\verba\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\Verba"; Filename: "{app}\Verba.exe"
Name: "{autodesktop}\Verba"; Filename: "{app}\Verba.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Verba.exe"; Description: "Verba starten"; Flags: nowait postinstall skipifsilent
