"""AI background removal for the first product photo — replaces a busy/
uneven background with a clean white one so the item reads as a proper
e-commerce listing photo. Uses `rembg` (local, no per-image API cost) rather
than a paid cutout service like remove.bg, matching this project's existing
cost profile of only paying per-item for the Anthropic API.
"""
import io

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from rembg import new_session, remove
from scipy import ndimage

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


def _largest_component_mask(alpha: np.ndarray) -> np.ndarray:
    """rembg's alpha channel often leaves a handful of stray non-transparent
    pixels/small islands scattered outside the actual product (imperfect
    segmentation on a real-world, non-studio background) — a bounding box
    over ALL of them is much bigger than the product itself, so the product
    ends up looking small and off-center once composited. Keeping only the
    largest connected blob of foreground pixels discards that noise and
    gives a bounding box that's tight around the actual product."""
    mask = alpha > 10
    labeled, n = ndimage.label(mask)
    if n <= 1:
        return mask
    sizes = ndimage.sum(mask, labeled, index=range(1, n + 1))
    largest_label = int(np.argmax(sizes)) + 1
    return labeled == largest_label


def _add_drop_shadow(canvas: Image.Image, product_box: tuple[int, int, int, int]) -> Image.Image:
    """Draws a soft, blurred ellipse under the product's footprint before
    it's pasted on top — the difference between a catalog photo (product
    reads as sitting on a surface) and a flat cutout that looks like it's
    floating, which is what a plain white-background paste looks like on
    its own."""
    left, top, right, bottom = product_box
    width = right - left
    height = bottom - top

    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow_layer)
    shadow_w = int(width * 0.75)
    shadow_h = max(8, int(height * 0.06))
    cx = (left + right) // 2
    shadow_top = bottom - shadow_h // 2
    draw.ellipse(
        [cx - shadow_w // 2, shadow_top, cx + shadow_w // 2, shadow_top + shadow_h],
        fill=(0, 0, 0, 90),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(6, shadow_h // 2)))
    return Image.alpha_composite(canvas, shadow_layer)


def clean_product_photo(
    image_bytes: bytes,
    canvas_size: int = 1600,
    fill_ratio: float = 0.85,
) -> tuple[bytes, bool]:
    """Removes the background, crops tightly to the product (largest
    connected foreground blob — see _largest_component_mask), lightly
    freshens brightness/contrast, and composites it centered onto a pure
    white square canvas with a soft drop shadow — filling `fill_ratio` of
    the frame if the source resolution allows it without upscaling past
    `_MAX_UPSCALE`, otherwise left at native size (see module docstring
    above). Returns (jpeg_bytes, low_resolution).

    Does NOT remove reflections/glare — that needs actual generative photo
    retouching (a different, paid AI image-editing service), not background
    segmentation; a bright contrast boost can soften harsh hotspots but
    won't erase what's actually reflected in a glossy surface.

    Raises ValueError if rembg finds no non-transparent pixels (e.g. a
    completely blank/blown-out photo) — the caller should fall back to
    keeping the original photo rather than saving a blank white square.
    """
    cutout = remove(image_bytes, session=_get_session())
    rgba = Image.open(io.BytesIO(cutout)).convert("RGBA")

    alpha = np.array(rgba.getchannel("A"))
    mask = _largest_component_mask(alpha)
    if not mask.any():
        raise ValueError("Background removal found no foreground subject in this photo.")

    # Zero out alpha for anything outside the kept component so no stray
    # pixels bleed into the final composite either.
    clean_alpha = np.where(mask, alpha, 0).astype(np.uint8)
    rgba.putalpha(Image.fromarray(clean_alpha))

    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    top, bottom = np.where(rows)[0][[0, -1]]
    left, right = np.where(cols)[0][[0, -1]]
    cropped = rgba.crop((int(left), int(top), int(right) + 1, int(bottom) + 1))

    # Freshen the product itself: auto-levels the RGB channels (per-channel
    # contrast stretch, ignoring the transparent surround) then a small
    # brightness/contrast lift — reads as a cleaner, more "catalog" product
    # shot without looking artificially edited.
    rgb = cropped.convert("RGB")
    rgb = ImageOps.autocontrast(rgb, cutoff=1)
    rgb = ImageEnhance.Brightness(rgb).enhance(1.05)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.08)
    cropped = Image.merge("RGBA", (*rgb.split(), cropped.getchannel("A")))

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
    canvas = _add_drop_shadow(canvas, (paste_x, paste_y, paste_x + new_w, paste_y + new_h))
    canvas.paste(cropped, (paste_x, paste_y), cropped)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue(), low_resolution
