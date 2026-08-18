"""Return types for process_image() — kept separate from modules/models.py's
Product dataclass since these describe a single image-processing result,
not a catalog record.
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class EnhancedImage:
    jpeg_bytes: bytes
    width: int
    height: int
    low_resolution: bool = False  # True if source detail forced staying under target_occupancy


@dataclass
class QualityScore:
    overall: int  # 0-100, weighted combination of the sub-scores below
    sharpness: int
    exposure: int
    centering: int
    occupancy: int
    # Added for the adaptive-engine quality evaluation. All are classical-
    # CV proxies, same honesty caveat as the original four: there is no
    # ML quality model running locally, so each measures the closest
    # thing actually computable, not the literal named concept.
    noise: int = 100  # inverse of measured sensor/compression noise
    color_cast: int = 100  # proxy for "color accuracy": absence of a residual uncorrected tint
    background_quality: int = 100  # how close the finished canvas background is to the target color
    edge_quality: int = 100  # mask-boundary cleanliness (thin antialiasing vs. wide/ragged halo)


@dataclass
class ProcessingLog:
    """Structured record of what the adaptive engine actually decided for
    one photo — not shown to end users, but useful for debugging a
    specific bad result (e.g. "why did this one come out dark?") without
    re-running the pipeline with print statements added back in."""
    material_profile: str = "neutral"
    specular_coverage: float = 0.0
    # Segmentation (detector.py): what the adaptive path decided and what
    # it had to repair. `separation` is the measured product-vs-background
    # tonal distance the low_contrast decision came from, so a bad cutout
    # can be diagnosed without re-running segmentation by hand.
    separation: float = 0.0
    low_contrast_segmentation: bool = False
    mask_threshold_used: int = 0
    components_dropped: int = 0
    holes_filled: int = 0
    exposure_gamma: float = 1.0
    exposure_compressed: bool = False
    denoise_h: float = 0.0
    sharpen_strength: float = 0.0
    defringe_strength: float = 0.0
    detail_similarity: float = 1.0
    score_before_reoptimize: int = 0
    reoptimized: bool = False
    processing_time_seconds: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class DiagnosticReport:
    passed: bool
    issues: List[str] = field(default_factory=list)  # reasons that would fail the quality gate
    warnings: List[str] = field(default_factory=list)  # non-blocking notes (e.g. "low_resolution")
    log: ProcessingLog = None  # populated by pipeline.py; None only if never wired up by a caller


class LowQualityImageError(RuntimeError):
    """Raised by pipeline.process_image() when QualityScore.overall falls
    below config.quality_threshold. Carries the DiagnosticReport so the
    caller (app.py) can show the user specifically what to fix, the same
    way today's "no foreground subject" ValueError is already handled."""

    def __init__(self, report: DiagnosticReport):
        self.report = report
        super().__init__("; ".join(report.issues) or "Image quality below threshold.")
