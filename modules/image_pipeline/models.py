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
    overall: int  # 0-100, weighted combination of the four below
    sharpness: int
    exposure: int
    centering: int
    occupancy: int


@dataclass
class DiagnosticReport:
    passed: bool
    issues: List[str] = field(default_factory=list)  # reasons that would fail the quality gate
    warnings: List[str] = field(default_factory=list)  # non-blocking notes (e.g. "low_resolution")


class LowQualityImageError(RuntimeError):
    """Raised by pipeline.process_image() when QualityScore.overall falls
    below config.quality_threshold. Carries the DiagnosticReport so the
    caller (app.py) can show the user specifically what to fix, the same
    way today's "no foreground subject" ValueError is already handled."""

    def __init__(self, report: DiagnosticReport):
        self.report = report
        super().__init__("; ".join(report.issues) or "Image quality below threshold.")
