#!/usr/bin/env python3
"""
build_setup_db.py
=================
Empaqueta setup_db_cli.py como un ejecutable Windows standalone.
El .exe resultante no necesita Python instalado.

Uso (desde la raíz del proyecto o desde tools/):
    python tools/build_setup_db.py

Salida:
    tools/dist/atana_setup_db.exe

Requisito previo:
    pip install pyinstaller  (ya incluido en requirements.txt)
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Rutas ──────────────────────────────────────────────────────────────────────

THIS_DIR  = Path(__file__).parent.resolve()
ROOT      = THIS_DIR.parent
ENTRY     = THIS_DIR / "setup_db_cli.py"
DIST_DIR  = THIS_DIR / "dist"
WORK_DIR  = THIS_DIR / "_build_work_setupdb"
SPEC_DIR  = THIS_DIR / "_build_spec_setupdb"

EXE_NAME  = "atana_setup"

def main():
    if not ENTRY.exists():
        sys.exit(f"[ERROR] No se encontró {ENTRY}")

    # Crear dist sin borrar lo que ya está (otros exes del mismo build)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # Solo limpiar el exe anterior de este build específico
    old_exe = DIST_DIR / f"{EXE_NAME}.exe"
    if old_exe.exists():
        old_exe.unlink()

    # Limpiar carpetas temporales propias
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
        "--console",                         # app de consola — ventana CMD visible
        "--name",    EXE_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(SPEC_DIR),

        # Dependencias del script
        "--hidden-import", "pyodbc",
        "--hidden-import", "cryptography",
        "--hidden-import", "cryptography.fernet",
        "--hidden-import", "cryptography.hazmat.primitives.kdf.pbkdf2",
        "--hidden-import", "cryptography.hazmat.backends.openssl",

        str(ENTRY),
    ]

    result = subprocess.run(cmd, cwd=str(ROOT))

    if result.returncode != 0:
        sys.exit("[ERROR] Build fallido — revisá los mensajes de PyInstaller arriba.")

    exe = DIST_DIR / f"{EXE_NAME}.exe"
    if not exe.exists():
        exe = DIST_DIR / EXE_NAME   # por si se buildea en Linux/Mac

    if not exe.exists():
        sys.exit("[ERROR] No se encontró el ejecutable generado.")

    size   = exe.stat().st_size
    sha256 = _sha256(exe)

    print(f"\n{'=' * 55}")
    print(f"  Build exitoso")
    print(f"{'=' * 55}")
    print(f"  Archivo : {exe}")
    print(f"  Tamaño  : {size:,} bytes ({size / 1_048_576:.1f} MB)")
    print(f"  SHA-256 : {sha256}")
    print(f"\n  Ejecutar en Windows:")
    print(f"    atana_setup_db.exe")
    print(f"\n  Requisito en destino:")
    print(f"    ODBC Driver 17 for SQL Server (o 'SQL Server' genérico)")
    print(f"    -> Descargar: https://aka.ms/odbc17")
    print()

    # Limpiar carpetas temporales de PyInstaller
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
