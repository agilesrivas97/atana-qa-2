#!/usr/bin/env python3
"""
build_extractor.py
==================
Empaqueta extractor.py como un ejecutable Windows standalone.
Usa zxing-cpp para evitar dependencias de DLLs externas.

Salida: tools/dist/atana_extractor.exe
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

THIS_DIR = Path(__file__).parent.resolve()
ROOT     = THIS_DIR.parent
ENTRY    = THIS_DIR / "extractor.py"
DIST_DIR = THIS_DIR / "dist"
WORK_DIR = THIS_DIR / "_build_work_extractor"
SPEC_DIR = THIS_DIR / "_build_spec_extractor"

EXE_NAME = "atana_otp"


def main():
    if not ENTRY.exists():
        sys.exit(f"[ERROR] No se encontro {ENTRY}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    old_exe = DIST_DIR / f"{EXE_NAME}.exe"
    if old_exe.exists():
        old_exe.unlink()

    for d in (WORK_DIR, SPEC_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    print(f"Empaquetando {ENTRY.name} -> {EXE_NAME}.exe ...")
    print(f"Destino: {DIST_DIR}\n")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--console",
        "--name",    EXE_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(SPEC_DIR),

        # Pillow
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--collect-submodules", "PIL",

        # zxing-cpp (Autocontenido)
        "--hidden-import", "zxingcpp",
        "--collect-all", "zxingcpp",

        # pyotp
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
    print(f"\n  Ejecutar en Windows:")
    print(f"    atana_extractor.exe C:\\ruta\\a\\qr.png")
    print()

    for d in (WORK_DIR, SPEC_DIR):
        shutil.rmtree(d, ignore_errors=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    main()
