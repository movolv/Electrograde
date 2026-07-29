"""Lighting/tone enhancement: auto-levels, a small brightness/contrast
lift, optional denoise, and optional sharpening. Runs on the cropped
product's RGB channels only (alpha preserved separately) so the
autocontrast statistics aren't skewed by the white background added
later, and so denoise/sharpen aren't wasted on background pixels.
"""
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import PipelineConfig

# Denoising on a very large crop (e.g. a full-frame high-res phone photo
# where the product fills most of it) can take many seconds with
# fastNlMeansDenoisingColored; cap the working size and scale the result
# back up rather than let a single photo stall the pipeline.
_MAX_DENOISE_DIM = 1600

# Gray-world white balance can overcorrect a product that's genuinely a
# strong single color (e.g. a red kettle) by dragging it toward gray —
# capping the per-channel gain keeps it fixing lighting-cast color shifts
# (the common real case: phone-camera photos with a yellow/warm or blue/
# cool tint from indoor lighting) without distorting the product's actual
# color.
_MAX_WB_GAIN = 1.25
_MIN_WB_GAIN = 0.8


def _white_balance(rgba: Image.Image) -> Image.Image:
    """Gray-world white balance, computed only from product pixels (via
    alpha) so the surrounding transparent crop-corner filler doesn't skew
    the color statistics — corrects lighting color casts (common on
    phone-camera product photos) before tonal adjustments run."""
    rgb = np.array(rgba.convert("RGB")).astype(np.float32)
    alpha = np.array(rgba.getchannel("A")) > 10
    if alpha.sum() == 0:
        return rgba.convert("RGB")

    channel_means = [rgb[:, :, c][alpha].mean() for c in range(3)]
    gray_target = sum(channel_means) / 3
    for c in range(3):
        if channel_means[c] > 1e-3:
            gain = max(_MIN_WB_GAIN, min(_MAX_WB_GAIN, gray_target / channel_means[c]))
            rgb[:, :, c] *= gain

    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def _local_contrast_pop(rgb: Image.Image) -> Image.Image:
    """CLAHE (contrast-limited adaptive histogram equalization) on the
    LAB lightness channel — boosts local contrast/"pop" region-by-region
    rather than one flat global multiply, which is what gives a studio
    product photo its punchy-but-not-blown-out look. Applied on lightness
    only (a/b color channels untouched) so it doesn't shift color, just
    tonal contrast."""
    lab = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l = clahe.apply(l)
    merged = cv2.merge((l, a, b))
    return Image.fromarray(cv2.cvtColor(merged, cv2.COLOR_LAB2RGB))


def _denoise(rgb: Image.Image) -> Image.Image:
    if max(rgb.width, rgb.height) > _MAX_DENOISE_DIM:
        scale = _MAX_DENOISE_DIM / max(rgb.width, rgb.height)
        small = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))), Image.LANCZOS)
    else:
        scale = 1.0
        small = rgb

    arr = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2BGR)
    denoised = cv2.fastNlMeansDenoisingColored(arr, None, 4, 4, 7, 21)
    denoised_rgb = Image.fromarray(cv2.cvtColor(denoised, cv2.COLOR_BGR2RGB))

    if scale != 1.0:
        denoised_rgb = denoised_rgb.resize(rgb.size, Image.LANCZOS)
    return denoised_rgb


def _sharpen(rgb: Image.Image, strength: float) -> Image.Image:
    strength = max(0.0, min(1.0, strength))
    if strength == 0:
        return rgb
    return rgb.filter(ImageFilter.UnsharpMask(
        radius=2,
        percent=int(50 + 150 * strength),
        threshold=3,
    ))


def enhance(rgba: Image.Image, config: PipelineConfig) -> Image.Image:
    rgb = _white_balance(rgba)
    rgb = ImageOps.autocontrast(rgb, cutoff=1)
    rgb = _local_contrast_pop(rgb)
    rgb = ImageEnhance.Brightness(rgb).enhance(1.03)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.04)

    if config.denoise:
        rgb = _denoise(rgb)

    rgb = _sharpen(rgb, config.sharpening_strength)

    return Image.merge("RGBA", (*rgb.split(), rgba.getchannel("A")))
