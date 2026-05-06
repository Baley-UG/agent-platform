"""64-bit Charikar simhash over a token bag.

Used by the post upsert path to populate `ig_posts.caption_simhash`.
Two near-duplicate captions produce simhashes with low Hamming distance,
so a `WHERE caption_simhash = X OR hamming_distance(caption_simhash, X) <= 3`
query (M9-era helper) cheaply finds reposts.

This is a deliberately tiny implementation — pulling in a C-extension
library for ~30 lines of Python isn't worth the dependency cost. If
profiling later flags this as a hot path we can swap in `python-simhash`
without changing the column shape.
"""

import hashlib
import re
from typing import Iterable, List

_HASH_BITS = 64
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    """Lowercase word-character tokens. Good enough for caption-level
    similarity; keeps emoji/punctuation out of the bag so style differences
    don't drown out content overlap."""
    return _TOKEN_RE.findall(text.lower())


def _hash_token(token: str) -> int:
    """Stable 64-bit hash. md5 is fine here — we're not using it for
    cryptographic security, just for diffusion."""
    digest = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def simhash(text: str) -> int:
    """Compute the 64-bit simhash of `text`. Returns 0 for empty input."""
    if not text:
        return 0
    tokens = _tokens(text)
    if not tokens:
        return 0
    return _simhash_from_tokens(tokens)


def _simhash_from_tokens(tokens: Iterable[str]) -> int:
    counters = [0] * _HASH_BITS
    for token in tokens:
        token_hash = _hash_token(token)
        for bit in range(_HASH_BITS):
            counters[bit] += 1 if (token_hash >> bit) & 1 else -1
    out = 0
    for bit, value in enumerate(counters):
        if value > 0:
            out |= 1 << bit
    return out


def hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two simhashes. Useful for tests and
    near-duplicate queries (M9 will use it)."""
    return ((a ^ b) & ((1 << _HASH_BITS) - 1)).bit_count()


def to_signed_64(value: int) -> int:
    """Postgres BIGINT is signed — convert the unsigned simhash to fit.

    Round-trip safe: pass the result back through `from_signed_64` to
    recover the unsigned value before computing Hamming distances.
    """
    if value >= 1 << 63:
        return value - (1 << _HASH_BITS)
    return value


def from_signed_64(value: int) -> int:
    """Recover the unsigned simhash from a signed BIGINT readback."""
    if value < 0:
        return value + (1 << _HASH_BITS)
    return value
