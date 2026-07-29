"""AI background removal for the first product photo — replaces a busy/
uneven background with a clean white one so the item reads as a proper
e-commerce listing photo. Uses `rembg` (local, no per-image API cost) rather
than a paid cutout service like remove.bg, matching this project's existing
cost profile of only paying per-item for the Anthropic API.
"""
import io

import numpy as np
from PIL import Image
from rembg import new_session, remove

# isnet-general-use trades a bit of speed for noticeably cleaner edges than
# the default u2net model on glossy/reflective product photography (steel,
# plastic appliances) — worth the extra download size for this use case.
_MODEL_NAME = "isnet-general-use"
_session = None

# Enlarging a low-resolution phone-camera crop up to a full 1600px canvas is
# what actually introduces visible blur — no resize algorithm recovers detail
# that was never captured. Capping how far we'll upscale keeps the output as
# sharp as the source allows; when the source is too small to reach
# `fill_ratio` without exceeding this cap, the product is left at its native
# size (just centered) instead, and the caller is told via `low_resolution`
# so the UI can suggest retaking the photo closer up / with better focus.
_MAX_UPSCALE = 1.3


def _get_session():
    global _session
    if _session is None:
        _session = new_session(_MODEL_NAME)
    return _session


def clean_product_photo(
    image_bytes: bytes,
    canvas_size: int = 1600,
    fill_ratio: float = 0.85,
) -> tuple[bytes, bool]:
    """Removes the background, crops to the product's bounding box, and
    composites it centered onto a pure white square canvas — filling
    `fill_ratio` of the frame if the source resolution allows it without
    upscaling past `_MAX_UPSCALE`, otherwise left at native size (see
    module docstring above). Returns (jpeg_bytes, low_resolution).

    Raises ValueError if rembg finds no non-transparent pixels (e.g. a
    completely blank/blown-out photo) — the caller should fall back to
    keeping the original photo rather than saving a blank white square.
    """
    cutout = remove(image_bytes, session=_get_session())
    rgba = Image.open(io.BytesIO(cutout)).convert("RGBA")

    alpha = np.array(rgba.getchannel("A"))
    mask = alpha > 10
    if not mask.any():
        raise ValueError("Background removal found no foreground subject in this photo.")

    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    top, bottom = np.where(rows)[0][[0, -1]]
    left, right = np.where(cols)[0][[0, -1]]
    cropped = rgba.crop((int(left), int(top), int(right) + 1, int(bottom) + 1))

    target_dim = int(canvas_size * fill_ratio)
    ideal_scale = target_dim / max(cropped.width, cropped.height)
    scale = min(ideal_scale, _MAX_UPSCALE)
    low_resolution = ideal_scale > _MAX_UPSCALE

    new_w = max(1, int(cropped.width * scale))
    new_h = max(1, int(cropped.height * scale))
    resample = Image.LANCZOS if scale < 1 else Image.BICUBIC
    cropped = cropped.resize((new_w, new_h), resample)

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 255))
    paste_x = (canvas_size - new_w) // 2
    paste_y = (canvas_size - new_h) // 2
    canvas.paste(cropped, (paste_x, paste_y), cropped)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue(), low_resolution
