"""Generate marketplace-ready listing copy for a graded item:

1. product_name          - clean "[Product type] Brand (Model)" listing title
2. product_description   - general overview
3. condition_description - honest, short "Additional description" covering
                            only the physical condition (the "Condition &
                            Scratches Details" / Baselinker description_extra1
                            field)

Defaults to English; pass language="German" (etc.) to generate copy in
another language for a specific marketplace/channel — see LANGUAGES below.

This module only consumes already-decided grading output (grade, defects,
missing_components as plain strings) — it never re-derives or influences the
A/B/C/D grade itself. See modules/vision_grading.py for that.
"""
import re
from dataclasses import dataclass

from modules import ai_client

DEFAULT_LANGUAGE = "English"

# Per-language required opening sentence for condition_description. Kept
# short and free of near-synonym pairs (e.g. avoid "tested and checked" —
# machine translation of that into some languages collapses both words to
# the same term, producing an awkward repeated word).
_REQUIRED_OPENINGS = {
    "English": "Fully tested, in good working condition.",
    "German": "Vollständig getestet, in gutem funktionsfähigem Zustand.",
}

# Past wording(s) of the required opening — kept so a stray/cached
# occurrence never gets duplicated instead of replaced by the current one.
_LEGACY_OPENINGS = {
    "English": ("Tested and checked, in good working condition.",),
    "German": (),
}

# Visual noise that is never a real defect and must never appear in a
# generated condition description (dust, cleaning residue, transport
# packaging, stickers, etc.) — see task spec section 4.
_IGNORED_KEYWORDS = {
    "English": (
        "dust",
        "dirt particle",
        "fingerprint",
        "water drop",
        "transport tape",
        "packing tape",
        "sticker",
        "packaging",
        "cardboard",
        "coffee residue",
        "coffee stain",
        "food residue",
        "food stain",
    ),
    "German": (
        "staub",
        "schmutzpartikel",
        "fingerabdruck",
        "wassertropfen",
        "transportklebeband",
        "klebeband",
        "aufkleber",
        "etikett",
        "verpackung",
        "karton",
        "kaffeerückstände",
        "kaffeerest",
        "kaffeeflecken",
        "essensreste",
        "lebensmittelreste",
        "speisereste",
    ),
}

# Phrases the copy must never contain — every item processed here has
# already been tested and checked, so this claim is never accurate.
_FORBIDDEN_PHRASES = {
    "English": (
        "functional test not performed",
        "not functionally tested",
    ),
    "German": (
        "funktionsprüfung wurde nicht durchgeführt",
        "keine funktionsprüfung durchgeführt",
        "nicht funktionsgeprüft",
    ),
}

MAX_CONDITION_DESCRIPTION_LEN = 400


def _build_system_prompt(language: str) -> str:
    opening = _REQUIRED_OPENINGS[language]
    return (
        "You are a professional listing copywriter for a used-electronics "
        "reseller, writing for eBay, Amazon, and Back Market. Write STRICTLY "
        f"in {language} regardless of any other-language source text. Think "
        "like an honest professional used-goods seller: build buyer "
        "confidence, never invent facts, never over-criticize minor "
        "cosmetic wear, but always disclose real damage plainly. Respond "
        "with STRICT JSON only, no markdown fences, matching exactly this "
        "schema:\n"
        '{"product_type": str, "detected_brand": str, "detected_model": str, '
        '"product_description": str, "condition_description": str}\n\n'
        "product_type: a short, clean, marketplace-style noun phrase for "
        'what the item is (e.g. "Microwave Oven", "Robot Vacuum Cleaner", '
        '"Coffee Machine"). No brand, no model, no marketing words.\n\n'
        "detected_brand / detected_model: the caller gives you a 'Brand' "
        "and 'Model' field below — if either says 'unknown', try to extract "
        "it from the 'Draft/working product name' text (an existing, "
        "already-real product title, e.g. \"Bosch Serie 2 BGC05AAA1\" → "
        'brand "Bosch", model "Serie 2 BGC05AAA1"). This is reading '
        "information already present in that title, never inventing new "
        "information — if the title doesn't clearly contain a brand/model, "
        'leave the corresponding field as an empty string "" rather than '
        "guessing. If the caller's Brand/Model were already given (not "
        "'unknown'), just echo them back unchanged.\n\n"
        "product_description: a full, appealing marketing overview of the "
        "product — several sentences (roughly 500-900 characters), reading "
        "like a real marketplace listing, not a one-line summary. Cover: an "
        "engaging opening about what the product is and who it's for, its "
        "key features/specs, and what's included in the box — using ONLY "
        "the specs/box-contents facts given to you — never invent a "
        "specification, capacity, or feature that wasn't provided; if only "
        "a little spec information is available, write more about the "
        "product's general purpose/use cases instead of padding with "
        "invented facts. Do NOT mention scratches, defects, or condition "
        "here.\n\n"
        "condition_description: a short, honest condition summary. It MUST "
        f'start with exactly this sentence: "{opening}" — every item has '
        "already been tested and checked, so never write anything implying "
        "otherwise. After that opening sentence, add at most one or two "
        "short sentences about condition, following ALL of these rules:\n"
        "  - The absolute total length of condition_description must not "
        f"exceed {MAX_CONDITION_DESCRIPTION_LEN} characters.\n"
        "  - NEVER mention: dust, small dirt particles, fingerprints, water "
        "drops from cleaning, transport/packing tape, stickers, packaging, "
        "cardboard box condition, or cleanable consumable residue (coffee "
        "residue/stains, food residue/stains) — none of these are product "
        "defects, they are cleanable and irrelevant to condition.\n"
        "  - NEVER list individual scratches one by one. Summarize with "
        'general professional wording, e.g. "Visible signs of use and '
        'scratches on the housing, which do not affect operation."\n'
        "  - Separate COSMETIC wear (small scratches, normal usage marks, "
        "color fading, surface wear) from REAL DAMAGE (deep scratches, "
        "cracks, broken or missing parts, dents, deformation).\n"
        '  - For cosmetic-only wear, write something like "Cosmetic signs '
        'of use, operation not affected." Do not exaggerate minor cosmetic '
        'issues into words like "damaged".\n'
        "  - For real damage, name it plainly and specifically (e.g. "
        '"Visible dent on the metal housing corner.", "Handle has a '
        'crack.", "Plastic part is broken."), and state whether it affects '
        "functionality only if that is reasonably inferable — never claim "
        "something works if the defect suggests otherwise.\n"
        "  - Pay attention to and always mention dents/deformation on "
        "corners or metal panels/housings when present — these are never "
        "cosmetic-only and must not be omitted.\n"
        '  - If there are no notable defects, simply state something like '
        '"No visible cosmetic defects." after the opening sentence.\n'
        "  - Be factual and honest, not salesy, but avoid unnecessary "
        "negative wording or microscopic defect descriptions — focus only "
        "on important, visible defects a buyer should know about."
    )


@dataclass
class Descriptions:
    product_name: str
    product_description: str
    condition_description: str


def _strip_ignored_sentences(text: str, language: str) -> str:
    """Safety net: drop only the clause(s) that slipped past the prompt and
    still mention dust/stickers/packaging/etc. — these are never real
    defects. Operates at clause (comma-split) granularity within each
    sentence so an unrelated real defect mentioned in the same sentence
    (e.g. "dust on the housing and a crack on the corner") is preserved."""
    keywords = _IGNORED_KEYWORDS.get(language, _IGNORED_KEYWORDS[DEFAULT_LANGUAGE])
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept_sentences = []
    for sentence in sentences:
        if not sentence:
            continue
        trailing = sentence[-1] if sentence[-1] in ".!?" else ""
        body = sentence[:-1] if trailing else sentence
        clauses = re.split(r",\s*", body)
        kept_clauses = [
            c for c in clauses
            if c.strip() and not any(kw in c.lower() for kw in keywords)
        ]
        if kept_clauses:
            kept_sentences.append(", ".join(kept_clauses) + trailing)
    return " ".join(kept_sentences).strip()


def _clip(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    clipped = text[:max_len]
    last_space = clipped.rfind(" ")
    if last_space > 0:
        clipped = clipped[:last_space]
    return clipped.rstrip(" ,;:-")


def _finalize_condition_description(raw: str, language: str = DEFAULT_LANGUAGE) -> str:
    opening = _REQUIRED_OPENINGS.get(language, _REQUIRED_OPENINGS[DEFAULT_LANGUAGE])
    legacy_openings = _LEGACY_OPENINGS.get(language, ())
    forbidden_phrases = _FORBIDDEN_PHRASES.get(language, _FORBIDDEN_PHRASES[DEFAULT_LANGUAGE])

    text = (raw or "").strip()
    for phrase in forbidden_phrases:
        text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE).strip()

    # Strip a leading legacy/duplicate opening sentence (current or past
    # wording) before re-prepending the canonical one, so an old phrase
    # slipping out of the model (or old cached data) never doubles up.
    for candidate in (opening, *legacy_openings):
        if text.lower().startswith(candidate.lower()):
            text = text[len(candidate):].strip()
            break

    rest = _strip_ignored_sentences(text, language)
    if rest:
        rest = rest[0].upper() + rest[1:]
        text = f"{opening} {rest}"
    else:
        text = opening

    return _clip(text, MAX_CONDITION_DESCRIPTION_LEN)


def _build_product_name(product_type: str, brand: str, model: str) -> str:
    name = " ".join(p.strip() for p in (product_type, brand) if p and p.strip())
    if model and model.strip():
        name = f"{name} ({model.strip()})" if name else f"({model.strip()})"
    return name.strip()


def generate_descriptions(
    name: str,
    brand: str,
    model: str,
    category: str,
    spec_summary: str,
    box_contents: list,
    grade: str,
    condition_type: str,
    defects: list,
    missing_components: list,
    language: str = DEFAULT_LANGUAGE,
) -> Descriptions:
    if language not in _REQUIRED_OPENINGS:
        raise ValueError(
            f"Unsupported language {language!r}; supported: "
            f"{sorted(_REQUIRED_OPENINGS)}"
        )

    user = (
        f"Draft/working product name: {name}\n"
        f"Brand: {brand or 'unknown'}\n"
        f"Model: {model or 'unknown'}\n"
        f"Category: {category}\n"
        f"Spec summary: {spec_summary or 'not available'}\n"
        f"Standard box contents: {', '.join(box_contents) or 'unknown'}\n"
        f"Condition grade: {grade} ({condition_type or 'Used'})\n"
        f"Detected defects: {', '.join(defects) or 'none detected'}\n"
        f"Missing components: {', '.join(missing_components) or 'none'}\n"
    )
    data = ai_client.ask_json(_build_system_prompt(language), user)

    resolved_brand = brand.strip() if brand and brand.strip() else (data.get("detected_brand") or "").strip()
    resolved_model = model.strip() if model and model.strip() else (data.get("detected_model") or "").strip()

    product_name = _build_product_name(
        data.get("product_type", "") or category or "",
        resolved_brand,
        resolved_model,
    )
    condition_description = _finalize_condition_description(
        data.get("condition_description", ""), language
    )

    return Descriptions(
        product_name=product_name,
        product_description=data.get("product_description", ""),
        condition_description=condition_description,
    )
