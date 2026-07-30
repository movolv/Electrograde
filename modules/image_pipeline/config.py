"""All tunable parameters for the image enhancement pipeline in one place —
every stage reads from a PipelineConfig instance rather than hardcoding
constants, so behavior can be adjusted (or A/B tested) without touching
pipeline logic itself.
"""
from dataclasses import dataclass


@dataclass
class PipelineConfig:
    canvas_size: int = 2000
    jpeg_quality: int = 92
    background_color: tuple = (255, 255, 255)
    target_occupancy: float = 0.87

    # Enlarging a low-resolution crop up to fill target_occupancy is what
    # introduces visible blur — no resize algorithm recovers detail that
    # was never captured. This caps how far any stage will upscale.
    max_upscale: float = 1.3

    # Below this overall QualityScore, process_image raises
    # LowQualityImageError instead of returning a result. 90 (as an
    # initial spec target) rejects a lot of ordinary phone photos in
    # practice — start lower and raise once real pass/fail rates from
    # actual usage are visible.
    quality_threshold: int = 70

    reflection_reduction_strength: float = 0.5  # 0=off, 1=maximum
    sharpening_strength: float = 0.5  # 0=off, 1=maximum
    denoise: bool = True

    # Modest color-intensity lift (1.0 = untouched, matching PIL's
    # ImageEnhance.Color scale) — presentation polish in the same spirit
    # as the brightness/contrast lift above, not a hue/color change, so it
    # doesn't cross into "altering what the product looks like."
    saturation_boost: float = 1.15

    # Auto-detected at runtime by hardware.py; set False to force CPU
    # even if a CUDA-capable onnxruntime provider is present.
    use_gpu: bool = True

    # -- Adaptive engine (all values below are *behavior toggles/limits*,
    # not enhancement strengths — every actual correction amount is
    # computed per-image from that image's own histogram/statistics, per
    # the "never use fixed enhancement values" requirement) --
    adaptive_exposure: bool = True  # exposure.py: gamma/highlight/shadow/blacks/whites
    adaptive_sharpen_denoise: bool = True  # size sharpen/denoise strength from measured sharpness/noise
    vibrance_mode: bool = True  # True = per-pixel vibrance curve; False = flat saturation_boost
    defringe_strength: float = 0.5  # 0=off, 1=maximum chromatic-aberration edge defringe
    material_aware: bool = True  # branch correction strategy on the heuristic profile in material.py

    # Safety net (see safety.py): if enhancement measurably destroys
    # local detail/texture vs. the original (a proxy for "a scratch,
    # label, or serial number might now be less visible"), enhancement
    # strength is backed off. This threshold is an SSIM score (0-1); real
    # product photos enhanced by this pipeline scored >0.9 in testing,
    # so this is deliberately a generous floor, not a tight one.
    min_detail_similarity: float = 0.75

    # Re-optimization loop (pipeline.py): if the first pass scores below
    # quality_threshold but above this floor, exactly one more corrective
    # pass is tried and the better-scoring result is kept — never more
    # than max_reoptimize_passes, to bound both processing time and the
    # risk of "enhancing the enhancement" into an over-processed look.
    max_reoptimize_passes: int = 1
    recoverable_score_floor: int = 55
