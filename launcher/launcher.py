"""
launcher/launcher.py
====================
Lightweight process that installs the client ONCE.
Its only job: check version → replace executable → launch dispatcher.
No external dependencies — stdlib only.
"""

import os
import sys
import subprocess
import shutil
import time
from pathlib import Path


DISPATCHER_EXE = Path(__file__).parent.parent / "atana_dispatcher.exe"
CHECK_INTERVAL = 300  # seconds between checks (5 minutes)


def _start_tray():
    """Launches the system tray icon as a separate background process."""
    try:
        tray_proc = subprocess.Popen(
            [str(DISPATCHER_EXE), "--tray"],
            cwd=DISPATCHER_EXE.parent,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        print(f"Tray PID: {tray_proc.pid}")
        return tray_proc
    except Exception as e:
        print(f"Could not start tray: {e}")
        return None


def main():
    print("ATANA Launcher started")

    tray_proc = _start_tray()

    while True:
        # Restart tray if it crashed
        if tray_proc and tray_proc.poll() is not None:
            print("Tray exited unexpectedly — restarting...")
            tray_proc = _start_tray()

        # Launch the dispatcher
        proc = subprocess.Popen(
            [str(DISPATCHER_EXE)] + sys.argv[1:],
            cwd=DISPATCHER_EXE.parent,
        )

        print(f"Dispatcher PID: {proc.pid}")
        return_code = proc.wait()

        # Code 99 = update is ready
        if return_code == 99:
            print("Update available — applying...")
            _apply_update()
            print("Update applied — relaunching...")
            continue

        # Code 0 = normal exit
        if return_code == 0:
            print("Dispatcher closed normally")
            break

        # Other code = unexpected error — wait and retry
        print(f"Dispatcher exited with code {return_code} — retrying in 60s...")
        time.sleep(60)


def _apply_update():
    """Replaces the executable with the new version."""
    new_exe = DISPATCHER_EXE.with_suffix(".new")

    if not new_exe.exists():
        print("New executable file (.new) not found")
        return

    # Backup current executable
    backup = DISPATCHER_EXE.with_suffix(".bak")
    try:
        shutil.copy2(DISPATCHER_EXE, backup)
    except Exception:
        pass

    # Replace
    try:
        if os.name == "nt":
            # Windows: needs retry because exe may be in use
            for _ in range(5):
                try:
                    DISPATCHER_EXE.unlink(missing_ok=True)
                    shutil.move(str(new_exe), str(DISPATCHER_EXE))
                    print("Update applied successfully")
                    return
                except PermissionError:
                    time.sleep(2)
            print("Could not replace the executable")
        else:
            # Unix: os.replace is atomic
            os.replace(new_exe, DISPATCHER_EXE)
            DISPATCHER_EXE.chmod(0o755)
            print("Update applied successfully")
    except Exception as e:
        print(f"Error applying update: {e}")
        # Restore backup if it fails
        if backup.exists():
            shutil.copy2(backup, DISPATCHER_EXE)


if __name__ == "__main__":
    main()
