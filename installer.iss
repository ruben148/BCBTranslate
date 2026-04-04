; BCBTranslate Inno Setup Installer Script
; Requires Inno Setup 6+  —  https://jrsoftware.org/isinfo.php
;
; This installer:
;   - Installs / upgrades BCBTranslate into Program Files
;   - Creates a Desktop shortcut
;   - Creates a Start Menu shortcut
;   - Registers an uninstaller
;   - Preserves .env on upgrade (never overwrites credentials)

#define MyAppName      "BCBTranslate"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "BCBTranslate"
#define MyAppExeName   "BCBTranslate.exe"

[Setup]
; IMPORTANT: This AppId must stay the same across versions — it is what
; makes the installer recognise an existing installation and upgrade it.
AppId={{B7C3D1A0-9F4E-4B2A-8E6D-1A3C5F7E9B0D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=BCBTranslate_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

; Allow upgrade over existing installation without asking to uninstall first
UsePreviousAppDir=yes
CloseApplications=force
RestartApplications=yes

; Minimum Windows 10
MinVersion=10.0

; Privileges: install per-user by default (no admin needed)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

SetupIconFile=gui\resources\icons\app.ico
UninstallDisplayIcon={app}\BCBTranslate.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Shortcuts:"

[Files]
; Main application (entire PyInstaller output folder)
Source: "dist\BCBTranslate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; .env template — only copy if the user doesn't already have a .env (preserve credentials on upgrade)
Source: ".env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion
Source: ".env.example"; DestDir: "{app}"; DestName: ".env"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\gui\resources\icons\app.ico"; Tasks: desktopicon
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\gui\resources\icons\app.ico"; Tasks: startmenuicon
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; Offer to launch the app after installation
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// On upgrade: remind the user to check .env if credentials are missing
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not FileExists(ExpandConstant('{app}\.env')) then
    begin
      MsgBox('A .env.example file has been placed in the installation folder.' + #13#10 +
             'Please rename it to .env and fill in your Azure credentials.' + #13#10 + #13#10 +
             'Location: ' + ExpandConstant('{app}'),
             mbInformation, MB_OK);
    end;
  end;
end;
