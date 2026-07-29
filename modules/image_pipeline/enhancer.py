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
    rgb = rgba.convert("RGB")
    rgb = ImageOps.autocontrast(rgb, cutoff=1)
    rgb = ImageEnhance.Brightness(rgb).enhance(1.05)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.08)

    if config.denoise:
        rgb = _denoise(rgb)

    rgb = _sharpen(rgb, config.sharpening_strength)

    return Image.merge("RGBA", (*rgb.split(), rgba.getchannel("A")))
