"""Symmetric encryption for at-rest secrets.

A single Fernet key (`AD_SECRET_KEY`) protects the two stored secrets: the
YouCloud password and the current session cookie. Same reasoning as
ig_scraper's `crypto.py` — no per-row keys; the operational cost of
rotation and lookup outweighs the marginal benefit against this service's
threat model (a leaked DB dump).

Key format: a 32-byte url-safe base64 string. Generate with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Fail-fast: if `AD_SECRET_KEY` is missing or malformed, the first
encrypt/decrypt raises. We never silently fall back to plaintext storage.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.logging import logger

# Sentinels for the placeholder default — production must override these.
_PLACEHOLDER_KEYS = {"changeme-fernet-key", "", None}


def _build_fernet() -> Fernet:
    """Construct the Fernet instance, rejecting the placeholder key."""
    key = settings.AD_SECRET_KEY
    if key in _PLACEHOLDER_KEYS:
        raise RuntimeError(
            "AD_SECRET_KEY is not configured. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
            "and set it in the repo .env before storing YouCloud credentials."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "AD_SECRET_KEY is not a valid Fernet key (must be 32 bytes url-safe base64). Re-generate one."
        ) from exc


_fernet: Fernet | None = None


def _fernet_instance() -> Fernet:
    """Return the memoised Fernet, building it on first use.

    Lazy so importing this module doesn't crash dev tooling that runs
    without AD_SECRET_KEY set (e.g. unit tests of unrelated modules).
    """
    global _fernet
    if _fernet is None:
        _fernet = _build_fernet()
    return _fernet


def encrypt(plaintext: str) -> bytes:
    """Encrypt a UTF-8 string. Returns the Fernet ciphertext as bytes."""
    if plaintext is None:
        raise ValueError("encrypt() called with None")
    return _fernet_instance().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt Fernet ciphertext back to the original UTF-8 string."""
    if not ciphertext:
        raise ValueError("decrypt() called with empty ciphertext")
    try:
        return _fernet_instance().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        logger.error("crypto_invalid_token", note="key may have rotated without re-encrypting rows")
        raise RuntimeError("InvalidToken — AD_SECRET_KEY likely rotated; re-encrypt stored secrets.") from exc


def encrypt_optional(plaintext: str | None) -> bytes | None:
    """Encrypt or pass through None."""
    return None if plaintext is None else encrypt(plaintext)


def decrypt_optional(ciphertext: bytes | None) -> str | None:
    """Decrypt or pass through None."""
    return None if ciphertext is None else decrypt(ciphertext)
