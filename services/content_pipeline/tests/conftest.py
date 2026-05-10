"""Shared test fixtures.

Sets a real Fernet key in env BEFORE app modules import — `app.core.security`
refuses the placeholder default at import time.
"""

import os

from cryptography.fernet import Fernet

# Generate a per-test-session Fernet key so security.py can initialise.
os.environ.setdefault("CP_SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("CP_API_KEY", "test-api-key")
os.environ.setdefault("CP_JWT_SECRET", "test-jwt-secret-not-the-placeholder")
# Make the analyzer / DB tests skip real network resources.
os.environ.setdefault("APP_ENV", "test")
