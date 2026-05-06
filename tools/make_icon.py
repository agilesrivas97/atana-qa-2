#!/usr/bin/env python3
"""
tools/make_icon.py
==================
Genera installer/atana.ico descargando el logo de atana.com.ar.
Si la descarga falla, genera un ícono de texto como fallback.

Uso:
    python tools/make_icon.py

Salida:
    installer/atana.ico   ← referenciado en atana_setup.iss y build/build.py
"""

import io
import sys
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Requiere Pillow:  pip install Pillow")

ROOT      = Path(__file__).parent.parent
OUT_FILE  = ROOT / "installer" / "atana.ico"
LOGO_URL  = "https://atana.com.ar/wp-content/uploads/2022/08/cropped-logo-clean-1-1.png"

BRAND_ORANGE = "#F08C00"
SIZES = [256, 64, 48, 32, 16]


def _download_logo() -> Image.Image | None:
    try:
        print(f"  Descargando logo desde {LOGO_URL}...")
        req = urllib.request.Request(LOGO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        print(f"  Logo descargado: {img.size}")
        return img
    except Exception as e:
        print(f"  No se pudo descargar el logo: {e}")
        return None


def _make_icon_from_logo(size: int, logo: Image.Image) -> Image.Image:
    """Redimensiona el logo tal cual, sin tocar el fondo."""
    return logo.resize((size, size), Image.LANCZOS).convert("RGBA")


def _make_fallback_icon(size: int) -> Image.Image:
    """Fallback si no se puede descargar el logo: letra A naranja sobre blanco."""
    img  = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)

    font_size = int(size * 0.7)
    font = None
    for path in [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except Exception:
            pass

    letter = "A"
    if font:
        bbox = draw.textbbox((0, 0), letter, font=font)
        x = (size - (bbox[2] - bbox[0])) // 2 - bbox[0]
        y = (size - (bbox[3] - bbox[1])) // 2 - bbox[1] - int(size * 0.04)
        draw.text((x, y), letter, font=font, fill=BRAND_ORANGE)
    else:
        inner = int(size * 0.2)
        draw.rectangle([inner, inner, size - inner, size - inner], fill=BRAND_ORANGE)

    return img


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    logo = _download_logo()
    if logo is None:
        print("  Usando fallback de texto (letra A).")

    print(f"  Generando ícono con {len(SIZES)} tamaños: {SIZES}...")
    if logo is not None:
        frames = [_make_icon_from_logo(s, logo) for s in SIZES]
    else:
        frames = [_make_fallback_icon(s) for s in SIZES]

    # Guardar como .ico multi-resolución
    frames[0].save(
        OUT_FILE,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=frames[1:],
    )

    size_kb = OUT_FILE.stat().st_size // 1024
    print(f"\n  [OK] Guardado: {OUT_FILE}  ({size_kb} KB)")
    print(f"       Referenciado en installer/atana_setup.iss y build/build.py")


if __name__ == "__main__":
    main()
