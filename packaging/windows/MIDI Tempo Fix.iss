; MIDI Tempo Fix for Windows: the installer (Inno Setup 6).
; Built by build_app.py on Windows:  ISCC /DAppVersion=x.y.z /DSourceDir="dist\MIDI Tempo Fix" "MIDI Tempo Fix.iss"
;
; Installs for the current person only, into their own Programs folder, so it never asks
; for an administrator password. Adds a Start menu entry and an entry in Settings, Apps
; for uninstalling. Uninstalling leaves settings alone.
; WinSparkle runs this same installer quietly to update (see midi_tempo/winsparkle.py).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\MIDI Tempo Fix"
#endif

[Setup]
; Never change AppId: it's how Windows knows a new version replaces the old one.
AppId={{23249596-9b90-4c39-aac7-e23f232e5a3b}
AppName=MIDI Tempo Fix
AppVersion={#AppVersion}
AppVerName=MIDI Tempo Fix {#AppVersion}
AppPublisher=Matthew Whitaker
AppPublisherURL=https://github.com/Matthew2244/midi-tempo-fix
AppSupportURL=https://github.com/Matthew2244/midi-tempo-fix/issues
DefaultDirName={localappdata}\Programs\MIDI Tempo Fix
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=MIDI-Tempo-Fix-Setup
UninstallDisplayIcon={app}\MIDI Tempo Fix.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Close a running copy before replacing it, and open it again afterwards.
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Put MIDI Tempo Fix on the desktop"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; A new version may drop files the old one had; start clean inside the app folder only.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\MIDI Tempo Fix"; Filename: "{app}\MIDI Tempo Fix.exe"
Name: "{autodesktop}\MIDI Tempo Fix"; Filename: "{app}\MIDI Tempo Fix.exe"; Tasks: desktopicon

[Run]
; After a normal install: offer to open it. After a quiet update (WinSparkle): open it again.
Filename: "{app}\MIDI Tempo Fix.exe"; Description: "Open MIDI Tempo Fix now"; Flags: nowait postinstall skipifsilent
Filename: "{app}\MIDI Tempo Fix.exe"; Flags: nowait; Check: Relaunch

[Code]
// WinSparkle passes /relaunch=1 when it runs this quietly to update, so the app comes back
// by itself. A quiet install without it (a test machine, an administrator) opens nothing.
function Relaunch(): Boolean;
begin
  Result := WizardSilent() and (ExpandConstant('{param:relaunch|0}') = '1');
end;
