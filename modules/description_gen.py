"""Generate the two mandatory English listing description columns:
1. Product Description        - general overview, strictly English
2. Condition & Scratches Details - defects/missing-parts only, strictly English
"""
from dataclasses import dataclass

from modules import ai_client

SYSTEM_PROMPT = (
    "You are a professional e-commerce copywriter for a used-electronics "
    "reseller. Write STRICTLY in English regardless of any non-English "
    "product names or source text. Respond with STRICT JSON only, no "
    "markdown fences, matching exactly this schema:\n"
    '{"product_description": str, "condition_description": str}\n\n'
    "product_description: a general, appealing overview of the product "
    "(what it is, brand, model, key specs, what's included in the box). "
    "Do NOT mention scratches/defects here.\n\n"
    "condition_description: a separate, precise, honest description "
    "covering ONLY the physical condition — visible scratches, dents, wear, "
    "and any missing components/accessories. If there are no defects, state "
    "clearly that the item is in excellent cosmetic condition with no "
    "visible flaws. Be factual, not salesy, in this section."
)


@dataclass
class Descriptions:
    product_description: str
    condition_description: str


def generate_descriptions(
    name: str,
    brand: str,
    category: str,
    spec_summary: str,
    box_contents: list,
    grade: str,
    defects: list,
    missing_components: list,
) -> Descriptions:
    user = (
        f"Product name: {name}\n"
        f"Brand: {brand}\n"
        f"Category: {category}\n"
        f"Spec summary: {spec_summary}\n"
        f"Standard box contents: {', '.join(box_contents) or 'unknown'}\n"
        f"Condition grade: {grade}\n"
        f"Detected defects: {', '.join(defects) or 'none detected'}\n"
        f"Missing components: {', '.join(missing_components) or 'none'}\n"
    )
    data = ai_client.ask_json(SYSTEM_PROMPT, user)
    return Descriptions(
        product_description=data.get("product_description", ""),
        condition_description=data.get("condition_description", ""),
    )
