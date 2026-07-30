"""Heuristic "surface reflectance profile" — deliberately NOT a trained
material classifier. Naming a product's literal material (stainless
steel vs. brushed aluminum vs. painted metal vs. fabric, etc.) from a
single photo is itself a hard, unsolved-in-general ML problem, and this
session already has one direct cautionary tale about reaching for a
heavy model to solve a hard visual problem: a real attempt to install
StableDelight (an open-source reflection-removal model) failed outright
on this machine's dependency stack. Adding a "material classifier" here
would be the same category of risk for the same category of benefit.

What IS implemented: two cheap, honest, per-image proxies — specular
highlight coverage and overall tonal range/brightness — that are enough
to pick a correction STRATEGY (how aggressively to run local contrast,
whether to allow exposure gamma at all, how strong to run defringe)
without pretending to identify the literal material. The returned
`profile` string names a correction bucket, not a material.
"""
import cv2
import numpy as np
from scipy import ndimage

PROFILE_GLOSSY = "glossy"
PROFILE_MATTE = "matte"
PROFILE_DARK_SOLID = "dark_solid"
PROFILE_BRIGHT_SOLID = "bright_solid"
PROFILE_NEUTRAL = "neutral"

_SPECULAR_SATURATION_THRESHOLD = 40
_SPECULAR_BRIGHTNESS_THRESHOLD = 235
# Mirrors reflections.py's own blob-size cap — only small, localized
# bright spots count as "specular glare," not a large legitimately-bright
# surface (verified necessary this session: without a size cap, a plain
# white plastic body was miscounted as "glossy/reflective").
_MAX_SPECULAR_BLOB_FRACTION = 0.02
_GLOSSY_COVERAGE_THRESHOLD = 0.01

_DARK_SOLID_LUMA_THRESHOLD = 85
_BRIGHT_SOLID_LUMA_THRESHOLD = 200


def analyze(rgb_arr: np.ndarray, mask: np.ndarray) -> dict:
    product_area = int(mask.sum())
    if product_area == 0:
        return {"profile": PROFILE_NEUTRAL, "specular_coverage": 0.0, "mean_luma": 0.0}

    hsv = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2HSV)
    value, saturation = hsv[:, :, 2], hsv[:, :, 1]
    gray = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)
    mean_luma = float(gray[mask].mean())

    candidate = (value > _SPECULAR_BRIGHTNESS_THRESHOLD) & (saturation < _SPECULAR_SATURATION_THRESHOLD) & mask
    labeled, n = ndimage.label(candidate)
    max_blob = max(4, int(product_area * _MAX_SPECULAR_BLOB_FRACTION))
    specular_pixels = 0
    if n > 0:
        sizes = ndimage.sum(candidate, labeled, index=range(1, n + 1))
        specular_pixels = sum(s for s in sizes if s <= max_blob)
    specular_coverage = specular_pixels / product_area

    if mean_luma < _DARK_SOLID_LUMA_THRESHOLD:
        profile = PROFILE_DARK_SOLID
    elif mean_luma > _BRIGHT_SOLID_LUMA_THRESHOLD:
        profile = PROFILE_BRIGHT_SOLID
    elif specular_coverage > _GLOSSY_COVERAGE_THRESHOLD:
        profile = PROFILE_GLOSSY
    else:
        profile = PROFILE_MATTE

    return {"profile": profile, "specular_coverage": float(specular_coverage), "mean_luma": mean_luma}


# Per-profile correction strategy. `gamma_enabled=False` for the two
# "solid" profiles is a deliberate second safety layer on top of
# exposure.py's own compressed-range check — belt and suspenders against
# repeating this session's black-robot-washed-to-gray bug.
_PRESETS = {
    PROFILE_GLOSSY: {"clahe_clip": 1.0, "clahe_blend": 0.25, "defringe_multiplier": 1.3, "gamma_enabled": True},
    PROFILE_MATTE: {"clahe_clip": 1.5, "clahe_blend": 0.4, "defringe_multiplier": 1.0, "gamma_enabled": True},
    PROFILE_DARK_SOLID: {"clahe_clip": 1.0, "clahe_blend": 0.2, "defringe_multiplier": 0.8, "gamma_enabled": False},
    PROFILE_BRIGHT_SOLID: {"clahe_clip": 1.0, "clahe_blend": 0.2, "defringe_multiplier": 0.8, "gamma_enabled": False},
    PROFILE_NEUTRAL: {"clahe_clip": 1.5, "clahe_blend": 0.4, "defringe_multiplier": 1.0, "gamma_enabled": True},
}


def get_preset(profile: str) -> dict:
    return _PRESETS.get(profile, _PRESETS[PROFILE_NEUTRAL])
