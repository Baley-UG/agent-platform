"""Shared test fixtures.

`AD_SECRET_KEY` is set before any app module is imported: `crypto.py`
refuses to build a Fernet from the placeholder default, and several tests
round-trip encrypted values.
"""

import json
import os
import pathlib

import pytest

os.environ.setdefault("APP_ENV", "test")
# A real (throwaway) Fernet key so crypto round-trips work under test.
os.environ.setdefault("AD_SECRET_KEY", "Eqwn0SZ1LZxwMHM6qqwyzo4WP0BEBtZK0CX5qa4hv6k=")
os.environ.setdefault("AD_SCRAPER_API_KEY", "test-api-key")

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def material_list_page() -> dict:
    """A real `materialList` page, trimmed and with signatures redacted.

    Two materials captured from the live API: one video (type 202) with a
    three-way `campaign` fan-out, one image (type 102). The `auth_key`
    signatures are replaced with `REDACTED` placeholders — the leading
    epoch is preserved so expiry parsing is still exercised — and the facet
    arrays are cut to three entries each.

    `__typename` was added to the campaign entries, since the capture
    predates our query requesting it.
    """
    return json.loads((FIXTURES / "material_list_page.json").read_text())


@pytest.fixture(scope="session")
def video_material(material_list_page: dict) -> dict:
    """The video creative from the fixture page."""
    return next(row["material"] for row in material_list_page["data"] if row["material"]["type"] == 202)


@pytest.fixture(scope="session")
def image_material(material_list_page: dict) -> dict:
    """The image creative from the fixture page."""
    return next(row["material"] for row in material_list_page["data"] if row["material"]["type"] == 102)
