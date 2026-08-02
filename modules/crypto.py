"""Symmetric encryption for secrets-at-rest — currently just integration
credentials (modules/integration_store.py's company_integrations.credentials
column). Zero Streamlit/DB dependency, same tier as modules/auth.py's
password hashing.

Key comes from ELECTROGRADER_ENCRYPTION_KEY, one Fernet key per environment
(generate with scripts/generate_encryption_key.py). Never rotate this key
in place without a re-encryption migration — existing ciphertext becomes
unreadable, not just "needs re-login" like a session-secret rotation would.
"""
import json
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class EncryptionKeyMissingError(RuntimeError):
    pass


_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("ELECTROGRADER_ENCRYPTION_KEY")
        if not key:
            raise EncryptionKeyMissingError(
                "ELECTROGRADER_ENCRYPTION_KEY is not set. Generate one with "
                "`python scripts/generate_encryption_key.py` and add it to .env."
            )
        _fernet = Fernet(key.strip().encode("ascii"))
    return _fernet


def encrypt_dict(data: dict) -> str:
    """JSON-serializes then encrypts. Returns an opaque token string safe to
    store in a TEXT column."""
    raw = json.dumps(data or {}).encode("utf-8")
    return _get_fernet().encrypt(raw).decode("ascii")


def decrypt_dict(token: str) -> dict:
    """Inverse of encrypt_dict. Returns {} for an empty/None token (a
    disconnected integration has no credentials left to decrypt)."""
    if not token:
        return {}
    try:
        raw = _get_fernet().decrypt(token.encode("ascii"))
    except InvalidToken:
        raise InvalidToken(
            "Could not decrypt stored credentials — ELECTROGRADER_ENCRYPTION_KEY "
            "may have changed since they were saved."
        )
    return json.loads(raw.decode("utf-8"))


def mask_secret(value: str, keep: int = 4) -> str:
    """Display-only redaction, e.g. for a 'Recent activity' log line — never
    used for anything that round-trips back into a real credential."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
