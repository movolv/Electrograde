"""Decode barcodes/QR codes from a captured photo.

Uses pyzbar + Pillow. If pyzbar's native zbar library isn't installed on the
host OS, decoding gracefully fails and the caller falls back to manual entry.
"""
from io import BytesIO
from typing import List, Optional

from PIL import Image

try:
    from pyzbar.pyzbar import decode as _zbar_decode
    _ZBAR_AVAILABLE = True
except Exception:
    _ZBAR_AVAILABLE = False


def zbar_available() -> bool:
    return _ZBAR_AVAILABLE


def decode_barcodes(image_bytes: bytes) -> List[str]:
    """Return a list of decoded barcode/QR payloads found in the image."""
    if not _ZBAR_AVAILABLE:
        return []
    try:
        img = Image.open(BytesIO(image_bytes)).convert("L")
        results = _zbar_decode(img)
        return [r.data.decode("utf-8", errors="ignore") for r in results]
    except Exception:
        return []


def best_guess_model_number(payloads: List[str]) -> Optional[str]:
    """Barcodes are usually EAN/UPC; still useful as a lookup key even if not
    a literal model number — many spec sites index by UPC/EAN too."""
    if not payloads:
        return None
    return payloads[0]
