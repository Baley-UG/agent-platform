"""Fernet wrapper for at-rest encryption of social_accounts credentials.

Pattern lifted from `ig_scraper/app/services/crypto.py`. We refuse to start
with the placeholder key so production never ships unencrypted.
"""

from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_PLACEHOLDER = "changeme-fernet-key"


def _build_fernet() -> Fernet:
    """Construct the Fernet instance, refusing the placeholder key."""
    key = settings.CP_SECRET_KEY
    if key == _PLACEHOLDER:
        raise RuntimeError(
            "CP_SECRET_KEY is set to the placeholder value. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"CP_SECRET_KEY is not a valid Fernet key (must be 32 url-safe base64 bytes): {exc}"
        ) from exc


_fernet: Optional[Fernet] = None


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _build_fernet()
    return _fernet


def encrypt(plaintext: str) -> bytes:
    """Encrypt a UTF-8 string. Raises if Fernet is unconfigured."""
    return _get().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt a Fernet token to a UTF-8 string.

    Raises a clear error if the token can't be decrypted under the current
    key — usually means the key was rotated without re-encrypting stored
    blobs.
    """
    try:
        return _get().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Stored ciphertext failed to decrypt under the current CP_SECRET_KEY. "
            "If you rotated the key, re-encrypt all social_accounts.credentials_encrypted rows."
        ) from exc


def encrypt_optional(plaintext: Optional[str]) -> Optional[bytes]:
    return encrypt(plaintext) if plaintext is not None else None


def decrypt_optional(ciphertext: Optional[bytes]) -> Optional[str]:
    return decrypt(ciphertext) if ciphertext is not None else None
