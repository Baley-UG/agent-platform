"""Reference dedup helpers — perceptual hash + caption embedding lookups.

CP-M8 surface only the storage + comparison primitives; the actual hash
computation (per-frame phash for video, average-hash fallback for images)
lives in the import path and is wired in CP-M8.5 when we add the
imagehash + ffmpeg-frame-extract dependencies.

For now:
- `hamming_distance(a, b)` works on raw bytes (perceptual hash).
- `find_near_duplicates(session, project_id, content_hash, max_distance)`
  scans `content_references` for already-imported items within Hamming
  distance.
- The admin panel displays "duplicate of <ref_id> (distance N)" before
  the admin approves an incoming candidate.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.models.content_references import ContentReference


def hamming_distance(a: bytes, b: bytes) -> int:
    """Bit-distance between two equal-length byte strings.

    Returns -1 when lengths differ (caller should treat as "incomparable").
    """
    if a is None or b is None or len(a) != len(b):
        return -1
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def find_near_duplicates(
    session: Session,
    project_id: uuid.UUID,
    content_hash: bytes,
    *,
    max_distance: int = 6,
    limit: int = 10,
    exclude_id: Optional[uuid.UUID] = None,
) -> List[Tuple[ContentReference, int]]:
    """Scan project's references and return (row, distance) pairs within
    `max_distance`. O(N) per call; fine while reference pools are small.
    For large pools CP-M8.5 can switch to an LSH index.
    """
    stmt = select(ContentReference).where(
        ContentReference.project_id == project_id,
        ContentReference.content_hash.is_not(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(ContentReference.id != exclude_id)

    out: List[Tuple[ContentReference, int]] = []
    for row in session.exec(stmt).all():
        if row.content_hash is None:
            continue
        dist = hamming_distance(bytes(row.content_hash), content_hash)
        if dist == -1:
            continue
        if dist <= max_distance:
            out.append((row, dist))
    out.sort(key=lambda pair: pair[1])
    return out[:limit]
