"""Lighting/tone enhancement: auto-levels, local contrast, a small
brightness/contrast/saturation lift, optional denoise, and optional
sharpening. Runs on the cropped product's RGB channels only (alpha
preserved separately).

Every tone-mapping step below is restricted to product-only pixels via
the alpha mask, and preserves the product's original mean brightness.
Both matter and were verified necessary on a real black robot vacuum
this session: (1) the rectangular crop's corners (outside the product's
actual silhouette but inside its bounding box) still carry the original
photo's background pixels — PIL's RGBA->RGB conversion only drops alpha,
it doesn't zero the RGB underneath — so computing statistics over the
whole crop let bright studio-background corner pixels skew the "how much
to stretch/equalize this photo" decision; (2) even mask-restricted,
autocontrast/CLAHE still redistribute brightness based on the image's
*shape* (histogram), and for a product whose true color is simply dark,
that redistribution reads as "photo needs fixing" and brightens it well
past its real tone (measured: mean product brightness rose from 120 to
142 through autocontrast alone) — re-centering back onto the original
mean after each step keeps the local-contrast benefit without the
unwanted global brightening.
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


def _product_mask(rgba: Image.Image) -> np.ndarray:
    return np.array(rgba.getchannel("A")) > 10


def _mean_luma(rgb_arr: np.ndarray, mask: np.ndarray) -> float:
    return cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)[mask].mean()


def _restore_mean_brightness(original_arr: np.ndarray, transformed_arr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Shifts `transformed_arr` so its mean luma over `mask` matches
    `original_arr`'s — keeps whatever local-contrast shape change the
    transform made, without a net brightening/darkening."""
    diff = _mean_luma(original_arr, mask) - _mean_luma(transformed_arr, mask)
    return np.clip(transformed_arr.astype(np.float32) + diff, 0, 255).astype(np.uint8)


def _white_balance(rgba: Image.Image, mask: np.ndarray) -> Image.Image:
    """Gray-world white balance, computed only from product pixels (via
    alpha) so the surrounding transparent crop-corner filler doesn't skew
    the color statistics — corrects lighting color casts (common on
    phone-camera product photos) before tonal adjustments run."""
    rgb = np.array(rgba.convert("RGB")).astype(np.float32)
    if mask.sum() == 0:
        return rgba.convert("RGB")

    channel_means = [rgb[:, :, c][mask].mean() for c in range(3)]
    gray_target = sum(channel_means) / 3
    for c in range(3):
        if channel_means[c] > 1e-3:
            gain = max(_MIN_WB_GAIN, min(_MAX_WB_GAIN, gray_target / channel_means[c]))
            rgb[:, :, c] *= gain

    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def _soft_autocontrast(rgb: Image.Image, mask: np.ndarray) -> Image.Image:
    """A mild, brightness-preserving local-contrast stretch: PIL's
    ImageOps.autocontrast always stretches whatever range survives its
    cutoff out to the full 0-255, which brightens a naturally dark/low-
    contrast product well past its true tone. Blending at 30% strength
    and then re-centering onto the original mean keeps a bit of the
    "pop" without the unwanted global brightening."""
    original_arr = np.array(rgb)
    stretched = ImageOps.autocontrast(rgb, cutoff=1)
    blended = Image.blend(rgb, stretched, 0.3)
    corrected = _restore_mean_brightness(original_arr, np.array(blended), mask)
    return Image.fromarray(corrected)


def _local_contrast_pop(rgb: Image.Image, mask: np.ndarray) -> Image.Image:
    """CLAHE (contrast-limited adaptive histogram equalization) on the
    LAB lightness channel — boosts local contrast/"pop" region-by-region
    rather than one flat global multiply. Applied on lightness only (a/b
    color channels untouched) so it doesn't shift color, just tonal
    contrast. Re-centered onto the original mean brightness afterward —
    see module docstring."""
    original_arr = np.array(rgb)
    lab = cv2.cvtColor(original_arr, cv2.COLOR_RGB2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    l_clahe = clahe.apply(l)
    l_blended = cv2.addWeighted(l_clahe, 0.4, l, 0.6, 0)
    merged = cv2.merge((l_blended, a, b))
    result_arr = cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)
    corrected = _restore_mean_brightness(original_arr, result_arr, mask)
    return Image.fromarray(corrected)


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
    mask = _product_mask(rgba)

    rgb = _white_balance(rgba, mask)
    rgb = _soft_autocontrast(rgb, mask)
    rgb = _local_contrast_pop(rgb, mask)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.04)
    rgb = ImageEnhance.Color(rgb).enhance(config.saturation_boost)

    if config.denoise:
        rgb = _denoise(rgb)

    rgb = _sharpen(rgb, config.sharpening_strength)

    return Image.merge("RGBA", (*rgb.split(), rgba.getchannel("A")))
