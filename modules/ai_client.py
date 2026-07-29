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


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your environment or a .env file."
        )
    return anthropic.Anthropic(api_key=api_key)


def _extract_json(text: str) -> dict:
    """Pull the first {...} JSON object out of a model response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model output: {text[:200]}")
    return json.loads(match.group(0))


def ask_json(system: str, user: str, max_tokens: int = 1500) -> dict:
    client = _client()
    resp = client.messages.create(
        model=TEXT_MODEL,
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
