"""Orchestrates all pipeline stages in order. This is process_image()'s
implementation — modules/image_pipeline/__init__.py re-exports it as the
package's single public entry point.
"""
import io
import time
from dataclasses import replace

import numpy as np
from PIL import Image, ImageOps

from . import background, detector, diagnostics, enhancer, exposure, material, perspective, reflections, safety, validator
from .config import PipelineConfig
from .models import DiagnosticReport, EnhancedImage, LowQualityImageError, ProcessingLog, QualityScore


def _weakest_of(score: QualityScore, keys: tuple) -> str:
    values = {k: getattr(score, k) for k in keys}
    return min(values, key=values.get)


def _targeted_reoptimize_pass(enhanced_cropped: Image.Image, mask: np.ndarray, weakest: str, config: PipelineConfig) -> Image.Image:
    """One bounded, targeted corrective pass applied on top of the
    already-enhanced image — not a full re-run of enhance() (which would
    compound every stage a second time). Only touches whichever
    sub-score came out weakest, and only ever runs once per photo (see
    process_image's max_reoptimize_passes gate)."""
    rgb = enhanced_cropped.convert("RGB")
    if weakest == "exposure":
        analysis = exposure.analyze(np.array(rgb), mask)
        # A small, fixed extra nudge in whichever direction the first
        # pass's own adaptive gamma (already applied inside enhance())
        # didn't fully correct — deliberately smaller than the primary
        # correction's own cap, since this is a second, bounded pass.
        extra_gamma = 1.12 if analysis["mean_luma"] < 127 else 1 / 1.12
        rgb = exposure.apply_gamma(rgb, mask, extra_gamma)
    else:
        rgb = enhancer._sharpen(rgb, min(1.0, config.sharpening_strength + 0.25))
    return Image.merge("RGBA", (*rgb.split(), enhanced_cropped.getchannel("A")))


def _safer_config(config: PipelineConfig) -> PipelineConfig:
    """A reduced-strength variant used exactly once if the safety check
    (safety.py) finds enhancement has measurably eroded local detail —
    backs off the stages most likely responsible (sharpen, clarity —
    clarity's strength is itself derived from sharpening_strength inside
    enhancer.enhance — and defringe) rather than reverting to the
    untouched original outright."""
    return replace(
        config,
        sharpening_strength=config.sharpening_strength * 0.5,
        defringe_strength=config.defringe_strength * 0.5,
    )


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
    start_time = time.perf_counter()
    log = ProcessingLog()

    # Step 1 — import: correct EXIF orientation before anything else sees
    # the pixels (phone camera photos carry orientation as metadata, not
    # physically rotated pixels).
    image = Image.open(io.BytesIO(input_image))
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")

    # Step 2 — detection.
    rgba, mask, confidence, segmentation = detector.detect(image, use_gpu=config.use_gpu, config=config)
    if not mask.any():
        raise ValueError("No foreground product detected in this photo.")
    log.separation = segmentation["separation"]
    log.low_contrast_segmentation = segmentation["low_contrast"]
    log.mask_threshold_used = segmentation["mask_threshold"]
    log.components_dropped = segmentation["components_dropped"]
    log.holes_filled = segmentation["holes_filled"]

    # Step 4 — perspective correction (in-plane rotation only; see
    # perspective.py for why full 3D correction isn't attempted). The mask
    # threshold is passed through so the post-rotation mask is rebuilt with
    # the SAME definition of "product pixel" detection just used — on a
    # low-contrast photo detection lowers that threshold, and re-deriving
    # the mask at a stricter one here would quietly undo part of the repair.
    rgba, mask, _ = perspective.correct_perspective(
        rgba, mask, mask_threshold=segmentation["mask_threshold"]
    )

    # Step 3 — background removal outcome: crop to just the product.
    cropped = background.crop_to_mask(rgba, mask)
    product_mask = np.array(cropped.getchannel("A")) > 10
    original_cropped_rgb = cropped.convert("RGB")

    if config.material_aware:
        profile = material.analyze(np.array(original_cropped_rgb), product_mask)
    else:
        profile = {"profile": material.PROFILE_NEUTRAL, "specular_coverage": 0.0, "mean_luma": 0.0}
    log.material_profile = profile["profile"]
    log.specular_coverage = profile["specular_coverage"]

    if config.adaptive_exposure:
        exposure_stats = exposure.analyze(np.array(original_cropped_rgb), product_mask)
        log.exposure_gamma = exposure.compute_gamma(exposure_stats)
        log.exposure_compressed = exposure_stats["compressed"]

    # Step 6 — lighting/tone enhancement (white balance, adaptive
    # exposure, local contrast, vibrance, clarity, adaptive
    # denoise/sharpen — see enhancer.py for the material-aware presets).
    cropped = enhancer.enhance(cropped, config)

    # Step 7 — reflection reduction (classical hot-spot inpainting only;
    # see reflections.py's module docstring for the honest limitation).
    cropped = reflections.reduce_reflections(cropped, config.reflection_reduction_strength)

    # Safety check: has enhancement measurably eroded local detail (a
    # proxy for "a scratch/label/serial number might now be less
    # visible" — see safety.py, directly serving the project's rule that
    # this pipeline must never alter what a product actually looks
    # like)? If so, back off strength once and keep whichever version
    # preserved more detail.
    similarity = safety.measure_similarity(original_cropped_rgb, cropped.convert("RGB"), product_mask)
    if similarity < config.min_detail_similarity:
        safer = _safer_config(config)
        safer_cropped = enhancer.enhance(background.crop_to_mask(rgba, mask), safer)
        safer_cropped = reflections.reduce_reflections(safer_cropped, safer.reflection_reduction_strength)
        safer_similarity = safety.measure_similarity(original_cropped_rgb, safer_cropped.convert("RGB"), product_mask)
        if safer_similarity > similarity:
            cropped = safer_cropped
            similarity = safer_similarity
        log.notes.append(f"safety backoff applied (similarity {similarity:.3f} vs floor {config.min_detail_similarity})")
    log.detail_similarity = similarity

    # Step 5 + 9 — centered scaling onto the final canvas.
    canvas_rgb, low_resolution, placement_box = background.composite(cropped, config)

    # Step 8 — quality validation.
    score = validator.validate(
        cropped, placement_box, config.canvas_size, config.target_occupancy,
        canvas_rgb=canvas_rgb, background_color=config.background_color,
    )

    # Bounded re-optimization: if the score is borderline (below
    # threshold but above a recoverable floor), try exactly one targeted
    # corrective pass and keep whichever result scores higher — never
    # more than config.max_reoptimize_passes, both for the performance
    # budget and to avoid compounding over-correction into an unnatural
    # look (the exact runaway-adaptive-correction risk this session's
    # black-robot bug was one instance of).
    if (
        config.max_reoptimize_passes > 0
        and config.recoverable_score_floor <= score.overall < config.quality_threshold
    ):
        log.score_before_reoptimize = score.overall
        weakest = _weakest_of(score, ("sharpness", "exposure"))
        retried_cropped = _targeted_reoptimize_pass(cropped, product_mask, weakest, config)
        retried_canvas, retried_low_res, retried_box = background.composite(retried_cropped, config)
        retried_score = validator.validate(
            retried_cropped, retried_box, config.canvas_size, config.target_occupancy,
            canvas_rgb=retried_canvas, background_color=config.background_color,
        )
        if retried_score.overall > score.overall:
            cropped, canvas_rgb, low_resolution, placement_box, score = (
                retried_cropped, retried_canvas, retried_low_res, retried_box, retried_score
            )
            log.reoptimized = True

    log.processing_time_seconds = time.perf_counter() - start_time
    report = diagnostics.build_report(score, config.quality_threshold, confidence, low_resolution, log=log)

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
