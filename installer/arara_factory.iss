#define MyAppName "Verticliv"
#define MyAppVersion "0.20.1"
#define MyAppPublisher "Verticliv"
#define MyAppExeName "Verticliv.exe"

[Setup]
AppId={{F13BF67D-816D-45EA-8C72-75FA431AFA7B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Verticliv
DefaultGroupName=Verticliv
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer-output
OutputBaseFilename=Verticliv-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: checkedonce

[Files]
Source: "..\dist\Verticliv\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Upgrade the existing application in place; never remove its data directory.
Type: files; Name: "{app}\ARARA-Factory.exe"
Type: files; Name: "{autoprograms}\ARARA Factory.lnk"
Type: files; Name: "{autodesktop}\ARARA Factory.lnk"
Type: files; Name: "{autoprograms}\ARARA Factory — часовая запись.lnk"
Type: files; Name: "{autodesktop}\ARARA Factory — часовая запись.lnk"

[Icons]
Name: "{autoprograms}\Verticliv"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Verticliv"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить Verticliv"; Flags: nowait postinstall skipifsilent
