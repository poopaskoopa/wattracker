#ifndef AppVersion
  #error AppVersion must be supplied from pyproject.toml with /DAppVersion=x.y.z
#endif

[Setup]
AppId={{63E9478B-5D6D-4D36-9202-E8C7941AD567}
AppName=wattracker
AppVersion={#AppVersion}
AppVerName=wattracker {#AppVersion}
AppPublisher=wattracker
DefaultDirName={localappdata}\Programs\wattracker
DefaultGroupName=wattracker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=wattracker-{#AppVersion}-windows-x64-unsigned-setup
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\wattracker.exe
WizardStyle=modern

[Files]
Source: "..\dist\wattracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\wattracker.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Icons]
Name: "{group}\wattracker"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\scripts\wattracker.ps1"" -Action start -OpenBrowser"; WorkingDir: "{app}"; IconFilename: "{app}\wattracker.exe"
Name: "{group}\Uninstall wattracker"; Filename: "{uninstallexe}"

[Code]
function StopManagedWattracker(Launcher: String; var ResultCode: Integer): Boolean;
begin
  Result := Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Launcher + '" -Action stop',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode
  );
  if Result then
    Result := ResultCode = 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Launcher: String;
  ResultCode: Integer;
begin
  Result := '';
  Launcher := ExpandConstant('{app}\scripts\wattracker.ps1');
  if not FileExists(Launcher) then
    exit;

  if not StopManagedWattracker(Launcher, ResultCode) then
    Result := 'The existing managed wattracker process could not be stopped safely. Setup did not replace any application files.';
end;

function InitializeUninstall(): Boolean;
var
  Launcher: String;
  ResultCode: Integer;
begin
  Launcher := ExpandConstant('{app}\scripts\wattracker.ps1');
  if not FileExists(Launcher) then
    Result := False
  else
    Result := StopManagedWattracker(Launcher, ResultCode);
  if not Result then
    SuppressibleMsgBox(
      'The managed wattracker process could not be stopped safely. Uninstall did not remove any application files.',
      mbError, MB_OK, IDOK
    );
end;
