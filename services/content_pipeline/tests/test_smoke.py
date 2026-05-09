"""Smoke tests for CP-M1.

These exercise:
- importability of every module
- SQLModel.metadata table registration (count + names)
- FastAPI app builds + every router is mounted
- S3 helpers compute keys / URLs without hitting any real backend
- security.py round-trips with the per-session Fernet key
"""

from __future__ import annotations

import importlib
import uuid

import pytest


def test_imports():
    """Every package imports cleanly."""
    importlib.import_module("app.main")
    importlib.import_module("app.worker")
    importlib.import_module("app.scheduler")


def test_metadata_registers_all_tables():
    import app.models  # noqa: F401  (registers tables)
    from sqlmodel import SQLModel

    expected = {
        "projects",
        "brand_kits",
        "social_accounts",
        "content_references",
        "templates",
        "music_tracks",
        "media_assets",
        "model_routes",
        "generation_calls",
        "reference_intake_rules",
        "scenarios",
        "reference_usages",
    }
    actual = {t.name for t in SQLModel.metadata.tables.values() if t.schema == "content_pipeline"}
    assert expected.issubset(actual), f"missing: {expected - actual}"


def test_app_routes_mounted():
    from app.main import app

    paths = {route.path for route in app.routes}
    # API prefix is /api/v1
    expected_prefixes = {
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/brand-kits",
        "/api/v1/projects/{project_id}/social-accounts",
        "/api/v1/projects/{project_id}/templates",
        "/api/v1/projects/{project_id}/music-tracks",
        "/api/v1/projects/{project_id}/assets/upload-url",
        "/api/v1/projects/{project_id}/model-routes",
        "/api/v1/global/model-routes",
        "/api/v1/projects/{project_id}/references",
        "/api/v1/projects/{project_id}/intake-rules",
        "/api/v1/projects/{project_id}/inbox/candidates",
        "/api/v1/projects/{project_id}/scenarios",
    }
    for prefix in expected_prefixes:
        assert any(p.startswith(prefix) for p in paths), f"no route starts with {prefix}"


def test_security_round_trip():
    from app.core import security

    plain = "merhaba dünya — non-ASCII pwd !"
    token = security.encrypt(plain)
    assert security.decrypt(token) == plain
    assert security.encrypt_optional(None) is None
    assert security.decrypt_optional(None) is None


def test_security_rejects_placeholder(monkeypatch):
    """If someone forgets to override CP_SECRET_KEY, we fail loudly."""
    from app.core import security
    from app.core.config import settings

    monkeypatch.setattr(settings, "CP_SECRET_KEY", "changeme-fernet-key", raising=False)
    monkeypatch.setattr(security, "_fernet", None)
    with pytest.raises(RuntimeError, match="placeholder"):
        security.encrypt("nope")


def test_s3_keys_are_namespaced():
    from app.core import s3

    pid = uuid.uuid4()
    key = s3.make_key(pid, "templates", "intro.mp4")
    assert key.startswith(f"projects/{pid}/templates/")
    assert key.endswith("-intro.mp4")


def test_s3_path_style_url():
    from app.core import s3
    from app.core.config import settings

    # Path-style is the dev/MinIO default.
    settings.S3_USE_PATH_STYLE = True
    url = s3.public_url("projects/abc/finals/test.mp4")
    assert url.endswith(f"/{settings.S3_BUCKET}/projects/abc/finals/test.mp4")


def test_model_route_schemas_round_trip():
    from app.schemas.model_routes import ModelRouteCreate

    payload = ModelRouteCreate(
        task_key="scene_image",
        provider="fal",
        model_id="fal-ai/flux/dev",
        params={"image_size": "portrait_16_9"},
        priority=0,
        cost_unit="image",
        cost_per_unit_usd=0.025,
    )
    assert payload.task_key == "scene_image"
    assert payload.cost_per_unit_usd == 0.025
