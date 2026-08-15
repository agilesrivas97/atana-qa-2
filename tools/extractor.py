# pip install zxing-cpp pillow pyotp
import os
import sys
import time
from pathlib import Path

# Standalone CLI over shared.totp_extractor — kept as a separate exe (atana_otp.exe,
# see build_extractor.py) for manual/offline use. The panel's "Subir QR" button
# (ui/config_panel.py) uses the same shared module directly, in-process.
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.totp_extractor import extract_secret_from_uri  # noqa: E402

import pyotp


def verify_totp(secret: str) -> None:
    """Muestra el código actual y los adyacentes para verificar."""
    padding = (8 - len(secret) % 8) % 8
    secret  = secret + "=" * padding

    try:
        totp     = pyotp.TOTP(secret)
        now      = time.time()
        restante = 30 - int(now) % 30

        print(f"\n  Código anterior:  {totp.at(now - 30)}")
        print(f"  Código actual:    {totp.now()}  ← {restante}s restantes")
        print(f"  Código siguiente: {totp.at(now + 30)}")
        print(f"\n  Compará con Google Authenticator ahora mismo.")
    except Exception as e:
        print(f"  Error verificando TOTP: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def _process(img_path: Path) -> bool:
    """Processes one image. Returns True on success."""
    if not img_path.exists():
        print(f"Error: no se encontro {img_path}")
        return False

    try:
        from PIL import Image
        import zxingcpp
        img     = Image.open(img_path).convert("RGB")
        results = zxingcpp.read_barcodes(img)
    except Exception as e:
        print(f"Error abriendo o leyendo la imagen: {e}")
        return False

    if not results:
        print("Error: no se detecto ningun QR en la imagen.")
        print("Tip: proba con mayor resolucion o recorta la imagen al QR exacto.")
        return False

    uri = results[0].text
    print(f"\nURI detectada: {uri[:80]}...")

    accounts = extract_secret_from_uri(uri)
    if not accounts:
        print("Error: no se pudo extraer ningun secreto de la URI.")
        return False

    print(f"\nCuentas encontradas: {len(accounts)}")
    for i, acc in enumerate(accounts):
        print(f"\n--- Cuenta {i + 1} " + "-" * 35)
        print(f"  Nombre:  {acc['name']  or '(sin nombre)'}")
        print(f"  Issuer:  {acc['issuer'] or '(sin issuer)'}")
        print(f"  Secret:  {acc['secret']}")
        if acc.get("secret"):
            verify_totp(acc["secret"])

    print("\n" + "-" * 53)
    print("Copia el Secret en setup_db_cli cuando te lo pida (TOTP shared secret).")
    return True


if __name__ == "__main__":
    os.system("")  # habilitar ANSI en Windows 10+

    # Si se paso una imagen como argumento, procesarla primero
    first = Path(sys.argv[1].strip().strip('"\'}')).expanduser() if len(sys.argv) > 1 else None

    while True:
        if first is not None:
            img_path, first = first, None
        else:
            print()
            raw = input("Ruta a la imagen del QR (Enter para salir): ").strip().strip('"\'')
            if not raw:
                break
            img_path = Path(raw).expanduser()

        _process(img_path)

        print()
        otra = input("Procesar otra imagen? [s/N]: ").strip().lower()
        if otra not in ("s", "si", "y", "yes"):
            break

    print("\nHasta luego.")
