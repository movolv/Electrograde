"""Thin wrapper around the Anthropic Claude API used for:
- structuring messy web-search text into specs / box-contents
- vision-based condition grading
- listing description generation

Requires ANTHROPIC_API_KEY in the environment (see .env.example).
"""
import base64
import json
import os
import re
from typing import List, Optional

import anthropic

TEXT_MODEL = os.environ.get("ELECTROGRADER_TEXT_MODEL", "claude-sonnet-4-5")
VISION_MODEL = os.environ.get("ELECTROGRADER_VISION_MODEL", "claude-sonnet-4-5")
# For narrow, bounded extraction tasks (read this text, pull out a short
# structured list — no open-ended writing/reasoning) — e.g.
# modules/pricing.py's price extraction. A Haiku-tier model is measurably
# faster for exactly this shape of call and this task doesn't need Sonnet's
# extra reasoning depth; call sites opt into it explicitly via
# ask_json(..., model=FAST_MODEL) rather than this being the new default,
# since most ask_json() callers (description generation, EAN/ASIN
# identification) DO benefit from the stronger model.
FAST_MODEL = os.environ.get("ELECTROGRADER_FAST_MODEL", "claude-haiku-4-5")


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your environment or a .env file."
        )
    # max_retries=5 (SDK default is 2): under real multi-tenant load, many
    # companies generating descriptions/grading at once makes a transient
    # 429 (rate limit) or 529 (overloaded) far more likely to happen at
    # least once in a given call — the SDK already retries these with
    # exponential backoff internally, this just gives it more attempts
    # before giving up and raising to the caller.
    #
    # timeout=60 (SDK default is 600s/10min PER ATTEMPT): observed directly
    # — a manifest import's background identifier-lookup loop stuck for
    # 10+ minutes on a single ask_json() call with the process at 0% CPU
    # (blocked on network I/O, not looping), stalling the whole batch at
    # "Processing" forever with no exception ever raised (5 retries x up to
    # 10min default = up to ~50min worst case). Every call site that uses
    # this client already handles a raised exception as a normal "nothing
    # found" outcome (see identifier_lookup.find_identifiers' try/except
    # around ask_json), so failing fast here is strictly safer than hanging.
    return anthropic.Anthropic(api_key=api_key, max_retries=5, timeout=60.0)


def _extract_json(text: str) -> dict:
    """Pull the first {...} JSON object out of a model response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model output: {text[:200]}")
    return json.loads(match.group(0))


def ask_json(system: str, user: str, max_tokens: int = 1500, model: Optional[str] = None) -> dict:
    client = _client()
    resp = client.messages.create(
        model=model or TEXT_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(text)


def ask_text(system: str, user: str, max_tokens: int = 800) -> str:
    client = _client()
    resp = client.messages.create(
        model=TEXT_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if block.type == "text").strip()


def _detect_media_type(image_bytes: bytes) -> str:
    """Sniffs the real format from the file's magic bytes rather than
    trusting a filename/caller-supplied default — camera_input always
    produces JPEG, but st.file_uploader also accepts PNG from a user's
    photo library, and Claude's API rejects a mismatched media_type/content
    combination outright rather than transcoding for you."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_block(image_bytes: bytes, media_type: Optional[str] = None) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type or _detect_media_type(image_bytes),
            "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
        },
    }


def ask_vision_json(
    system: str,
    user_text: str,
    images: List[bytes],
    max_tokens: int = 2000,
) -> dict:
    client = _client()
    content = [_image_block(img) for img in images]
    content.append({"type": "text", "text": user_text})
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(text)
