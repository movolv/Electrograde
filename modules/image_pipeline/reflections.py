"""Reflection/glare reduction — classical image processing only.

IMPORTANT LIMITATION (documented deliberately, not an oversight): this
does NOT perform true reflection removal. True removal — e.g. erasing a
photographer's silhouette mirrored in a glossy black panel — requires a
generative model that reconstructs plausible detail behind the
reflection. This session tested the leading local open-source option
(StableDelight) and it could not be installed on this machine: cascading
version incompatibilities (a Windows path bug, an fp16/variant mismatch,
a `diffusers` internal-API break, a `huggingface_hub` internal-API break,
and finally `numpy==1.26.4` needing a C compiler that isn't installed).
That leaves classical inpainting as the only "local, no paid API" option
available today — it only helps small, bright, well-defined glare "hot
spots" (a window/light reflection blown out to near-white), not a
soft-toned mirrored silhouette. If full removal of that kind of
reflection is required, it needs a paid generative API (e.g. OpenAI's
GPT Image) wired in as a separate, optional stage — this module's single
function is intentionally the only thing that would need swapping to do
that later.
"""
import cv2
import numpy as np
from PIL import Image
from scipy import ndimage

_SATURATION_THRESHOLD = 40  # HSV saturation below this = candidate glare

# A genuine specular hot spot is a small, tight blob — a bright/white
# plastic or brushed-metal SURFACE can easily satisfy a brightness+
# saturation threshold over a much larger area without being glare at
# all. Verified this session: without a size cap, "reflection reduction"
# was inpainting large swaths of legitimately bright product surfaces,
# visibly softening/blurring real detail (measured as a large sharpness
# score drop on genuinely sharp studio photos). Capping each candidate
# blob's area relative to the product keeps this targeted at actual small
# hotspots.
_MAX_BLOB_FRACTION_OF_PRODUCT = 0.015


def reduce_reflections(rgba: Image.Image, strength: float) -> Image.Image:
    """`strength` in [0, 1]: higher picks up dimmer/more highlights as
    "glare" to inpaint over. 0 is a no-op. Only touches pixels inside the
    product (alpha > 10), and only small localized bright spots — see
    _MAX_BLOB_FRACTION_OF_PRODUCT — so it never bleeds into the
    background or softens large legitimately-bright surfaces."""
    strength = max(0.0, min(1.0, strength))
    if strength == 0:
        return rgba

    rgb = np.array(rgba.convert("RGB"))
    alpha = np.array(rgba.getchannel("A"))
    product_area = int((alpha > 10).sum())
    if product_area == 0:
        return rgba

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]

    # Tight by design: strength only widens the margin a little (240-250
    # brightness range), it never opens this up to ordinary bright
    # surfaces the way a much lower threshold would.
    brightness_threshold = 250 - int(10 * strength)
    candidate_mask = (value > brightness_threshold) & (saturation < _SATURATION_THRESHOLD) & (alpha > 10)

    if not candidate_mask.any():
        return rgba

    labeled, n = ndimage.label(candidate_mask)
    max_blob_pixels = max(4, int(product_area * _MAX_BLOB_FRACTION_OF_PRODUCT))
    highlight_mask = np.zeros_like(candidate_mask, dtype=bool)
    if n > 0:
        sizes = ndimage.sum(candidate_mask, labeled, index=range(1, n + 1))
        for i, size in enumerate(sizes, start=1):
            if size <= max_blob_pixels:
                highlight_mask |= labeled == i

    if not highlight_mask.any():
        return rgba

    highlight_mask = (highlight_mask.astype(np.uint8) * 255)
    highlight_mask = cv2.dilate(highlight_mask, np.ones((3, 3), np.uint8), iterations=1)

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    inpainted_bgr = cv2.inpaint(bgr, highlight_mask, 5, cv2.INPAINT_TELEA)
    inpainted_rgb = Image.fromarray(cv2.cvtColor(inpainted_bgr, cv2.COLOR_BGR2RGB))

    return Image.merge("RGBA", (*inpainted_rgb.split(), rgba.getchannel("A")))
