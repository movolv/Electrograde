"""Crop-to-mask, centered scaling (with an upscale cap to avoid blur —
verified necessary this session on a real low-res phone photo), and
compositing onto a solid background with a soft drop shadow so the
product reads as sitting on a surface rather than floating.
"""
from PIL import Image, ImageDraw, ImageFilter
import numpy as np

from .config import PipelineConfig


def crop_to_mask(rgba: Image.Image, mask: np.ndarray) -> Image.Image:
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    top, bottom = np.where(rows)[0][[0, -1]]
    left, right = np.where(cols)[0][[0, -1]]
    return rgba.crop((int(left), int(top), int(right) + 1, int(bottom) + 1))


def _add_drop_shadow(canvas: Image.Image, product_box: tuple) -> Image.Image:
    left, top, right, bottom = product_box
    width = right - left
    height = bottom - top

    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow_layer)
    shadow_w = int(width * 0.75)
    shadow_h = max(8, int(height * 0.06))
    cx = (left + right) // 2
    shadow_top = bottom - shadow_h // 2
    draw.ellipse(
        [cx - shadow_w // 2, shadow_top, cx + shadow_w // 2, shadow_top + shadow_h],
        fill=(0, 0, 0, 90),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(6, shadow_h // 2)))
    return Image.alpha_composite(canvas, shadow_layer)


def composite(cropped_rgba: Image.Image, config: PipelineConfig) -> tuple[Image.Image, bool, tuple]:
    """Scales `cropped_rgba` to fill `config.target_occupancy` of a
    `config.canvas_size` square (never upscaling past `config.max_upscale`),
    centers it on a `config.background_color` canvas with a drop shadow.
    Returns (RGB canvas image, low_resolution flag, placement_box) where
    placement_box = (left, top, right, bottom) of the product on the
    canvas — validator.py uses this to check centering/occupancy without
    a second, wasteful segmentation pass on the finished image.
    """
    target_dim = int(config.canvas_size * config.target_occupancy)
    ideal_scale = target_dim / max(cropped_rgba.width, cropped_rgba.height)
    scale = min(ideal_scale, config.max_upscale)
    low_resolution = ideal_scale > config.max_upscale

    new_w = max(1, int(cropped_rgba.width * scale))
    new_h = max(1, int(cropped_rgba.height * scale))
    resample = Image.LANCZOS if scale < 1 else Image.BICUBIC
    resized = cropped_rgba.resize((new_w, new_h), resample)

    canvas = Image.new("RGBA", (config.canvas_size, config.canvas_size), config.background_color + (255,))
    paste_x = (config.canvas_size - new_w) // 2
    paste_y = (config.canvas_size - new_h) // 2
    canvas = _add_drop_shadow(canvas, (paste_x, paste_y, paste_x + new_w, paste_y + new_h))
    canvas.paste(resized, (paste_x, paste_y), resized)

    placement_box = (paste_x, paste_y, paste_x + new_w, paste_y + new_h)
    return canvas.convert("RGB"), low_resolution, placement_box
