#!/usr/bin/env python3
"""
build_panel.py
===============
Empaqueta ui/panel_main.py como un ejecutable Windows standalone — el panel
(General + Configuración), independiente de atana_dispatcher.exe.

Se instala en la misma carpeta que atana_dispatcher.exe (ver installer/
atana_setup.iss) y solo habla con la API local (localhost:{api_port}); no
necesita credenciales de SQL Server ni las claves Fernet.

Salida: dist/exe/atana_panel.exe (mismo dist/exe que build/build.py, para
        que installer/atana_setup.iss tome ambos exes de un solo lugar)
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).parent.parent
ENTRY    = ROOT / "ui" / "panel_main.py"
DIST_DIR = ROOT / "dist" / "exe"
WORK_DIR = ROOT / "build" / "work_panel"
SPEC_DIR = ROOT / "build"

EXE_NAME = "atana_panel"
ICON     = ROOT / "installer" / "atana.ico"


def main():
    if not ENTRY.exists():
        sys.exit(f"[ERROR] No se encontro {ENTRY}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    old_exe = DIST_DIR / f"{EXE_NAME}.exe"
    if old_exe.exists():
        old_exe.unlink()

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Empaquetando {ENTRY.name} -> {EXE_NAME}.exe ...")
    print(f"Destino: {DIST_DIR}\n")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--noconsole",   # ventana tkinter — sin consola detrás
        "--name",     EXE_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(SPEC_DIR),
    ]

    if ICON.exists():
        cmd += ["--icon", str(ICON)]

    cmd += [
        # UI modules loaded by ui/panel_main.py
        "--hidden-import", "ui.panel_app",
        "--hidden-import", "ui.config_panel",
        "--hidden-import", "ui.totp_tool",
        "--hidden-import", "ui.async_utils",
        "--hidden-import", "shared.api_client",
        "--hidden-import", "shared.totp_extractor",
        "--hidden-import", "shared.paths",

        # tkinter is bundled with Python — no hidden import needed

        "--hidden-import", "loguru",

        # QR decoding for the "📷 Subir QR" button (same as tools/build_extractor.py)
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--collect-submodules", "PIL",
        "--hidden-import", "zxingcpp",
        "--collect-all", "zxingcpp",
        "--hidden-import", "pyotp",

        str(ENTRY),
    ]

    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        sys.exit("[ERROR] Build fallido — revisa los mensajes de PyInstaller arriba.")

    exe = DIST_DIR / f"{EXE_NAME}.exe"
    if not exe.exists():
        exe = DIST_DIR / EXE_NAME
    if not exe.exists():
        sys.exit("[ERROR] No se encontro el ejecutable generado.")

    size   = exe.stat().st_size
    sha256 = _sha256(exe)

    print(f"\n{'=' * 55}")
    print(f"  Build exitoso")
    print(f"{'=' * 55}")
    print(f"  Archivo : {exe}")
    print(f"  Tamano  : {size:,} bytes ({size / 1_048_576:.1f} MB)")
    print(f"  SHA-256 : {sha256}")
    print()

    shutil.rmtree(WORK_DIR, ignore_errors=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    main()
