"""Lighting/tone enhancement: white balance, adaptive exposure, auto-
levels, local contrast, vibrance, clarity/texture, adaptive denoise and
sharpen. Runs on the cropped product's RGB channels only (alpha
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
unwanted global brightening. Every new adaptive stage added below
(exposure, material-aware presets, adaptive denoise/sharpen) follows the
same discipline rather than re-deriving it.
"""
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from . import defringe, exposure, material
from .config import PipelineConfig

# Denoising on a very large crop (e.g. a full-frame high-res phone photo
# where the product fills most of it) can take many seconds with
# fastNlMeansDenoisingColored; cap the working size and scale the result
# back up rather than let a single photo stall the pipeline.
_MAX_DENOISE_DIM = 1600
_FIXED_DENOISE_H = 4.0  # used when config.adaptive_sharpen_denoise is False

# Gray-world white balance can overcorrect a product that's genuinely a
# strong single color (e.g. a red kettle) by dragging it toward gray —
# capping the per-channel gain keeps it fixing lighting-cast color shifts
# (the common real case: phone-camera photos with a yellow/warm or blue/
# cool tint from indoor lighting) without distorting the product's actual
# color.
_MAX_WB_GAIN = 1.25
_MIN_WB_GAIN = 0.8

# Adaptive sharpen/denoise strength thresholds — Laplacian variance and
# Immerkær noise-sigma ranges observed on this session's real BaseLinker
# test photos. Below/above these, strength saturates at its min/max
# rather than extrapolating indefinitely.
#
# _SHARP_VARIANCE_FLOOR matters specifically: verified on a real, very
# soft/low-detail source photo (a phone photo of a De'Longhi coffee
# machine, measured variance ~14) that boosting sharpen strength UP for
# low measured sharpness — the original design here — assumed "soft"
# always means "camera focus could be compensated for." Below this floor
# it more likely means the source genuinely lacks fine detail (heavy
# upstream compression, real defocus/motion blur), and no unsharp mask
# recovers detail that was never captured. Pushing strength up in that
# regime instead amplified noise/JPEG blocking into visible halos and a
# posterized, cartoon-outline look once stacked with this pipeline's
# other sharpening stages (clarity, and background.py's own extra
# upscale-path sharpen). Below the floor, strength now eases off instead
# of ramping up.
_SHARP_VARIANCE_FLOOR = 25.0
_SHARP_VARIANCE_LOW = 150.0
_SHARP_VARIANCE_HIGH = 600.0
_NOISE_SIGMA_LOW = 1.5
_NOISE_SIGMA_HIGH = 8.0

_NOISE_KERNEL = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)


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


def _local_contrast_pop(rgb: Image.Image, mask: np.ndarray, clip_limit: float = 1.5, blend: float = 0.4) -> Image.Image:
    """CLAHE (contrast-limited adaptive histogram equalization) on the
    LAB lightness channel — boosts local contrast/"pop" region-by-region
    rather than one flat global multiply. Applied on lightness only (a/b
    color channels untouched) so it doesn't shift color, just tonal
    contrast. `clip_limit`/`blend` are set per material.py's reflectance
    preset (e.g. gentler on glossy surfaces, which already have strong
    natural contrast from specular highlights). Re-centered onto the
    original mean brightness afterward — see module docstring."""
    original_arr = np.array(rgb)
    lab = cv2.cvtColor(original_arr, cv2.COLOR_RGB2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    l_clahe = clahe.apply(l)
    l_blended = cv2.addWeighted(l_clahe, blend, l, 1.0 - blend, 0)
    merged = cv2.merge((l_blended, a, b))
    result_arr = cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)
    corrected = _restore_mean_brightness(original_arr, result_arr, mask)
    return Image.fromarray(corrected)


def _vibrance(rgb: Image.Image, boost: float) -> Image.Image:
    """Per-pixel HSV saturation-dependent gain, replacing a flat
    ImageEnhance.Color multiply: already-vivid pixels get little/no extra
    boost while low-saturation pixels get boosted more, tapering smoothly
    to no boost as saturation approaches maximum — the standard
    definition distinguishing "vibrance" from a flat saturation lift, and
    why a product that's mostly one bold color doesn't get pushed toward
    an oversaturated, unrealistic look."""
    arr = np.array(rgb)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV).astype(np.float32)
    saturation = hsv[:, :, 1]
    extra = max(0.0, boost - 1.0)
    taper = 1.0 - (saturation / 255.0)
    gain = 1.0 + extra * taper
    hsv[:, :, 1] = np.clip(saturation * gain, 0, 255)
    result = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
    return Image.fromarray(result)


def _clarity(rgb: Image.Image, mask: np.ndarray, strength: float) -> Image.Image:
    """Large-radius, low-percent unsharp mask — boosts a mid/low
    frequency "texture crispness" distinct from CLAHE's per-tile local
    contrast (much smaller effective radius) and `_sharpen`'s
    high-frequency edge sharpening (much smaller radius still).
    Re-centered onto the original mean brightness, same discipline as
    every other tone step here.

    Computed on a downsampled copy (same _MAX_DENOISE_DIM cap and
    resize-back pattern `_denoise` already uses): a large-radius blur is
    inherently a low-frequency effect, so full source resolution buys no
    extra accuracy here — only meaningfully lower latency on high-
    resolution phone photos, which mattered to stay inside this
    pipeline's per-photo performance budget."""
    strength = max(0.0, min(1.0, strength))
    if strength == 0:
        return rgb

    original_arr = np.array(rgb)
    if max(rgb.width, rgb.height) > _MAX_DENOISE_DIM:
        scale = _MAX_DENOISE_DIM / max(rgb.width, rgb.height)
        small = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))), Image.LANCZOS)
    else:
        scale = 1.0
        small = rgb

    clarity_small = small.filter(ImageFilter.UnsharpMask(radius=25, percent=int(30 * strength), threshold=2))
    clarity_img = clarity_small.resize(rgb.size, Image.LANCZOS) if scale != 1.0 else clarity_small

    result_arr = _restore_mean_brightness(original_arr, np.array(clarity_img), mask)
    return Image.fromarray(result_arr)


def _estimate_sharpness(gray: np.ndarray, mask: np.ndarray) -> float:
    """Laplacian variance over product pixels, normalized to a fixed
    reference size — reuses the exact resolution-independence fix
    validated in validator.py, so the adaptive sharpen strength below is
    sized from a genuinely comparable measurement, not one that varies
    with source photo resolution."""
    h, w = gray.shape
    scale = 1000 / max(w, h)
    small_gray = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(
        mask.astype(np.uint8), (small_gray.shape[1], small_gray.shape[0]), interpolation=cv2.INTER_NEAREST
    ) > 0
    values = cv2.Laplacian(small_gray, cv2.CV_64F)[small_mask]
    return float(values.var()) if values.size else 0.0


def _estimate_noise(gray: np.ndarray, mask: np.ndarray) -> float:
    """Immerkær's fast noise estimator: convolve with a fixed Laplacian-
    like kernel whose response on pure noise has a known relationship to
    the noise's standard deviation, then scale accordingly. Restricted to
    interior (non-border) product-mask pixels — the standard formula
    already excludes a 1px border where the convolution isn't well
    defined."""
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    conv = cv2.filter2D(gray.astype(np.float32), -1, _NOISE_KERNEL, borderType=cv2.BORDER_REFLECT)
    interior = np.zeros_like(mask)
    interior[1:-1, 1:-1] = True
    valid = mask & interior
    if not valid.any():
        return 0.0
    return float(np.sqrt(np.pi / 2) * np.abs(conv[valid]).mean() / 6)


def _adaptive_sharpen_strength(variance: float, base_strength: float) -> float:
    """Already-sharp photos (high measured variance) get less unsharp
    masking to avoid visible halos from over-sharpening; moderately soft
    photos get more, up to the configured base strength's ceiling. Below
    _SHARP_VARIANCE_FLOOR, strength eases back off instead of continuing
    to ramp up — see the floor's own comment for why."""
    if variance <= _SHARP_VARIANCE_FLOOR:
        factor = 0.5
    elif variance <= _SHARP_VARIANCE_LOW:
        t = (variance - _SHARP_VARIANCE_FLOOR) / (_SHARP_VARIANCE_LOW - _SHARP_VARIANCE_FLOOR)
        factor = 0.5 + t * 0.9  # ramps 0.5x -> 1.4x across the "moderately soft" band
    elif variance >= _SHARP_VARIANCE_HIGH:
        factor = 0.4
    else:
        t = (variance - _SHARP_VARIANCE_LOW) / (_SHARP_VARIANCE_HIGH - _SHARP_VARIANCE_LOW)
        factor = 1.4 - t * 1.0  # 1.4x at the low end down to 0.4x at the high end
    return max(0.0, min(1.0, base_strength * factor))


def _adaptive_denoise_h(sigma: float) -> float:
    """Maps a measured noise sigma to fastNlMeansDenoisingColored's `h`
    strength parameter — clean photos get a light touch, genuinely noisy
    ones get proportionally more, capped at both ends."""
    if sigma <= _NOISE_SIGMA_LOW:
        return 2.0
    if sigma >= _NOISE_SIGMA_HIGH:
        return 10.0
    t = (sigma - _NOISE_SIGMA_LOW) / (_NOISE_SIGMA_HIGH - _NOISE_SIGMA_LOW)
    return 2.0 + t * 8.0


def _denoise(rgb: Image.Image, h: float) -> Image.Image:
    if max(rgb.width, rgb.height) > _MAX_DENOISE_DIM:
        scale = _MAX_DENOISE_DIM / max(rgb.width, rgb.height)
        small = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))), Image.LANCZOS)
    else:
        scale = 1.0
        small = rgb

    arr = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2BGR)
    denoised = cv2.fastNlMeansDenoisingColored(arr, None, h, h, 7, 21)
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

    if config.material_aware:
        profile = material.analyze(np.array(rgba.convert("RGB")), mask)
        preset = material.get_preset(profile["profile"])
    else:
        preset = material.get_preset(material.PROFILE_NEUTRAL)

    rgb = _white_balance(rgba, mask)

    if config.adaptive_exposure and preset["gamma_enabled"]:
        analysis = exposure.analyze(np.array(rgb), mask)
        gamma = exposure.compute_gamma(analysis)
        rgb = exposure.apply_gamma(rgb, mask, gamma)
        rgb = exposure.apply_highlight_shadow_recovery(rgb, mask, analysis)

    rgb = _soft_autocontrast(rgb, mask)
    rgb = _local_contrast_pop(rgb, mask, clip_limit=preset["clahe_clip"], blend=preset["clahe_blend"])
    rgb = ImageEnhance.Contrast(rgb).enhance(1.04)

    if config.vibrance_mode:
        rgb = _vibrance(rgb, config.saturation_boost)
    else:
        rgb = ImageEnhance.Color(rgb).enhance(config.saturation_boost)

    if config.defringe_strength > 0:
        rgb = defringe.reduce_fringing(rgb, mask, config.defringe_strength * preset["defringe_multiplier"])

    if config.denoise:
        gray = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2GRAY)
        if config.adaptive_sharpen_denoise:
            denoise_h = _adaptive_denoise_h(_estimate_noise(gray, mask))
        else:
            denoise_h = _FIXED_DENOISE_H
        rgb = _denoise(rgb, denoise_h)

    if config.adaptive_sharpen_denoise:
        gray = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2GRAY)
        sharpen_strength = _adaptive_sharpen_strength(_estimate_sharpness(gray, mask), config.sharpening_strength)
    else:
        sharpen_strength = config.sharpening_strength

    rgb = _clarity(rgb, mask, sharpen_strength * 0.5)
    rgb = _sharpen(rgb, sharpen_strength)

    return Image.merge("RGBA", (*rgb.split(), rgba.getchannel("A")))
