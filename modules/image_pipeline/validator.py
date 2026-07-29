"""Heuristic quality scoring — classical CV metrics only (no ML quality
model is available locally). Sharpness/exposure are measured on the
cropped product itself (pre-composite) since they're about capture
quality; centering/occupancy are measured from the deterministic
placement math background.composite() already did, not a second
segmentation pass on the finished canvas.
"""
import cv2
import numpy as np
from PIL import Image

from .models import QualityScore


_SHARPNESS_REFERENCE_DIM = 1000  # px, long side — see _sharpness_score


def _sharpness_score(product_rgb: np.ndarray, product_mask: np.ndarray) -> int:
    gray = cv2.cvtColor(product_rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape

    # Laplacian variance is resolution-dependent, not just blur-dependent
    # — verified on real studio photos this session: the SAME genuinely
    # sharp photo scored ~40 at its native ~3000px crop size but ~450-800
    # once resized to a standard reference size first (smooth product
    # surfaces contribute proportionally more near-zero-variance pixels
    # at higher resolutions, dragging the average down regardless of
    # actual focus). Normalizing to a fixed reference size before scoring
    # makes the metric comparable across different source resolutions.
    scale = _SHARPNESS_REFERENCE_DIM / max(w, h)
    small_gray = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(
        product_mask.astype(np.uint8), (small_gray.shape[1], small_gray.shape[0]), interpolation=cv2.INTER_NEAREST
    ) > 0

    values = cv2.Laplacian(small_gray, cv2.CV_64F)[small_mask]
    if values.size == 0:
        return 0
    variance = values.var()
    # Empirically (real BaseLinker studio photos, post-normalization):
    # sharp in-focus product photos scored 300-1000 at this reference
    # size across several real test photos this session.
    return max(0, min(100, int(variance / 6)))


_CLIPPING_FREE_ALLOWANCE = 0.10  # fraction of pure-0/pure-255 pixels tolerated before any penalty


def _exposure_score(product_rgb: np.ndarray, product_mask: np.ndarray) -> int:
    gray = cv2.cvtColor(product_rgb, cv2.COLOR_RGB2GRAY)
    values = gray[product_mask]
    if values.size == 0:
        return 0
    # Only the EXACT 0/255 bins count as hard clipping (genuine sensor
    # saturation/lost detail) — a wider "near white/black" band (the
    # original approach) flagged real product photos as "overexposed"
    # just for containing legitimately white or black components (e.g.
    # a white plastic steamer body: ~26% of masked pixels sat at
    # brightness 254 on a perfectly good studio photo this session,
    # while the true 255 bin held under 0.01% — that's a light-colored
    # product, not blown highlights). A tolerance allowance before any
    # penalty kicks in absorbs the fact that black/white product photos
    # naturally have some pixels at the extremes without being "badly
    # exposed" — verified against real BaseLinker studio photos.
    hist, _ = np.histogram(values, bins=256, range=(0, 256))
    total = values.size
    hard_clipping = (hist[0] + hist[255]) / total
    over_allowance = max(0.0, hard_clipping - _CLIPPING_FREE_ALLOWANCE)
    return max(0, min(100, int(100 - over_allowance * 300)))


def _centering_score(placement_box: tuple, canvas_size: int) -> int:
    left, top, right, bottom = placement_box
    cx, cy = (left + right) / 2, (top + bottom) / 2
    half = canvas_size / 2
    offset = (abs(cx - half) / half + abs(cy - half) / half) / 2
    return max(0, min(100, int(100 * (1 - offset))))


def _occupancy_score(placement_box: tuple, canvas_size: int, target_occupancy: float) -> int:
    left, top, right, bottom = placement_box
    product_area = (right - left) * (bottom - top)
    actual_occupancy = product_area / (canvas_size * canvas_size)
    diff = abs(actual_occupancy - target_occupancy)
    return max(0, min(100, int(100 - diff * 300)))


def validate(
    product_rgb: Image.Image,
    placement_box: tuple,
    canvas_size: int,
    target_occupancy: float,
) -> QualityScore:
    product_mask = np.array(product_rgb.getchannel("A")) > 10 if product_rgb.mode == "RGBA" else np.ones(product_rgb.size[::-1], dtype=bool)
    arr = np.array(product_rgb.convert("RGB"))
    sharpness = _sharpness_score(arr, product_mask)
    exposure = _exposure_score(arr, product_mask)
    centering = _centering_score(placement_box, canvas_size)
    occupancy = _occupancy_score(placement_box, canvas_size, target_occupancy)

    overall = int(round(
        sharpness * 0.4 + exposure * 0.3 + centering * 0.15 + occupancy * 0.15
    ))
    return QualityScore(
        overall=overall, sharpness=sharpness, exposure=exposure,
        centering=centering, occupancy=occupancy,
    )
