; ============================================================
; ATANA Agents Dispatcher — Inno Setup installer script
; ============================================================
; Prerequisites:
;   - Inno Setup 6+  (https://jrsoftware.org/isinfo.php)
;   - nssm.exe (x64) placed next to this .iss file
;   - atana_dispatcher.exe built via:  python build/build.py --version X.X.X
;     and placed next to this .iss file
;
; Build the installer:
;   ISCC installer\atana_setup.iss /DMyAppVersion=2.0.0
; Output:
;   installer\Output\AtanaAgents_Setup_v2.0.0.exe
; ============================================================

#ifndef MyAppVersion
  #define MyAppVersion "2.0.0"
#endif

#define MyAppName      "ATANA Agents"
#define MyAppPublisher "ATANA"
#define MyAppExeName   "atana_dispatcher.exe"
#define MyServiceName  "AtanaDispatcher"
#define MyInstallDir   "{autopf}\ATANA"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={#MyInstallDir}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=AtanaAgents_Setup_v{#MyAppVersion}
SetupIconFile=atana.ico
UninstallDisplayIcon={app}\atana_dispatcher.exe
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#MyAppName} v{#MyAppVersion}
SetupLogging=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
; Main executable (agents bundled inside) — built by build/build.py into dist/exe/
Source: "..\dist\exe\atana_dispatcher.exe"; DestDir: "{app}"; Flags: ignoreversion

; Panel (Overview + Configuración) — separate exe, built by tools/build_panel.py
; into the same dist/exe/. Openable independently of the tray/dispatcher service.
Source: "..\dist\exe\atana_panel.exe"; DestDir: "{app}"; Flags: ignoreversion

; NSSM service wrapper — committed to installer/
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion

; Registers the Scheduled Task that supervises the tray icon (see [Run] below)
Source: "register_tray_task.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Reinicia el tray a mano (soporte) sin esperar al watchdog ni reinstalar
Source: "restart_tray.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Default config — copied only if config.json does not exist yet
Source: "..\config.example.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
; Create logs dir with write permission for all users so the tray (non-admin) can write logs
Name: "{app}\logs"; Permissions: users-modify

[Icons]
; Panel — abre independientemente del tray/servicio (ver ui/panel_main.py)
Name: "{group}\ATANA Panel"; Filename: "{app}\atana_panel.exe"
Name: "{autodesktop}\ATANA Panel"; Filename: "{app}\atana_panel.exe"; Tasks: desktopicon

; Reiniciar tray — utilidad de soporte, ver installer/restart_tray.ps1
Name: "{group}\ATANA - Reiniciar Tray"; Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\restart_tray.ps1"""; \
    WorkingDir: "{app}"; IconFilename: "{app}\atana_dispatcher.exe"

[Tasks]
Name: "desktopicon"; Description: "Crear acceso directo al Panel en el Escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[Run]
; --- UPGRADE: detener servicio antes de reemplazar el exe ---
Filename: "{app}\nssm.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden waituntilterminated; Check: IsServiceInstalled

; --- UPGRADE: matar proceso tray (servicio ya parado, solo queda el tray en sesion de usuario) ---
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; Check: IsServiceInstalled

; --- FRESH: registrar servicio ---
Filename: "{app}\nssm.exe"; Parameters: "install {#MyServiceName} ""{app}\{#MyAppExeName}"""; Flags: runhidden waituntilterminated; Check: IsFirstInstall

; --- SIEMPRE: configurar servicio (idempotente — aplica en fresh install Y upgrades) ---
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppDirectory ""{app}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} DisplayName ""{#MyAppName}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} Description ""Servicio de descarga automatica de liquidaciones - ATANA"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppEnvironmentExtra ""ATANA_SERVICE=1"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} Start SERVICE_AUTO_START"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppRestartDelay 15000"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppStdout ""{app}\logs\service.log"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppStderr ""{app}\logs\service_err.log"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppStdoutCreationDisposition 4"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} AppStderrCreationDisposition 4"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#MyServiceName} DependOnService {code:GetSqlServiceName}"; Flags: runhidden waituntilterminated; Check: HasSqlService

; --- FRESH: wizard de configuracion DB — bloqueante, garantiza que config.json este listo antes de continuar ---
Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup-db"; Flags: waituntilterminated skipifsilent; Check: IsFirstInstall

; --- SIEMPRE: iniciar servicio (config.json ya tiene datos correctos) ---
Filename: "{app}\nssm.exe"; Parameters: "start {#MyServiceName}"; Flags: runhidden waituntilterminated

; --- UPGRADE: limpiar la entrada HKCU\Run de instaladores viejos (superada por la Scheduled Task de abajo) ---
Filename: "reg.exe"; Parameters: "delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v AtanaTray /f"; Flags: runhidden; Check: IsServiceInstalled

; --- SIEMPRE: registrar (o reparar) la Scheduled Task que supervisa el tray — arranca "at logon" y
;     se re-dispara cada 3 min si el tray murio (ver installer/register_tray_task.ps1) ---
Filename: "powershell.exe"; Parameters: "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\register_tray_task.ps1"" -ExePath ""{app}\{#MyAppExeName}"""; Flags: runhidden waituntilterminated

; --- SIEMPRE: lanzar tray ahora mismo (la Scheduled Task recien registrada no dispara "at logon"
;     retroactivamente para la sesion ya activa; en upgrade reemplaza el viejo) ---
Filename: "{app}\{#MyAppExeName}"; Parameters: "--tray"; Flags: nowait

[UninstallRun]
; 1. Detener el servicio
Filename: "{app}\nssm.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden waituntilterminated; RunOnceId: "StopService"
; 2. Matar el proceso tray (corre en sesion de usuario, no lo maneja NSSM)
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillTray"
; 3. Eliminar la Scheduled Task que supervisaba el tray
Filename: "powershell.exe"; Parameters: "-NonInteractive -WindowStyle Hidden -Command ""Unregister-ScheduledTask -TaskName 'AtanaTrayWatchdog' -Confirm:$false -ErrorAction SilentlyContinue"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveTrayTask"
; 4. Breve pausa para que el SO libere los handles antes de eliminar el servicio
Filename: "powershell.exe"; Parameters: "-WindowStyle Hidden -NonInteractive -Command ""Start-Sleep -Seconds 3"""; Flags: runhidden waituntilterminated; RunOnceId: "WaitHandles"
; 5. Eliminar el servicio de Windows
Filename: "{app}\nssm.exe"; Parameters: "remove {#MyServiceName} confirm"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"

[UninstallDelete]
; Logs generados durante la operacion
Type: filesandordirs; Name: "{app}\logs"
; Configuracion del cliente (generada en el wizard de instalacion)
Type: files; Name: "{app}\config.json"
; Browsers de Playwright instalados por el dispatcher en el primer arranque
Type: filesandordirs; Name: "{localappdata}\atana"
; Archivos temporales de actualizacion automatica
Type: files; Name: "{tmp}\atana_upd_*.ps1"
Type: files; Name: "{tmp}\atana_update_*.exe"

[Code]
// Captured BEFORE any [Run] entries execute — not re-evaluated lazily.
// If we used nssm.exe or registry checks inside the Check: functions,
// they would see the service as installed after the "nssm install" [Run] entry
// runs, making IsFirstInstall() always return false for subsequent entries.
var
  _WasServiceInstalled: Boolean;
  _SqlServiceName: String;

function DetectSqlServiceName(): String;
// Returns the Windows service name for the first SQL Server instance found.
// Checks default instance (MSSQLSERVER) first, then named instances via registry.
// Returns empty string if SQL Server is not installed.
var
  Names: TArrayOfString;
  i: Integer;
  InstanceName, ServiceName: String;
begin
  // Default instance
  if RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\MSSQLSERVER') then
  begin
    Result := 'MSSQLSERVER';
    Exit;
  end;

  // Named instances — registered under Instance Names\SQL
  if RegGetValueNames(HKLM,
    'SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL',
    Names) then
  begin
    for i := 0 to GetArrayLength(Names) - 1 do
    begin
      InstanceName := Names[i];
      ServiceName  := 'MSSQL$' + InstanceName;
      if RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\' + ServiceName) then
      begin
        Result := ServiceName;
        Exit;
      end;
    end;
  end;

  Result := '';
end;

procedure InitializeWizard();
begin
  // Capture state BEFORE any [Run] entries fire — not re-evaluated lazily
  _WasServiceInstalled := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\{#MyServiceName}');
  _SqlServiceName      := DetectSqlServiceName();
end;

function IsServiceInstalled(): Boolean;
begin
  Result := _WasServiceInstalled;
end;

function IsFirstInstall(): Boolean;
begin
  Result := not _WasServiceInstalled;
end;

function HasSqlService(): Boolean;
begin
  Result := _SqlServiceName <> '';
end;

function GetSqlServiceName(Param: String): String;
begin
  Result := _SqlServiceName;
end;
