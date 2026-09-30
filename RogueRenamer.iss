#define MyAppName "Rogue Renamer"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Rogue Systems"
#define MyAppExeName "RogueRenamer.exe"

[Setup]
AppId={{6E10D677-B694-4A96-84D9-01F899CB381B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Rogue Renamer
DefaultGroupName=Rogue Renamer
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=RogueRenamer-Setup-{#MyAppVersion}
SetupIconFile=assets\rogue_renamer.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\RogueRenamer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Rogue Renamer"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Rogue Renamer"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Rogue Renamer"; Flags: nowait postinstall skipifsilent