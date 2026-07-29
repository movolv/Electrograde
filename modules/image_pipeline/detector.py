"""Product detection: runs rembg's segmentation model to get a foreground
mask, then keeps only the largest connected component to discard stray
segmentation noise (verified necessary on real, non-studio-background
phone photos this session — a bounding box over every non-transparent
pixel was much bigger than the actual product, making it look small and
off-center in the final composite).
"""
import io

import numpy as np
from PIL import Image
from rembg import new_session, remove
from scipy import ndimage

from .hardware import select_device

# isnet-general-use trades a bit of speed for noticeably cleaner edges than
# the default u2net model on glossy/reflective product photography (steel,
# plastic appliances) — verified on real BaseLinker product photos.
_MODEL_NAME = "isnet-general-use"
_session = None
_session_device = None


def _get_session(use_gpu: bool = True):
    global _session, _session_device
    device = select_device(use_gpu)
    if _session is None or _session_device != device:
        # rembg's session picks up whichever onnxruntime execution
        # providers are actually installed automatically; this app only
        # installs the CPU onnxruntime package, so this always resolves
        # to CPU today regardless of `device` — see hardware.py.
        _session = new_session(_MODEL_NAME)
        _session_device = device
    return _session


def largest_component_mask(alpha: np.ndarray) -> np.ndarray:
    """Keeps only the largest connected blob of foreground pixels,
    discarding smaller stray islands from imperfect segmentation."""
    mask = alpha > 10
    labeled, n = ndimage.label(mask)
    if n <= 1:
        return mask
    sizes = ndimage.sum(mask, labeled, index=range(1, n + 1))
    largest_label = int(np.argmax(sizes)) + 1
    return labeled == largest_label


def mask_confidence(full_mask: np.ndarray, largest_mask: np.ndarray) -> int:
    """Heuristic 0-100 confidence proxy — rembg has no native confidence
    score, so this estimates segmentation quality from two signals:
    (1) how much of the raw mask survived largest-component filtering
    (a mask fragmented into many similar-sized pieces suggests confused
    segmentation), and (2) whether the kept region touches the image
    border (a product cut off by the frame edge is a bad capture, not
    just a segmentation artifact)."""
    total = int(full_mask.sum())
    kept = int(largest_mask.sum())
    if total == 0:
        return 0
    coherence = kept / total

    border_touch = (
        largest_mask[0, :].any() or largest_mask[-1, :].any()
        or largest_mask[:, 0].any() or largest_mask[:, -1].any()
    )
    border_penalty = 20 if border_touch else 0

    score = int(round(coherence * 100)) - border_penalty
    return max(0, min(100, score))


def detect(image: Image.Image, use_gpu: bool = True) -> tuple[Image.Image, np.ndarray, int]:
    """Runs segmentation and returns (rgba_image_with_clean_alpha,
    boolean_mask, confidence_0_100). rgba_image_with_clean_alpha has
    alpha zeroed outside the kept (largest) component so downstream
    stages never see the discarded noise pixels.
    """
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    cutout = remove(buf.getvalue(), session=_get_session(use_gpu))
    rgba = Image.open(io.BytesIO(cutout)).convert("RGBA")

    alpha = np.array(rgba.getchannel("A"))
    full_mask = alpha > 10
    mask = largest_component_mask(alpha)
    confidence = mask_confidence(full_mask, mask)

    clean_alpha = np.where(mask, alpha, 0).astype(np.uint8)
    rgba.putalpha(Image.fromarray(clean_alpha))

    return rgba, mask, confidence
