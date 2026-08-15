"""
shared/totp_extractor.py
=========================
Pure QR/otpauth parsing logic — no I/O, no printing.

Shared by:
  - tools/extractor.py   (standalone CLI, atana_otp.exe)
  - ui/config_panel.py   ("📷 Subir QR" button in the panel — decodes the
                          image locally in the panel process and sends only
                          the extracted secret to the API; the image itself
                          never leaves the machine, and zxingcpp/protobuf
                          parsing never has to be a dependency of the
                          always-on dispatcher service)
"""

import base64
from urllib.parse import urlparse, parse_qs


def parse_varint(data: bytes, idx: int) -> tuple[int, int]:
    """Reads a protobuf varint. Returns (value, new_idx)."""
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
    """Reads a length-delimited field. Returns (bytes, new_idx)."""
    length, idx = parse_varint(data, idx)
    return data[idx:idx + length], idx + length


def parse_migration_payload(payload: bytes) -> list[dict]:
    """
    Parses the Google Authenticator migration protobuf.
      MigrationPayload { repeated OtpParameters otp_parameters = 1; }
      OtpParameters {
        bytes secret = 1; string name = 2; string issuer = 3;
        int32 algorithm = 4; int32 digits = 5; int32 type = 6; int64 counter = 7;
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
                accounts.append(_parse_otp_parameters(field_data))
        elif wire_type == 0:  # varint
            _, idx = parse_varint(payload, idx)
        elif wire_type == 5:  # 32-bit
            idx += 4
        elif wire_type == 1:  # 64-bit
            idx += 8
        else:
            break  # unknown type — stop

    return accounts


def _parse_otp_parameters(data: bytes) -> dict:
    """Parses a single OtpParameters message."""
    account = {
        "secret": None, "name": "", "issuer": "",
        "algorithm": 1, "digits": 6, "type": 2,  # SHA1, 6 digits, TOTP
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
                account["name"] = field_data.decode("utf-8", errors="ignore")
            elif field_number == 3:
                account["issuer"] = field_data.decode("utf-8", errors="ignore")
        elif wire_type == 0:
            value, idx = parse_varint(data, idx)
            if field_number == 4:
                account["algorithm"] = value
            elif field_number == 5:
                account["digits"] = value
            elif field_number == 6:
                account["type"] = value
        elif wire_type == 5:
            idx += 4
        elif wire_type == 1:
            idx += 8
        else:
            break

    return account


def extract_secret_from_uri(uri: str) -> list[dict]:
    """
    Extracts secrets from any authenticator URI:
      - otpauth://totp/...?secret=...
      - otpauth-migration://offline?data=...
    Returns a list of dicts with secret/name/issuer.
    """
    if uri.startswith("otpauth-migration://"):
        query   = parse_qs(urlparse(uri).query)
        data_b4 = query.get("data", [None])[0]
        if not data_b4:
            return []
        padding = (4 - len(data_b4) % 4) % 4
        payload = base64.b64decode(data_b4 + "=" * padding)
        return parse_migration_payload(payload)

    if uri.startswith("otpauth://"):
        parsed = urlparse(uri)
        params = parse_qs(parsed.query)
        secret = params.get("secret", [None])[0]
        if not secret:
            return []
        secret  = secret.strip().replace(" ", "").upper()
        padding = (8 - len(secret) % 8) % 8
        secret  = secret + "=" * padding
        return [{
            "secret": secret,
            "name":   parsed.path.lstrip("/"),
            "issuer": params.get("issuer", [""])[0],
        }]

    return []


def extract_secrets_from_image(image_path) -> list[dict]:
    """
    Reads a QR code from an image file and returns the accounts found in it
    (usually one, or several for a Google Authenticator "export accounts" QR).
    Raises ValueError with a user-facing message if nothing usable is found.
    Requires zxingcpp + Pillow — only imported here, never at module load
    time, so importing this module doesn't require them (extract_secret_from_uri
    is pure and dependency-free).
    """
    from PIL import Image
    import zxingcpp

    img = Image.open(image_path).convert("RGB")
    results = zxingcpp.read_barcodes(img)
    if not results:
        raise ValueError("No se detectó ningún QR en la imagen.")

    uri = results[0].text
    accounts = extract_secret_from_uri(uri)
    if not accounts:
        raise ValueError("No se pudo extraer ningún secreto de la URI del QR.")
    return accounts
