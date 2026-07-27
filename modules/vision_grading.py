"""Vision AI condition grading + manifest-vs-photo verification.

Sends all captured photos (front/back/sides/defect close-ups) plus the
expected identity (from spec_lookup / manifest) to Claude's vision model and
asks for a structured grading report — AND a verification check on whether
the photographed item actually looks like the claimed identity. A manifest's
EAN/description is never assumed correct; this is the deliberate check
against that assumption.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from modules import ai_client

GRADE_SCALE = {
    "A": "Like new / no visible wear, fully complete",
    "B": "Light cosmetic wear (minor scuffs/scratches), fully functional-looking, complete or near-complete",
    "C": "Noticeable cosmetic wear (visible scratches/dents), possibly missing minor accessories",
    "D": "Heavy wear/damage and/or missing components; sold as-is / for parts",
}

# If a barcode decoded directly from a photo (hard, deterministic signal)
# disagrees with the manifest's claimed EAN, cap AI confidence this low
# regardless of what the vision model itself reports.
BARCODE_MISMATCH_CONFIDENCE_CAP = 20


@dataclass
class GradingResult:
    grade: str = ""
    grade_confidence: int = 0
    grade_reasoning: str = ""
    condition_type: str = ""  # "New" | "Used"
    defects: List[str] = field(default_factory=list)
    missing_components: List[str] = field(default_factory=list)
    functional_checklist: List[str] = field(default_factory=list)
    product_match: str = ""  # "YES" | "NO"
    match_confidence: int = 0
    match_notes: str = ""


SYSTEM_PROMPT = (
    "You are an expert used-electronics quality inspector AND fraud/mismatch "
    "checker for a liquidation reseller. You will be shown several photos of "
    "ONE item (different angles, and possibly close-ups of flaws), plus the "
    "item's CLAIMED identity (which may come from an unverified liquidation "
    "manifest — never assume it is correct).\n\n"
    "You have two jobs:\n\n"
    "1) CONDITION GRADING — analyze physical condition only from what is "
    "visibly shown; do not assume defects that aren't visible.\n\n"
    "2) IDENTITY VERIFICATION — independently judge whether what you actually "
    "see in the photos (brand markings, product type, form factor, any "
    "visible model/EAN label) is CONSISTENT with the claimed identity. Treat "
    "the claimed identity as a hypothesis to test, not a fact.\n\n"
    "Respond with STRICT JSON only, no markdown fences, matching exactly this "
    "schema:\n"
    '{"grade": "A"|"B"|"C"|"D", '
    '"grade_confidence": int (0-100), '
    '"grade_reasoning": str (1-3 sentences explaining the grade), '
    '"condition_type": "New"|"Used", '
    '"defects": [str, ...] (each a specific visible flaw, e.g. '
    '"1cm scratch on top-left corner of lid"), '
    '"missing_components": [str, ...] (only list items you can positively '
    'infer are missing based on the expected box contents given; leave empty '
    'if unsure), '
    '"functional_checklist": [str, ...] (5-8 concise manual test steps a '
    "reseller should perform before listing this exact type of item), "
    '"product_match": "YES"|"NO" (does the photographed item look consistent '
    "with the claimed identity below? \"NO\" if brand/product type/form "
    'factor clearly conflict), '
    '"match_confidence": int (0-100, your confidence in the product_match '
    'verdict), '
    '"match_notes": str (1-2 sentences: what you compared and why — call out '
    "specifically any conflict in brand, model, product type, or a visible "
    'EAN/barcode label)}\n\n'
    "How to set grade_confidence (0-100): based on photo quality (focus, "
    "lighting, resolution), clarity of visible defects, certainty about box "
    "completeness, and how cleanly the item matches ONE grade band vs sitting "
    "ambiguously between two. Never default to a high number out of habit — "
    "insufficient photos must produce a lower score.\n\n"
    "How to set match_confidence (0-100): high only if multiple independent "
    "visual cues (branding, product type, form factor, any visible label) "
    "all agree with the claimed identity. Low if photos don't clearly show "
    "branding/labels, or if anything looks inconsistent.\n\n"
    "All text must be in English."
)


def grade_item(
    images: List[bytes],
    category: str,
    expected_box_contents: List[str],
    expected_identity: Optional[dict] = None,
    manifest_ean: str = "",
    photo_decoded_barcodes: Optional[List[str]] = None,
) -> GradingResult:
    """
    expected_identity: optional dict with any of brand/model/product_name/
        category as currently believed (e.g. from spec_lookup or manifest
        item description) — used as the hypothesis to verify against photos.
    manifest_ean: the manifest's claimed EAN/barcode, if any.
    photo_decoded_barcodes: barcodes pyzbar actually decoded FROM the
        captured photos themselves (a deterministic signal, independent of
        the vision model's judgement) — used to hard-check against
        manifest_ean.
    """
    if not images:
        raise ValueError("At least one photo is required for grading.")

    box_text = ", ".join(expected_box_contents) if expected_box_contents else "unknown"
    identity = expected_identity or {}
    identity_text = (
        f"Claimed brand: {identity.get('brand', 'unknown')}\n"
        f"Claimed model: {identity.get('model', 'unknown')}\n"
        f"Claimed product name: {identity.get('product_name', 'unknown')}\n"
        f"Claimed category: {category or identity.get('category', 'unknown')}\n"
        f"Claimed EAN/barcode: {manifest_ean or 'unknown'}\n"
    )

    user_text = (
        f"{identity_text}\n"
        f"Expected standard box contents: {box_text}\n\n"
        "Photos are attached in order as captured (may include front, back, "
        "sides, and defect close-ups). Grade the item AND verify its identity "
        "per the schema."
    )

    data = ai_client.ask_vision_json(SYSTEM_PROMPT, user_text, images)

    def _clamp_pct(v) -> int:
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, n))

    grade_confidence = _clamp_pct(data.get("grade_confidence", 0))
    match_confidence = _clamp_pct(data.get("match_confidence", 0))
    product_match = str(data.get("product_match", "")).strip().upper()
    match_notes = data.get("match_notes", "")

    # Deterministic override: a barcode actually decoded from a photo beats
    # the vision model's soft visual judgement. Never let AI "assume" the
    # manifest is right when we have hard evidence it's mismatched.
    if manifest_ean and photo_decoded_barcodes:
        manifest_ean_clean = manifest_ean.strip().lstrip("0")
        decoded_clean = {b.strip().lstrip("0") for b in photo_decoded_barcodes if b.strip()}
        if decoded_clean and manifest_ean_clean not in decoded_clean:
            product_match = "NO"
            match_confidence = min(match_confidence, BARCODE_MISMATCH_CONFIDENCE_CAP)
            match_notes = (
                f"Barcode decoded from photo(s) ({', '.join(sorted(decoded_clean))}) "
                f"does not match manifest EAN ({manifest_ean}). {match_notes}"
            ).strip()

    return GradingResult(
        grade=str(data.get("grade", "")).strip()[:1].upper(),
        grade_confidence=grade_confidence,
        grade_reasoning=data.get("grade_reasoning", ""),
        condition_type=str(data.get("condition_type", "")).strip(),
        defects=list(data.get("defects", []) or []),
        missing_components=list(data.get("missing_components", []) or []),
        functional_checklist=list(data.get("functional_checklist", []) or []),
        product_match=product_match,
        match_confidence=match_confidence,
        match_notes=match_notes,
    )
