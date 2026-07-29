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
    sharpening_strength: float = 0.3  # 0=off, 1=maximum
    denoise: bool = True

    # Auto-detected at runtime by hardware.py; set False to force CPU
    # even if a CUDA-capable onnxruntime provider is present.
    use_gpu: bool = True
