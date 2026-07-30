"""Chromatic-aberration edge defringe — the realistic subset of "lens
correction" this pipeline attempts. Full lens distortion / CA calibration
needs a per-lens profile (keyed off the exact phone/lens) to avoid making
distortion worse rather than better; without one, generic correction is
guesswork. That's out of scope here (see the approved plan's Risks
section). What IS real and doesn't need a calibration profile: CA's
visible symptom is a thin purple/magenta or green fringe running along
high-contrast edges (the lens focusing different wavelengths slightly
differently). This module finds strong luminance edges, then within a
thin band around them, flags pixels whose hue sits in the fringe-
characteristic ranges with anomalously high saturation, and desaturates
just those pixels.

Restricting the fix to a thin edge band (not "any purple/green pixel")
is what keeps this from graying out a genuinely purple or green product
surface — a real product color fills a region, not a thin band hugging
a contrast edge.
"""
import cv2
import numpy as np
from PIL import Image

# Edge band kept thin deliberately (see module docstring): wide enough to
# catch a fringe line, narrow enough that no ordinary product surface
# color falls inside it.
_EDGE_DILATE_PX = 2
_EDGE_THRESHOLD_PERCENTILE = 90  # top decile of gradient magnitude = "strong edge"

# Hue ranges (OpenCV's 0-179 scale) characteristic of CA fringing: purple/
# magenta and green, the two complementary colors typical of axial/
# lateral chromatic aberration.
_PURPLE_HUE_RANGE = (135, 170)
_GREEN_HUE_RANGE = (35, 85)
_FRINGE_SATURATION_THRESHOLD = 60


def _edge_band(gray: np.ndarray) -> np.ndarray:
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(grad_x, grad_y)
    threshold = np.percentile(magnitude, _EDGE_THRESHOLD_PERCENTILE)
    edges = (magnitude >= threshold).astype(np.uint8)
    kernel = np.ones((_EDGE_DILATE_PX * 2 + 1, _EDGE_DILATE_PX * 2 + 1), np.uint8)
    return cv2.dilate(edges, kernel) > 0


def _fringe_pixels(hsv: np.ndarray, edge_band: np.ndarray) -> np.ndarray:
    hue, saturation = hsv[:, :, 0], hsv[:, :, 1]
    purple = (hue >= _PURPLE_HUE_RANGE[0]) & (hue <= _PURPLE_HUE_RANGE[1])
    green = (hue >= _GREEN_HUE_RANGE[0]) & (hue <= _GREEN_HUE_RANGE[1])
    saturated = saturation >= _FRINGE_SATURATION_THRESHOLD
    return edge_band & saturated & (purple | green)


def reduce_fringing(rgb: Image.Image, mask: np.ndarray, strength: float) -> Image.Image:
    """Desaturates thin-edge-band purple/green chromatic-aberration
    fringe pixels within the product mask, proportional to `strength`
    (0=off, 1=full desaturation of flagged pixels only)."""
    strength = max(0.0, min(1.0, strength))
    if strength == 0:
        return rgb

    arr = np.array(rgb)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)

    edge_band = _edge_band(gray) & mask
    if not edge_band.any():
        return rgb

    fringe = _fringe_pixels(hsv, edge_band)
    if not fringe.any():
        return rgb

    hsv = hsv.astype(np.float32)
    hsv[:, :, 1] = np.where(fringe, hsv[:, :, 1] * (1.0 - strength), hsv[:, :, 1])
    result = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2RGB)
    return Image.fromarray(result)
