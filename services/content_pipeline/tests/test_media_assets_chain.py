"""Pure-logic tests for the media_assets chain walker.

We use a FakeSession that mirrors `session.get(MediaAsset, id)` so we can
test `walk_chain` / `active_version` without spinning a Postgres.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import pytest

from app.services.media_assets import active_version, walk_chain


@dataclass
class FakeAsset:
    id: uuid.UUID
    version: int
    previous_version_id: Optional[uuid.UUID] = None
    replaced_by_id: Optional[uuid.UUID] = None
    project_id: uuid.UUID = field(default_factory=uuid.uuid4)


class FakeSession:
    def __init__(self, assets: list[FakeAsset]) -> None:
        self._by_id = {a.id: a for a in assets}

    def get(self, _model, asset_id):  # noqa: ARG002
        return self._by_id.get(asset_id)


def _chain_three() -> tuple[list[FakeAsset], FakeAsset, FakeAsset, FakeAsset]:
    """Build a 3-version chain: v1 → v2 → v3 (active)."""
    v1 = FakeAsset(id=uuid.uuid4(), version=1)
    v2 = FakeAsset(id=uuid.uuid4(), version=2, previous_version_id=v1.id)
    v3 = FakeAsset(id=uuid.uuid4(), version=3, previous_version_id=v2.id)
    v1.replaced_by_id = v2.id
    v2.replaced_by_id = v3.id
    return [v1, v2, v3], v1, v2, v3


def test_walk_chain_from_root_returns_all_three():
    assets, v1, _v2, _v3 = _chain_three()
    chain = walk_chain(FakeSession(assets), v1.id)
    assert [a.version for a in chain] == [1, 2, 3]


def test_walk_chain_from_middle_still_returns_all_three():
    assets, _v1, v2, _v3 = _chain_three()
    chain = walk_chain(FakeSession(assets), v2.id)
    assert [a.version for a in chain] == [1, 2, 3]


def test_walk_chain_from_active_returns_all_three():
    assets, _v1, _v2, v3 = _chain_three()
    chain = walk_chain(FakeSession(assets), v3.id)
    assert [a.version for a in chain] == [1, 2, 3]


def test_walk_chain_handles_single_version():
    only = FakeAsset(id=uuid.uuid4(), version=1)
    chain = walk_chain(FakeSession([only]), only.id)
    assert [a.version for a in chain] == [1]


def test_walk_chain_returns_empty_for_unknown_id():
    assert walk_chain(FakeSession([]), uuid.uuid4()) == []


def test_walk_chain_breaks_on_cycle():
    """Corrupted chain (a → b → a) should not loop forever."""
    a = FakeAsset(id=uuid.uuid4(), version=1)
    b = FakeAsset(id=uuid.uuid4(), version=2, previous_version_id=a.id)
    a.replaced_by_id = b.id
    b.replaced_by_id = a.id  # cycle
    chain = walk_chain(FakeSession([a, b]), a.id)
    # Should bail before infinite loop. Order may vary; just ensure bounded.
    assert len(chain) <= 2


def test_active_version_returns_latest():
    assets, v1, _v2, v3 = _chain_three()
    assert active_version(FakeSession(assets), v1.id).version == v3.version


def test_active_version_returns_none_for_missing():
    assert active_version(FakeSession([]), uuid.uuid4()) is None
