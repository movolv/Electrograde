"""Adaptive exposure analysis and correction: percentile-based histogram
read, adaptive gamma, and highlight/shadow recovery — every threshold
below is derived from the image's OWN measured statistics, never a flat
"add +10 brightness" constant.

The hard problem this module has to get right — and the reason it
exists as careful analysis rather than a one-line "if too dark, brighten
it" — is telling a genuinely UNDEREXPOSED photo apart from a well-lit
photo of a genuinely DARK (or bright) product. This session already hit
that exact trap once: naive autocontrast/CLAHE brightened a real black
robot vacuum from a mean luma of 120 to 142 because both algorithms only
look at "how is brightness distributed," not "is this product supposed
to be dark." The signal that actually distinguishes the two cases is
TONAL RANGE, not mean brightness: a dark product shot under decent
light still has bright highlights/reflections (a wide black-point-to-
white-point spread), while a truly underexposed photo compresses
*everything* toward the low end (a narrow spread). This module only
applies gamma correction when the range is narrow (genuinely
compressed/flat), never just because the mean is off-center.
"""
import cv2
import numpy as np
from PIL import Image

# Percentile (not literal min/max) black/white point — a handful of true
# 0/255 outlier pixels shouldn't swing the whole read.
_BLACK_PERCENTILE = 0.5
_WHITE_PERCENTILE = 99.5

_MID_GRAY_TARGET = 127.0
# How far mean luma must sit from mid-gray before treating it as a
# possible exposure problem at all — a product that's simply a bit dark
# or light shouldn't get touched.
_EXPOSURE_DEADBAND = 35.0
# Below this p0.5-to-p99.5 spread, the photo is using a compressed range
# regardless of where its mean sits — the real "underexposed" signal.
_COMPRESSED_RANGE_THRESHOLD = 140.0
_MAX_GAMMA_DELTA = 0.35  # caps how far computed gamma can move from 1.0

_CLIP_FREE_ALLOWANCE = 0.01  # fraction of pure-0/255 pixels tolerated before recovery kicks in
_MAX_RECOVERY_STRENGTH = 0.5


def analyze(rgb_arr: np.ndarray, mask: np.ndarray) -> dict:
    """Pure analysis, no mutation — also feeds ProcessingLog."""
    gray = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)
    values = gray[mask]
    if values.size == 0:
        return {
            "mean_luma": 0.0, "black_point": 0.0, "white_point": 255.0,
            "tonal_range": 255.0, "compressed": False,
            "clip_low_frac": 0.0, "clip_high_frac": 0.0,
        }

    hist, _ = np.histogram(values, bins=256, range=(0, 256))
    total = values.size

    return {
        "mean_luma": float(values.mean()),
        "black_point": float(np.percentile(values, _BLACK_PERCENTILE)),
        "white_point": float(np.percentile(values, _WHITE_PERCENTILE)),
        "tonal_range": float(np.percentile(values, _WHITE_PERCENTILE) - np.percentile(values, _BLACK_PERCENTILE)),
        "compressed": bool((np.percentile(values, _WHITE_PERCENTILE) - np.percentile(values, _BLACK_PERCENTILE)) < _COMPRESSED_RANGE_THRESHOLD),
        "clip_low_frac": float(hist[0] / total),
        "clip_high_frac": float(hist[255] / total),
    }


def compute_gamma(analysis: dict) -> float:
    """Returns the gamma to apply (1.0 = no-op). Only ever deviates from
    1.0 when BOTH the mean is off-center AND the tonal range is
    compressed — see module docstring for why the range check matters."""
    if not analysis["compressed"]:
        return 1.0
    deviation = analysis["mean_luma"] - _MID_GRAY_TARGET
    if abs(deviation) <= _EXPOSURE_DEADBAND:
        return 1.0
    excess = min(1.0, (abs(deviation) - _EXPOSURE_DEADBAND) / (128 - _EXPOSURE_DEADBAND))
    delta = _MAX_GAMMA_DELTA * excess
    return 1.0 + delta if deviation > 0 else 1.0 - delta


def apply_gamma(rgb: Image.Image, mask: np.ndarray, gamma: float) -> Image.Image:
    if abs(gamma - 1.0) < 1e-3:
        return rgb
    arr = np.array(rgb).astype(np.float32) / 255.0
    corrected = np.power(arr, gamma) * 255.0
    corrected = np.clip(corrected, 0, 255).astype(np.uint8)

    # Re-center on the original mean — gamma reshapes the tone curve;
    # this keeps that shape change without a residual net brightness
    # shift, same discipline as enhancer.py's other tone-mapping steps.
    orig_gray = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2GRAY)
    new_gray = cv2.cvtColor(corrected, cv2.COLOR_RGB2GRAY)
    diff = float(orig_gray[mask].mean() - new_gray[mask].mean())
    corrected = np.clip(corrected.astype(np.float32) + diff, 0, 255).astype(np.uint8)
    return Image.fromarray(corrected)


def apply_highlight_shadow_recovery(rgb: Image.Image, mask: np.ndarray, analysis: dict) -> Image.Image:
    """Gently compresses the extreme top/bottom of the tone curve toward
    mid-range, but ONLY proportional to how much *genuine* hard clipping
    (exact 0/255 pixels) was measured — a photo with no real clipping is
    left untouched rather than having its blacks/whites adjusted for no
    reason."""
    arr = np.array(rgb).astype(np.float32)

    high_excess = max(0.0, analysis["clip_high_frac"] - _CLIP_FREE_ALLOWANCE)
    low_excess = max(0.0, analysis["clip_low_frac"] - _CLIP_FREE_ALLOWANCE)
    if high_excess <= 0 and low_excess <= 0:
        return rgb

    highlight_strength = min(_MAX_RECOVERY_STRENGTH, high_excess * 20)
    shadow_strength = min(_MAX_RECOVERY_STRENGTH, low_excess * 20)

    if highlight_strength > 0:
        # Pull values above 200 down toward 200, proportional to strength.
        high_mask = arr > 200
        arr[high_mask] -= (arr[high_mask] - 200) * highlight_strength
    if shadow_strength > 0:
        low_mask = arr < 55
        arr[low_mask] += (55 - arr[low_mask]) * shadow_strength

    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
