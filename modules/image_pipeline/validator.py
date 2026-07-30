"""Heuristic quality scoring — classical CV metrics only (no ML quality
model is available locally). Sharpness/exposure/noise/color-cast are
measured on the cropped product itself (pre-composite) since they're
about capture/enhancement quality; centering/occupancy/background are
measured from the deterministic placement math background.composite()
already did, not a second segmentation pass on the finished canvas.

Every added sub-score keeps the same honesty discipline as the original
four: each measures the closest thing actually computable with classical
CV, not the literal named concept — e.g. `color_cast` is a residual-tint
proxy standing in for "color accuracy," which would need a reference
target this pipeline never has.
"""
import cv2
import numpy as np
from PIL import Image

from .models import QualityScore

_SHARPNESS_REFERENCE_DIM = 1000  # px, long side — see _sharpness_score
_NOISE_KERNEL = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)


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


def _noise_score(product_rgb: np.ndarray, product_mask: np.ndarray) -> int:
    """Immerkær's fast noise estimator (same formula used adaptively in
    enhancer.py to size denoise strength, duplicated here rather than
    imported to keep validator.py's scoring independent of enhancer.py's
    correction logic — the two modules answer different questions and
    shouldn't need to change together)."""
    gray = cv2.cvtColor(product_rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    if h < 3 or w < 3:
        return 100
    conv = cv2.filter2D(gray.astype(np.float32), -1, _NOISE_KERNEL, borderType=cv2.BORDER_REFLECT)
    interior = np.zeros_like(product_mask)
    interior[1:-1, 1:-1] = True
    valid = product_mask & interior
    if not valid.any():
        return 100
    sigma = float(np.sqrt(np.pi / 2) * np.abs(conv[valid]).mean() / 6)
    return max(0, min(100, int(100 - sigma * 8)))


def _color_cast_score(product_rgb: np.ndarray, product_mask: np.ndarray) -> int:
    """Proxy for "color accuracy": measures how far the product's own
    channel means sit from gray-world neutral. This can't detect a color
    cast that gray-world white balance is fundamentally blind to (e.g. a
    product that's uniformly one saturated color), but it does catch a
    residual uncorrected lighting tint surviving enhancer.py's white
    balance — the failure mode this score exists to catch."""
    if product_mask.sum() == 0:
        return 100
    channel_means = [float(product_rgb[:, :, c][product_mask].mean()) for c in range(3)]
    gray_target = sum(channel_means) / 3
    if gray_target < 1e-3:
        return 100
    deviation = max(abs(m - gray_target) for m in channel_means) / gray_target
    return max(0, min(100, int(100 - deviation * 300)))


def _background_quality_score(canvas_rgb: Image.Image, placement_box: tuple, canvas_size: int, background_color: tuple) -> int:
    """Samples the finished canvas OUTSIDE the product's placement box
    (with a small margin so soft product edges aren't counted) and
    measures how close those pixels are to the configured background
    color — catches leftover color cast, compositing artifacts, or a
    non-uniform background that a passing sharpness/exposure score alone
    wouldn't flag."""
    arr = np.array(canvas_rgb.convert("RGB")).astype(np.float32)
    left, top, right, bottom = (int(v) for v in placement_box)
    margin = 10
    sample_mask = np.ones((canvas_size, canvas_size), dtype=bool)
    sample_mask[max(0, top - margin):min(canvas_size, bottom + margin), max(0, left - margin):min(canvas_size, right + margin)] = False
    if not sample_mask.any():
        return 100
    target = np.array(background_color, dtype=np.float32)
    deviation = float(np.abs(arr[sample_mask] - target).mean())
    return max(0, min(100, int(100 - deviation * 4)))


def _edge_quality_score(alpha: np.ndarray) -> int:
    """Fraction of semi-transparent (partially antialiased) pixels
    relative to total product-mask size — a clean, thin antialiased edge
    has a small soft-pixel fraction; a wide or ragged segmentation halo
    (poor rembg confidence, motion blur at the product boundary) has
    proportionally much more."""
    opaque = alpha > 10
    total = int(opaque.sum())
    if total == 0:
        return 100
    soft = int((opaque & (alpha < 245)).sum())
    soft_fraction = soft / total
    return max(0, min(100, int(100 - soft_fraction * 400)))


def validate(
    product_rgb: Image.Image,
    placement_box: tuple,
    canvas_size: int,
    target_occupancy: float,
    canvas_rgb: Image.Image = None,
    background_color: tuple = (255, 255, 255),
) -> QualityScore:
    alpha = np.array(product_rgb.getchannel("A")) if product_rgb.mode == "RGBA" else None
    product_mask = alpha > 10 if alpha is not None else np.ones(product_rgb.size[::-1], dtype=bool)
    arr = np.array(product_rgb.convert("RGB"))

    sharpness = _sharpness_score(arr, product_mask)
    exposure = _exposure_score(arr, product_mask)
    centering = _centering_score(placement_box, canvas_size)
    occupancy = _occupancy_score(placement_box, canvas_size, target_occupancy)
    noise = _noise_score(arr, product_mask)
    color_cast = _color_cast_score(arr, product_mask)
    edge_quality = _edge_quality_score(alpha) if alpha is not None else 100
    background_quality = (
        _background_quality_score(canvas_rgb, placement_box, canvas_size, background_color)
        if canvas_rgb is not None else 100
    )

    overall = int(round(
        sharpness * 0.25 + exposure * 0.20 + centering * 0.10 + occupancy * 0.10
        + noise * 0.10 + color_cast * 0.10 + background_quality * 0.10 + edge_quality * 0.05
    ))
    return QualityScore(
        overall=overall, sharpness=sharpness, exposure=exposure,
        centering=centering, occupancy=occupancy, noise=noise,
        color_cast=color_cast, background_quality=background_quality,
        edge_quality=edge_quality,
    )
