; Inno Setup script for the Verba Windows installer.
; Built in CI after PyInstaller:  iscc /DAppVersion=1.0.0 packaging\verba.iss
; Expects the PyInstaller output at dist\verba\ (one-dir build).
; UTF-8 with BOM: without it Inno reads this file in the system codepage, and
; the wizard page below is written in three languages.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A2F52B4-9D14-4E7C-B1F3-3C9A51E2D604}}
AppName=Verba
AppVersion={#AppVersion}
AppPublisher=Verba
DefaultDirName={autopf}\Verba
UsePreviousAppDir=yes
Uninstallable=yes
DefaultGroupName=Verba
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Verba-Setup-{#AppVersion}
SetupIconFile=verba.ico
UninstallDisplayIcon={app}\Verba.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
; A running Verba is closed by the installer, never restarted by it: the
; in-app update (backend/verba/services/updates.py) runs Setup silently, and
; the [Run] entry below is what brings the app back — exactly once.
RestartApplications=no
RestartIfNeededByRun=yes
PrivilegesRequiredOverridesAllowed=dialog
; The data directory is asked for on a page of its own (see [Code]) and
; defaults to %LOCALAPPDATA%\Verba. It survives uninstall and updates, and a
; silent update keeps the answer the install before it was given.

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
german.DataDirCaption=Datenordner auswählen
german.DataDirDescription=Wohin soll Verba seine Daten legen?
german.DataDirBody=Aufnahmen, Transkripte, die Datenbank, die heruntergeladenen Modelle und die nachinstallierten Komponenten kommen in diesen Ordner. Er bleibt bei einem Update und bei der Deinstallation erhalten.%n%nEin vorhandener Datenordner wird nicht verschoben: wer hier einen anderen wählt, fängt dort neu an.
english.DataDirCaption=Choose the data folder
english.DataDirDescription=Where should Verba keep its data?
english.DataDirBody=Recordings, transcripts, the database, the downloaded models and the components installed later on go into this folder. It survives an update and uninstalling.%n%nAn existing data folder is not moved: picking a different one starts afresh there.
russian.DataDirCaption=Выбор папки данных
russian.DataDirDescription=Где Verba должна хранить свои данные?
russian.DataDirBody=Записи, расшифровки, база данных, загруженные модели и доустанавливаемые компоненты будут лежать в этой папке. Она сохраняется при обновлении и при удалении программы.%n%nСуществующая папка данных не переносится: если выбрать другую, работа начнётся там с нуля.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Dirs]
; The data directory is created here, not by the app on its first start: a
; folder an elevated setup creates outside the profile (C:\Verba) inherits
; read-only rights for normal users, and Verba would then fail to write the
; very packages it installs into it. `users-modify` is what makes the chosen
; directory usable by whoever runs the program afterwards.
Name: "{code:DataDir}"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "..\dist\verba\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[InstallDelete]
; Remove files from older one-dir bundles before copying the new version.
; Runtime data is stored outside {app} and is therefore preserved.
Type: filesandordirs; Name: "{app}\*"

[Icons]
Name: "{group}\Verba"; Filename: "{app}\Verba.exe"
Name: "{autodesktop}\Verba"; Filename: "{app}\Verba.exe"; Tasks: desktopicon

[Run]
; `runasoriginaluser` on both: Setup writes into Program Files and is
; therefore elevated, and a program it starts would inherit that token. Verba
; installs its feature packages itself (setup_check.py) — as an elevated
; process it writes them into the user's own data directory as somebody else,
; and the next ordinary start cannot read them any more. The app has no
; business running as administrator; this is where that is decided.
Filename: "{app}\Verba.exe"; Description: "Verba starten"; Flags: nowait postinstall skipifsilent runasoriginaluser
; A silent run is an update started from inside Verba: Setup closed the
; running app to replace its files, so this brings it back on its own —
; without anyone clicking anything.
Filename: "{app}\Verba.exe"; Flags: nowait runasoriginaluser; Check: WizardSilent

[Code]
var
  DataDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    ExpandConstant('{cm:DataDirCaption}'),
    ExpandConstant('{cm:DataDirDescription}'),
    ExpandConstant('{cm:DataDirBody}'),
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := GetPreviousData('DataDir', ExpandConstant('{localappdata}\Verba'));
end;

function ChosenDataDir: String;
begin
  // The page where there is one, the answer of the install before otherwise.
  // An update runs Setup silently and must never quietly move an installation
  // back to %LOCALAPPDATA% - [InstallDelete] wipes the program directory
  // first, so the marker is written from scratch every time and this is the
  // only thing that remembers. A brace comment would end at the first }, so
  // a constant like {app} may not appear in one.
  if Assigned(DataDirPage) then
    Result := Trim(DataDirPage.Values[0])
  else
    Result := '';
  if Result = '' then
    Result := GetPreviousData('DataDir', ExpandConstant('{localappdata}\Verba'));
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', ChosenDataDir);
end;

function DataDir(Param: String): String;
begin
  Result := ChosenDataDir;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Written after the program files. config.py reads it as DATA_DIR_MARKER. }
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\data-dir.txt'), ChosenDataDir, False);
end;
