"""Local AI image enhancement pipeline for the primary e-commerce listing
photo. Generates a professional-looking studio product shot (white
background, centered, enhanced lighting) from a raw product photo while
never altering the actual product — no scratches/dents removed, no parts
invented, no colors/logos changed. Only presentation is improved.

The ONLY supported entry point is process_image() — everything else in
this package is an internal implementation detail and may change without
notice.
"""
from .config import PipelineConfig
from .models import DiagnosticReport, EnhancedImage, LowQualityImageError, QualityScore
from .pipeline import process_image

__all__ = [
    "process_image",
    "PipelineConfig",
    "EnhancedImage",
    "QualityScore",
    "DiagnosticReport",
    "LowQualityImageError",
]
