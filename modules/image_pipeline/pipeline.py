"""Orchestrates all pipeline stages in order. This is process_image()'s
implementation — modules/image_pipeline/__init__.py re-exports it as the
package's single public entry point.
"""
import io

from PIL import Image, ImageOps

from . import background, detector, diagnostics, enhancer, perspective, reflections, validator
from .config import PipelineConfig
from .models import DiagnosticReport, EnhancedImage, LowQualityImageError, QualityScore


def process_image(
    input_image: bytes,
    config: PipelineConfig = None,
) -> tuple:
    """Runs the full enhancement pipeline on one photo's raw bytes.

    Returns (EnhancedImage, QualityScore, DiagnosticReport).

    Raises:
        ValueError: no foreground product could be detected at all.
        LowQualityImageError: a product was detected, but the resulting
            QualityScore.overall is below config.quality_threshold — the
            caller should show `error.report.issues` and prompt a retake,
            same pattern as the ValueError case above.
    """
    config = config or PipelineConfig()

    # Step 1 — import: correct EXIF orientation before anything else sees
    # the pixels (phone camera photos carry orientation as metadata, not
    # physically rotated pixels).
    image = Image.open(io.BytesIO(input_image))
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")

    # Step 2 — detection.
    rgba, mask, confidence = detector.detect(image, use_gpu=config.use_gpu)
    if not mask.any():
        raise ValueError("No foreground product detected in this photo.")

    # Step 4 — perspective correction (in-plane rotation only; see
    # perspective.py for why full 3D correction isn't attempted).
    rgba, mask, _ = perspective.correct_perspective(rgba, mask)

    # Step 3 — background removal outcome: crop to just the product.
    cropped = background.crop_to_mask(rgba, mask)

    # Step 6 — lighting/tone enhancement.
    cropped = enhancer.enhance(cropped, config)

    # Step 7 — reflection reduction (classical hot-spot inpainting only;
    # see reflections.py's module docstring for the honest limitation).
    cropped = reflections.reduce_reflections(cropped, config.reflection_reduction_strength)

    # Step 5 + 9 — centered scaling onto the final canvas.
    canvas_rgb, low_resolution, placement_box = background.composite(cropped, config)

    # Step 8 — quality validation.
    score = validator.validate(cropped, placement_box, config.canvas_size, config.target_occupancy)
    report = diagnostics.build_report(score, config.quality_threshold, confidence, low_resolution)

    if not report.passed:
        raise LowQualityImageError(report)

    out = io.BytesIO()
    canvas_rgb.save(out, format="JPEG", quality=config.jpeg_quality)

    enhanced = EnhancedImage(
        jpeg_bytes=out.getvalue(),
        width=config.canvas_size,
        height=config.canvas_size,
        low_resolution=low_resolution,
    )
    return enhanced, score, report
