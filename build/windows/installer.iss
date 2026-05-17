; Inno Setup script for Describely.
; Pass the project root via /DProjectRoot=<path>; defaults to the .iss
; directory's grand-parent so the script also works when iscc is invoked
; from the repo root.

#ifndef ProjectRoot
  #define ProjectRoot SourcePath + "..\.."
#endif

#define AppName "Describely"
#define AppShortName "Describely"
#define AppVersion "1.0.0"
#define AppPublisher "Describely"
#define AppURL "https://describely.app"
#define AppExeName "Describely.exe"
#define BundleDir ProjectRoot + "\dist\Describely"
#define OutDir ProjectRoot + "\build\windows\out"
#define LicenseFile ProjectRoot + "\TERMS.md"
#define IconFile ProjectRoot + "\build\assets\app.ico"

[Setup]
AppId={{F2D5B1C8-7A3E-4D9F-A1E6-9C4D8E5B7F12}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={localappdata}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir={#OutDir}
OutputBaseFilename={#AppShortName}-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
LicenseFile={#LicenseFile}
SetupLogging=yes
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (c) 2026 Describely contributors
#if FileExists(IconFile)
SetupIconFile={#IconFile}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "contextmenu"; Description: "Add ""Create transcription"" to the Explorer right-click menu for video and audio files"; GroupDescription: "Explorer integration:"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#LicenseFile}"; DestDir: "{app}"; DestName: "TERMS.md"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Terms of Use"; Filename: "{app}\TERMS.md"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Right-click handler for every supported media extension. Per-user
; (HKCU), so no admin rights needed. The exe receives "%1" as its first
; argv element; main_window picks it up.
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mkv\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mkv\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mkv\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mov\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mov\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mov\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.avi\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.avi\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.avi\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.webm\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.webm\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.webm\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.m4v\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.m4v\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.m4v\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp3\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp3\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp3\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.wav\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.wav\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.wav\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.m4a\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.m4a\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.m4a\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.flac\shell\DescribelyCreateTranscription"; ValueType: string; ValueData: "Create transcription"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.flac\shell\DescribelyCreateTranscription"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExeName}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.flac\shell\DescribelyCreateTranscription\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
