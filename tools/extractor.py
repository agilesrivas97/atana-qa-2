# pip install zxing-cpp pillow pyotp
import os
import sys
import zxingcpp
from PIL import Image
from urllib.parse import urlparse, parse_qs
import pyotp
import base64
import struct
import time
from pathlib import Path


def parse_varint(data: bytes, idx: int) -> tuple[int, int]:
    """Lee un varint de protobuf. Retorna (valor, nuevo_idx)."""
    result = 0
    shift  = 0
    while idx < len(data):
        byte    = data[idx]
        idx    += 1
        result |= (byte & 0x7F) << shift
        shift  += 7
        if not (byte & 0x80):
            break
    return result, idx


def parse_length_delimited(data: bytes, idx: int) -> tuple[bytes, int]:
    """Lee un campo length-delimited. Retorna (bytes, nuevo_idx)."""
    length, idx = parse_varint(data, idx)
    return data[idx:idx + length], idx + length


def parse_migration_payload(payload: bytes) -> list[dict]:
    """
    Parsea el protobuf de Google Authenticator migration.
    Estructura:
      MigrationPayload {
        repeated OtpParameters otp_parameters = 1;
      }
      OtpParameters {
        bytes  secret    = 1;
        string name      = 2;
        string issuer    = 3;
        int32  algorithm = 4;
        int32  digits    = 5;
        int32  type      = 6;
        int64  counter   = 7;
      }
    """
    accounts = []
    idx = 0

    while idx < len(payload):
        tag_wire, idx = parse_varint(payload, idx)
        field_number  = tag_wire >> 3
        wire_type     = tag_wire & 0x7

        if wire_type == 2:  # length-delimited
            field_data, idx = parse_length_delimited(payload, idx)

            if field_number == 1:  # otp_parameters
                account = _parse_otp_parameters(field_data)
                accounts.append(account)
            # ignorar otros campos del MigrationPayload

        elif wire_type == 0:  # varint
            _, idx = parse_varint(payload, idx)
        elif wire_type == 5:  # 32-bit
            idx += 4
        elif wire_type == 1:  # 64-bit
            idx += 8
        else:
            break  # tipo desconocido — parar

    return accounts


def _parse_otp_parameters(data: bytes) -> dict:
    """Parsea un OtpParameters del protobuf."""
    account = {
        "secret":    None,
        "name":      "",
        "issuer":    "",
        "algorithm": 1,   # SHA1
        "digits":    6,
        "type":      2,   # TOTP
    }
    idx = 0

    while idx < len(data):
        tag_wire, idx = parse_varint(data, idx)
        field_number  = tag_wire >> 3
        wire_type     = tag_wire & 0x7

        if wire_type == 2:
            field_data, idx = parse_length_delimited(data, idx)
            if field_number == 1:
                account["secret"] = base64.b32encode(field_data).decode().rstrip("=")
            elif field_number == 2:
                account["name"]   = field_data.decode("utf-8", errors="ignore")
            elif field_number == 3:
                account["issuer"] = field_data.decode("utf-8", errors="ignore")

        elif wire_type == 0:
            value, idx = parse_varint(data, idx)
            if field_number == 4:
                account["algorithm"] = value
            elif field_number == 5:
                account["digits"]    = value
            elif field_number == 6:
                account["type"]      = value

        elif wire_type == 5:
            idx += 4
        elif wire_type == 1:
            idx += 8
        else:
            break

    return account


def extract_secret_from_uri(uri: str) -> list[dict]:
    """
    Extrae secretos de cualquier URI de autenticador:
      - otpauth://totp/...?secret=...
      - otpauth-migration://offline?data=...
    Retorna lista de dicts con secret, name, issuer.
    """
    if uri.startswith("otpauth-migration://"):
        query   = parse_qs(urlparse(uri).query)
        data_b4 = query.get("data", [None])[0]
        if not data_b4:
            return []
        # Agregar padding si falta
        padding = (4 - len(data_b4) % 4) % 4
        payload = base64.b64decode(data_b4 + "=" * padding)
        return parse_migration_payload(payload)

    elif uri.startswith("otpauth://"):
        parsed = urlparse(uri)
        params = parse_qs(parsed.query)
        secret = params.get("secret", [None])[0]
        if not secret:
            return []
        # Normalizar secreto — sin espacios, uppercase, con padding
        secret  = secret.strip().replace(" ", "").upper()
        padding = (8 - len(secret) % 8) % 8
        secret  = secret + "=" * padding
        name    = parsed.path.lstrip("/")
        issuer  = params.get("issuer", [""])[0]
        return [{"secret": secret, "name": name, "issuer": issuer}]

    return []


def verify_totp(secret: str) -> None:
    """Muestra el código actual y los adyacentes para verificar."""
    # Asegurar padding correcto
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