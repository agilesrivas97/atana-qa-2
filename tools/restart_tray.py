"""
tools/restart_tray.py
======================
Console tool: kills any running tray process and relaunches it. Support
utility — no reinstall, no waiting for the automatic watchdog (~3 min
worst case, see dispatcher/main.py's _register_tray_task).

Packaged as atana_restart_tray.exe (see tools/build_restart_tray.py),
installed next to atana_dispatcher.exe and linked from the Start Menu
("ATANA - Reiniciar Tray"). Replaces the old installer/restart_tray.ps1 —
same logic, but a real console .exe instead of a raw PowerShell script (no
ExecutionPolicy dance, double-clickable, less likely to get flagged by
AV/SmartScreen than "powershell.exe -ExecutionPolicy Bypass ...").
"""

import subprocess
import sys
import time
from pathlib import Path

TASK_NAME = "AtanaTrayWatchdog"


def _run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        capture_output=True, text=True, timeout=20,
    )


def _find_tray_pids() -> list[int]:
    result = _run_ps(
        "Get-CimInstance Win32_Process -Filter \"Name='atana_dispatcher.exe'\" "
        "| Where-Object { $_.CommandLine -like '*--tray*' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]


def _task_exists() -> bool:
    result = _run_ps(f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue) -ne $null")
    return result.stdout.strip().lower() == "true"


def main():
    print("=== ATANA - Reiniciar tray ===\n")

    print("Buscando procesos del tray...")
    pids = _find_tray_pids()
    if not pids:
        print("  No habia ningun proceso del tray corriendo.")
    else:
        for pid in pids:
            print(f"  Matando PID {pid}...")
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"  {len(pids)} proceso(s) del tray terminado(s).")

    time.sleep(1)

    print("\nRelanzando el tray...")
    if _task_exists():
        print(f"  Usando la Scheduled Task '{TASK_NAME}'.")
        _run_ps(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
    else:
        # Instalacion vieja que todavia no registro la task (se registra sola
        # al reiniciar el servicio AtanaDispatcher) — lanzamos directo.
        exe = Path(sys.executable).resolve().parent / "atana_dispatcher.exe"
        print(f"  Scheduled Task no encontrada — lanzando directo ({exe}).")
        try:
            subprocess.Popen([str(exe), "--tray"])
        except Exception as e:
            print(f"  ERROR: no se pudo lanzar {exe}: {e}")

    time.sleep(2)

    print()
    running = _find_tray_pids()
    if running:
        print(f"Tray reiniciado correctamente (PID {running[0]}).")
    else:
        print("El tray todavia no aparece corriendo — puede tardar unos segundos mas.")
        print("Volve a revisar la bandeja del sistema en un momento.")

    print("\nPresiona Enter para cerrar...")
    try:
        input()
    except EOFError:
        pass


if __name__ == "__main__":
    main()
