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

    # -- Segmentation (detector.py) --
    #
    # rembg's own mask post-processing (morphology + smoothing on its raw
    # output). Measured on real failing photos in this repo: it is the
    # single biggest win available here and costs ~0.1s. On a glossy chrome
    # product (uploads/781-B4) it took the pipeline's own edge-quality
    # score from 0 to 75 and on a dark product with a thin pole
    # (uploads/1006-J2) from 48 to 68, with no measurable downside on
    # high-contrast controls (uploads/1064-A3: 89 -> 94).
    post_process_mask: bool = True
    #
    # rembg's alpha matting. DELIBERATELY OFF despite being the obvious
    # candidate for "soft edges on low-contrast products": measured on the
    # same real photos it made results WORSE, not better — it shattered
    # masks into interior holes (365 holes on 781-B4, 145 on 1006-J2, 64 on
    # a high-contrast control that previously had none), raised boundary
    # roughness (12.8 -> 17.1 on 781-B4), and cost ~3x the runtime
    # (0.8s -> 3.8s) while emitting pymatting convergence warnings. The
    # flag is kept so this is one edit to re-test on a future rembg/
    # pymatting version, not so it can be switched on blind.
    enable_alpha_matting: bool = False
    alpha_matting_foreground_threshold: int = 240
    alpha_matting_background_threshold: int = 10
    alpha_matting_erode_size: int = 10
    #
    # Alpha level at/below which a pixel counts as background. Verified on
    # the real failing photos that lowering this recovers almost nothing on
    # its own (~100% of the wrongly-dropped pixels sit at exactly alpha 0,
    # i.e. rembg is confidently wrong rather than uncertain) — it is here
    # for the low-contrast path and for tuning, not as the primary fix.
    mask_threshold: int = 10
    low_contrast_mask_threshold: int = 5
    #
    # Morphological closing applied to the mask before connectivity
    # analysis, to bridge a boundary that segmentation broke (the classic
    # "thin handle got disconnected, then discarded" failure). Odd px.
    mask_closing_kernel: int = 7
    #
    # Connected components at least this fraction of the largest one are
    # kept as real product parts rather than discarded as noise; smaller
    # ones are still kept when they sit within `component_proximity_px` of
    # the main body (a detached handle/lid/foot), so a product is never
    # silently reduced to whichever single blob happened to be biggest.
    secondary_component_ratio: float = 0.05
    component_proximity_px: int = 24
    #
    # Interior holes smaller than this fraction of the product's area are
    # filled — segmentation speckle inside white/mirrored surfaces. Larger
    # holes are left alone because they are genuine see-through openings.
    # Threshold chosen from measured data, not guessed: on uploads/781-B4
    # the two genuine openings measure 7.31% and 3.57% of product area
    # while every speckle hole measured 0.20% or less, so 1% sits in a wide
    # empty gap between the two populations.
    max_hole_fraction: float = 0.01
    #
    # Soft-feathers the mask edge over this many pixels (anti-aliasing, not
    # a visible blur ring — verified that feathering harder reads as
    # "blurry edges"). Deliberately NOT raised on the low-contrast path:
    # tried that first and measured it backwards — a wider feather adds
    # semi-transparent pixels, which is precisely the ragged-halo symptom
    # being fixed here (edge quality 64 -> 40 on uploads/577-D3, 84 -> 72
    # on uploads/781-B4). One feather value for every path.
    edge_feather_px: float = 0.8
    #
    # Adaptive safety net: when the product separates from its background
    # by less than this median luma difference (white product on a white
    # backdrop, black on black), segmentation keeps MORE of what it found
    # — lower alpha threshold, larger closing kernel, smaller size bar for
    # a component to count as a real part. Those are pure safety nets:
    # measured on real photos they change nothing when segmentation was
    # already clean (identical mask area on all four test photos), and
    # only matter when a part would otherwise have been dropped.
    #
    # 120 comes from the measured separation of real photos in this repo:
    # white steam mops 52 and 57, glossy chrome 67, glossy pouch 108, vs.
    # dark products on white at 226-254. It sits in the empty gap between
    # those two populations, so light/glossy products get the safety net
    # and normal-contrast photos keep today's behaviour exactly.
    adaptive_segmentation: bool = True
    low_contrast_separation: float = 120.0

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
