"""Detail-preservation safety check: verifies enhancement hasn't
measurably destroyed local texture/detail — a proxy for "a scratch,
label, or serial number might now be less legible," directly serving
the project's CRITICAL RULE that this pipeline must never alter what a
product actually looks like. Uses SSIM (structural similarity) between
the original and enhanced product region, restricted to product-mask
pixels only.

Global SSIM over the whole rectangular crop is deliberately NOT used:
that would mix in trivial agreement on background-filled corner pixels
neither enhancement stage touches, silently inflating the score exactly
the way un-masked brightness statistics once did in `enhancer.py`
(the black-robot bug). Instead, skimage's `full=True` mode returns a
local similarity map the same shape as the input, and this module
averages that map over product-mask pixels only.
"""
import cv2
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity


def measure_similarity(original_rgb: Image.Image, enhanced_rgb: Image.Image, mask: np.ndarray) -> float:
    """Returns an SSIM score in [0,1] (1.0 = identical) computed over
    product-mask pixels only. Grayscale comparison — texture/detail
    preservation is the concern here, not color."""
    if mask.sum() == 0:
        return 1.0

    original_gray = cv2.cvtColor(np.array(original_rgb.convert("RGB")), cv2.COLOR_RGB2GRAY)
    enhanced_gray = cv2.cvtColor(np.array(enhanced_rgb.convert("RGB")), cv2.COLOR_RGB2GRAY)

    _, similarity_map = structural_similarity(original_gray, enhanced_gray, full=True, data_range=255)
    return float(similarity_map[mask].mean())


def constrain_to_safe_strength(similarity: float, min_similarity: float, current_strength: float) -> float:
    """If measured similarity has fallen below the configured floor,
    scales back an enhancement strength value (sharpen/clarity/contrast
    blend, etc.) proportionally — a soft backoff rather than an all-or-
    nothing revert, so a borderline case loses only as much strength as
    needed rather than the whole enhancement."""
    if similarity >= min_similarity:
        return current_strength
    deficit = (min_similarity - similarity) / max(min_similarity, 1e-6)
    return max(0.0, current_strength * (1.0 - min(1.0, deficit)))
