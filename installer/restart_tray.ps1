# installer/restart_tray.ps1
# ============================
# Mata cualquier proceso del tray que este corriendo y lo vuelve a levantar.
# Pensado para soporte: si el icono se ve colgado o no aparece, esto lo
# arregla al toque sin reinstalar ni esperar al watchdog automatico (que
# puede tardar hasta ~3 min en el peor caso — ver dispatcher/main.py
# _ensure_tray_supervised / _tray_watchdog_check).
#
# Uso: doble click en el acceso directo "ATANA - Reiniciar Tray" del Menu
# Inicio, o manualmente:
#   powershell -ExecutionPolicy Bypass -File restart_tray.ps1

$ErrorActionPreference = "SilentlyContinue"
$TaskName = "AtanaTrayWatchdog"

Write-Host "=== ATANA — Reiniciar tray ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Buscando procesos del tray..."

$killed = 0
Get-CimInstance Win32_Process -Filter "Name='atana_dispatcher.exe'" |
    Where-Object { $_.CommandLine -like '*--tray*' } |
    ForEach-Object {
        Write-Host "  Matando PID $($_.ProcessId)..."
        Stop-Process -Id $_.ProcessId -Force
        $killed++
    }

if ($killed -eq 0) {
    Write-Host "  No habia ningun proceso del tray corriendo."
} else {
    Write-Host "  $killed proceso(s) del tray terminado(s)."
}

Start-Sleep -Seconds 1

Write-Host ""
Write-Host "Relanzando el tray..."

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "  Usando la Scheduled Task '$TaskName'."
    Start-ScheduledTask -TaskName $TaskName
} else {
    # Instalacion vieja que todavia no registro la task (se registra sola al
    # reiniciar el servicio AtanaDispatcher) — lanzamos directo como fallback.
    Write-Host "  Scheduled Task '$TaskName' no encontrada — lanzando directo."
    Start-Process -FilePath (Join-Path $PSScriptRoot "atana_dispatcher.exe") -ArgumentList "--tray"
}

Start-Sleep -Seconds 2

$running = Get-CimInstance Win32_Process -Filter "Name='atana_dispatcher.exe'" |
    Where-Object { $_.CommandLine -like '*--tray*' } |
    Select-Object -First 1

Write-Host ""
if ($running) {
    Write-Host "Tray reiniciado correctamente (PID $($running.ProcessId))." -ForegroundColor Green
} else {
    Write-Host "El tray todavia no aparece corriendo — puede tardar unos segundos mas." -ForegroundColor Yellow
    Write-Host "Volve a revisar la bandeja del sistema en un momento."
}

Write-Host ""
Write-Host "Presiona Enter para cerrar..."
Read-Host | Out-Null
