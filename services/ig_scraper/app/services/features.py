"""Caption feature extraction.

Cheap, deterministic features computed once per post upsert and stored
on `ig_posts` so the AI generation pipeline (Phase 2) can filter by
content shape without re-parsing every caption.

Trade-offs:
- Language detection is `langdetect` — fast, no model download, ~85%
  accurate on short texts. Falls back to None on failure.
- Emoji counting uses Unicode property ranges (no `emoji` lib) so we
  don't pay another dependency for ~10 lines of code.
- has_question / has_cta are regex-based: fast, deterministic, easy to
  extend with new patterns. Patterns cover Turkish + English because
  those are the two ig_scraper tenants today.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from app.services.simhash import simhash, to_signed_64


# Hashtag: `#` followed by Unicode word characters. Matches "#yenilanc1"
# and "#örnek" but not "##" or "#".
_HASHTAG_RE = re.compile(r"(?<!\w)#(\w+)", re.UNICODE)
# Mention: `@username` — IG handles are ASCII-only, periods + underscores allowed.
_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9._]+)")
# Emoji: pictographic ranges from Unicode 16. Not exhaustive but catches
# the 99% of emoji in real captions. We accept some false positives
# (CJK symbols) in exchange for zero deps.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical
    "\U0001F780-\U0001F7FF"  # geometric extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"  # extended-A
    "\U0001FA70-\U0001FAFF"  # extended-B
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "]"
)

# Question detector: explicit `?` (incl. fullwidth) OR a Turkish question
# particle. Turkish marks questions with "mi/mı/mu/mü" suffixes; common
# enough in captions that we want them counted.
_QUESTION_RE = re.compile(
    r"[?？]"
    r"|\b(mi|mı|mu|mü)\b",
    re.IGNORECASE,
)

# CTA patterns: imperative verbs that drive engagement. Covers the
# Turkish + English vocabulary that shows up most in our scrapes. Keep
# these word-boundary anchored so we don't false-positive on substrings.
_CTA_PATTERNS = [
    # English
    r"\b(check|click|swipe|tap|save|share|comment|tag|follow|subscribe|sign\s*up|shop\s*now|buy\s*now|learn\s*more|read\s*more|don't\s*miss)\b",
    r"\blink\s+in\s+bio\b",
    r"\bdm\s+(me|us|for)\b",
    # Turkish
    r"\b(takip|takipte|paylaş|kaydet|yorumla|yorumlay|abone|tıkla|bağlantı|profilime|biyodaki)",
    r"\blink\s+(profilde|biyoda|biyodaki)\b",
    r"\b(şimdi|hemen)\s+(al|sat|gör|inceleyin|inceleyin)\b",
]
_CTA_RE = re.compile("|".join(_CTA_PATTERNS), re.IGNORECASE | re.UNICODE)


@dataclass
class CaptionFeatures:
    """Result bundle. All fields aligned with `ig_posts` columns.

    `caption_simhash_signed` is what gets stored in BIGINT; round-trip
    via `simhash.from_signed_64()` to compare against fresh simhashes.
    """

    language: Optional[str]
    emoji_count: int
    hashtag_count: int
    mention_count: int
    caption_length: int
    has_question: bool
    has_cta: bool
    caption_simhash_signed: int
    hashtags: List[str]
    mentions: List[str]


def _detect_language(text: str) -> Optional[str]:
    """Return ISO 639-1 lang code, or None if detection fails or input
    is too short to be informative."""
    if len(text) < 10:
        return None
    try:
        # Imported lazily so that test environments without langdetect
        # only break tests that explicitly use it.
        from langdetect import DetectorFactory, detect

        # Make detection deterministic across runs.
        DetectorFactory.seed = 0
        return detect(text)
    except Exception:  # noqa: BLE001 — broad on purpose
        return None


def extract(caption: Optional[str]) -> CaptionFeatures:
    """Compute every caption feature in one pass.

    Returns zero-valued features for None / empty captions so the
    upsert pipeline can call this unconditionally.
    """
    text = (caption or "").strip()
    if not text:
        return CaptionFeatures(
            language=None,
            emoji_count=0,
            hashtag_count=0,
            mention_count=0,
            caption_length=0,
            has_question=False,
            has_cta=False,
            caption_simhash_signed=0,
            hashtags=[],
            mentions=[],
        )

    hashtags = [m.lower() for m in _HASHTAG_RE.findall(text)]
    mentions = [m.lower() for m in _MENTION_RE.findall(text)]
    return CaptionFeatures(
        language=_detect_language(text),
        emoji_count=len(_EMOJI_RE.findall(text)),
        hashtag_count=len(hashtags),
        mention_count=len(mentions),
        caption_length=len(text),
        has_question=bool(_QUESTION_RE.search(text)),
        has_cta=bool(_CTA_RE.search(text)),
        caption_simhash_signed=to_signed_64(simhash(text)),
        # De-duplicate while preserving order so list-equality tests stay stable.
        hashtags=list(dict.fromkeys(hashtags)),
        mentions=list(dict.fromkeys(mentions)),
    )
