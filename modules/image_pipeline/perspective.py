"""Perspective correction — scoped honestly to in-plane rotation only.
True 3D perspective correction (like a document scanner) needs a known
reference plane or multiple viewpoints to compute a homography; for an
arbitrary, irregularly-shaped consumer-electronics product photographed
from one angle, there's no reliable way to infer that automatically. What
IS reliably detectable is simple in-plane tilt (the product photographed
at a slight rotation relative to the camera) via the mask's minimum-area
bounding rectangle — that's what this module does.
"""
import cv2
import numpy as np
from PIL import Image

_MIN_CORRECTION_DEGREES = 1.0  # skip rotation below this — not worth the resample blur

# minAreaRect's angle is only meaningful for shapes that are actually
# close to rectangular — for a round/oval product (verified this session:
# a steamer's oval lid), the minimal bounding rectangle can sit at nearly
# any angle with almost equal validity, so "straightening" to it produces
# an arbitrary, wrong-looking rotation instead of a correction. Extent
# (contour area / bounding-rect area) close to 1.0 means "fills its
# rectangle" (a real rectangle scores ~1.0; a circle/oval scores ~0.78;
# an irregular blob lower still) — only trust the angle above this.
_MIN_RECTANGULARITY = 0.85


def _normalize_angle(angle: float) -> float:
    """Reduces any raw minAreaRect angle to the smallest equivalent
    correction in (-45, 45] — makes this robust to the angle-convention
    differences between OpenCV versions (the box is a valid rectangle
    around the contour either way; a +/-90 relabeling of which side
    counts as "width" doesn't change what rotation straightens it)."""
    while angle > 45:
        angle -= 90
    while angle <= -45:
        angle += 90
    return angle


def correct_perspective(rgba: Image.Image, mask: np.ndarray) -> tuple[Image.Image, np.ndarray, bool]:
    """Returns (possibly-rotated rgba, updated mask, corrected: bool).
    No-ops (returns inputs unchanged, corrected=False) if the mask is
    empty, degenerate, or already close enough to axis-aligned.
    """
    mask_u8 = mask.astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return rgba, mask, False

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < 10:
        return rgba, mask, False

    rect = cv2.minAreaRect(largest)
    (rect_w, rect_h) = rect[1]
    rect_area = rect_w * rect_h
    if rect_area <= 0:
        return rgba, mask, False

    rectangularity = cv2.contourArea(largest) / rect_area
    if rectangularity < _MIN_RECTANGULARITY:
        return rgba, mask, False

    angle = _normalize_angle(rect[-1])

    if abs(angle) < _MIN_CORRECTION_DEGREES:
        return rgba, mask, False

    rotated = rgba.rotate(-angle, resample=Image.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))
    rotated_mask = np.array(rotated.getchannel("A")) > 10
    return rotated, rotated_mask, True
