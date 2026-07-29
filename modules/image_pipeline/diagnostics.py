"""Builds a human-readable DiagnosticReport from a QualityScore — kept
separate from validator.py's scoring math so "what counts as a problem"
can be tuned independently of "how the numbers are computed."
"""
from .models import DiagnosticReport, QualityScore

_SUBSCORE_THRESHOLD = 40  # below this, a specific sub-score gets called out


def build_report(score: QualityScore, threshold: int, detector_confidence: int, low_resolution: bool) -> DiagnosticReport:
    issues: list = []
    warnings: list = []

    if score.sharpness < _SUBSCORE_THRESHOLD:
        issues.append("Image appears blurry or out of focus.")
    if score.exposure < _SUBSCORE_THRESHOLD:
        issues.append("Image is over- or under-exposed.")
    if score.centering < _SUBSCORE_THRESHOLD:
        warnings.append("Product is not well centered in the frame.")
    if score.occupancy < _SUBSCORE_THRESHOLD:
        warnings.append("Product is small relative to the frame.")
    if detector_confidence < _SUBSCORE_THRESHOLD:
        warnings.append("Background/product separation is uncertain — check the result closely.")
    if low_resolution:
        warnings.append("Source photo resolution was too low to fill the frame without blurring; retake closer up for a larger result.")

    passed = score.overall >= threshold
    if not passed and not issues:
        issues.append(f"Overall quality score {score.overall} is below the required {threshold}.")

    return DiagnosticReport(passed=passed, issues=issues, warnings=warnings)
