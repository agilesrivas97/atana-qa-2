#!/usr/bin/env python3
"""
build_restart_tray.py
======================
Empaqueta restart_tray.py como un ejecutable Windows standalone de consola.
Ver también build/build.py (dispatcher) y build_panel.py (panel) — este
tercer exe se instala junto a los otros dos (ver installer/atana_setup.iss).

Salida: dist/exe/atana_restart_tray.exe (mismo dist/exe que los otros dos,
        para que el instalador tome todo de un solo lugar)
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).parent.parent
ENTRY    = ROOT / "tools" / "restart_tray.py"
DIST_DIR = ROOT / "dist" / "exe"
WORK_DIR = ROOT / "build" / "work_restart_tray"
SPEC_DIR = ROOT / "build"

EXE_NAME = "atana_restart_tray"
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
        "--console",   # ventana de consola visible — herramienta interactiva
        "--name",     EXE_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(SPEC_DIR),
    ]

    if ICON.exists():
        cmd += ["--icon", str(ICON)]

    cmd += [str(ENTRY)]

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
