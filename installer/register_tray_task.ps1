# installer/register_tray_task.ps1
# ============================================================
# Registers the Scheduled Task that supervises the ATANA tray icon.
# Invoked by the Inno Setup installer ([Run] section) on every fresh
# install/upgrade.
#
# NOTE: dispatcher/main.py's _ensure_tray_supervised() carries an inline copy
# of this same logic — it runs at every dispatcher startup so that clients
# who get a new dispatcher version purely via auto-update (which replaces
# only the .exe, not this script or the installer's [Run] steps) still end
# up with the task registered/repaired. Keep both in sync.
#
# Why a Scheduled Task instead of a HKCU\...\Run entry (the old approach):
#   - "At log on" fires for whichever user logs in — Windows itself crosses
#     the Session 0 -> interactive-session boundary, no polling needed.
#   - A second trigger repeats every 3 minutes for as long as Windows is up.
#     Combined with MultipleInstances=IgnoreNew, this is a no-op while the
#     tray is alive and a relaunch the moment it dies — self-healing for the
#     whole session, not just at dispatcher startup.
#   - ExecutionTimeLimit is explicitly unlimited. Leaving it at the Task
#     Scheduler default silently kills the process once the limit elapses —
#     the likely root cause of "tray dies mid-session and never comes back".
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File register_tray_task.ps1 -ExePath "C:\Program Files\ATANA\atana_dispatcher.exe"

param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [string]$TaskName = "AtanaTrayWatchdog"
)

$ErrorActionPreference = "SilentlyContinue"

# Clean up artifacts from older installer versions.
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "AtanaTray"
Unregister-ScheduledTask -TaskName "AtanaTrayStart" -Confirm:$false

$ErrorActionPreference = "Stop"

$action    = New-ScheduledTaskAction -Execute $ExePath -Argument "--tray"
$logon     = New-ScheduledTaskTrigger -AtLogOn
$watchdog  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 3) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Users" -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger @($logon, $watchdog) `
    -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Write-Host "Scheduled Task '$TaskName' registered ($ExePath --tray)"
