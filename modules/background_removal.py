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


def _get_session():
    global _session
    if _session is None:
        _session = new_session(_MODEL_NAME)
    return _session


def clean_product_photo(
    image_bytes: bytes,
    canvas_size: int = 1600,
    fill_ratio: float = 0.85,
) -> bytes:
    """Removes the background, crops to the product's bounding box, and
    composites it centered onto a pure white square canvas sized so the
    product fills `fill_ratio` of the frame. Returns JPEG bytes.

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
    scale = target_dim / max(cropped.width, cropped.height)
    new_w = max(1, int(cropped.width * scale))
    new_h = max(1, int(cropped.height * scale))
    cropped = cropped.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 255))
    paste_x = (canvas_size - new_w) // 2
    paste_y = (canvas_size - new_h) // 2
    canvas.paste(cropped, (paste_x, paste_y), cropped)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()
