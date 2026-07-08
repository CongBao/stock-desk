#ifndef AppVersion
  #define AppVersion "0+unknown"
#endif
#ifndef BundleDir
  #define BundleDir "..\\..\\dist\\pyinstaller\\stock-desk"
#endif
#ifndef OutputDir
  #define OutputDir "..\\..\\dist\\installers"
#endif

[Setup]
AppId={{3AA44D44-9469-49C8-8939-32B4EE3AFE21}
AppName=Stock Desk
AppVersion={#AppVersion}
AppPublisher=CongBao
AppPublisherURL=https://github.com/CongBao/stock-desk
DefaultDirName={localappdata}\Programs\Stock Desk
DefaultGroupName=Stock Desk
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=stock-desk-{#AppVersion}-windows-x86_64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
LicenseFile=..\..\LICENSE

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Stock Desk"; Filename: "{app}\stock-desk.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Stock Desk"; Filename: "{app}\stock-desk.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\stock-desk.exe"; Description: "Launch Stock Desk"; Flags: nowait postinstall skipifsilent
