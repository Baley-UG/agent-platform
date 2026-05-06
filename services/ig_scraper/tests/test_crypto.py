"""Round-trip tests for the Fernet wrapper.

The crypto layer is small but load-bearing — every account password and
proxy password goes through it. These tests run without a DB and without
instagrapi, so they're fast and deterministic.
"""

import os

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _set_secret_key(monkeypatch):
    """Generate a fresh Fernet key for each test so we don't share state."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("IG_SECRET_KEY", key)
    monkeypatch.setenv("APP_ENV", "test")
    # Force re-import so settings/_fernet pick up the new env.
    import importlib

    import app.core.config as cfg
    import app.services.crypto as crypto

    importlib.reload(cfg)
    importlib.reload(crypto)
    return crypto


def test_round_trip(_set_secret_key):
    crypto = _set_secret_key
    plaintext = "p@ssw0rd-with-üñîcödé-✨"
    ciphertext = crypto.encrypt(plaintext)
    assert isinstance(ciphertext, bytes)
    assert ciphertext != plaintext.encode("utf-8")
    assert crypto.decrypt(ciphertext) == plaintext


def test_optional_round_trip(_set_secret_key):
    crypto = _set_secret_key
    assert crypto.encrypt_optional(None) is None
    assert crypto.decrypt_optional(None) is None
    ct = crypto.encrypt_optional("hello")
    assert crypto.decrypt_optional(ct) == "hello"


def test_two_encryptions_differ(_set_secret_key):
    """Fernet uses a random IV — same input must yield different ciphertexts."""
    crypto = _set_secret_key
    a = crypto.encrypt("same")
    b = crypto.encrypt("same")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same"


def test_invalid_token_raises(_set_secret_key, monkeypatch):
    """Decrypting under a different key must fail loudly, not silently."""
    crypto = _set_secret_key
    ct = crypto.encrypt("secret")

    # Rotate the key without re-encrypting.
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("IG_SECRET_KEY", new_key)
    import importlib

    import app.core.config as cfg
    import app.services.crypto as crypto2

    importlib.reload(cfg)
    importlib.reload(crypto2)

    with pytest.raises(RuntimeError, match="InvalidToken"):
        crypto2.decrypt(ct)


def test_placeholder_key_rejected(monkeypatch):
    """Importing crypto with the placeholder key should refuse to encrypt."""
    monkeypatch.setenv("IG_SECRET_KEY", "changeme-fernet-key")
    import importlib

    import app.core.config as cfg
    import app.services.crypto as crypto

    importlib.reload(cfg)
    importlib.reload(crypto)

    with pytest.raises(RuntimeError, match="not configured"):
        crypto.encrypt("anything")
