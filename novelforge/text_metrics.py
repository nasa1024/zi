"""Language-aware text metrics used by drafting and quality checks.

The legacy Chinese pipeline treats Python code points as its content unit.  We
keep that behaviour for compatibility while giving English projects a real
Unicode-aware word count (contractions and hyphenated compounds count once).
"""
from __future__ import annotations

import re


# ``[^\W\d_]`` is a Unicode letter without depending on a third-party regex
# package.  Apostrophes inside contractions and hyphens inside compounds stay
# within one token: ``don't``, ``dragon-rider`` and ``O’Malley`` are one word.
_LETTER = r"[^\W\d_]"
_WORD_RE = re.compile(
    rf"{_LETTER}+(?:[\'’]{_LETTER}+)*(?:-{_LETTER}+(?:[\'’]{_LETTER}+)*)*"
    r"|\d+(?:[.,]\d+)*",
    re.UNICODE,
)
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_OTHER_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_ENGLISH_MARKERS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "he", "her", "his", "i", "in", "is", "it", "not", "of",
    "on", "or", "she", "that", "the", "their", "they", "this", "to", "was",
    "we", "were", "with", "you",
}
_OTHER_LATIN_MARKERS = {
    # High-frequency Spanish/French/German function words.  They are used only
    # as a strong negative signal (three or more), never to identify a locale.
    "avec", "dans", "das", "del", "der", "des", "die", "du", "el", "elle", "est",
    "et", "hacia", "la", "las", "le", "les", "los", "mais", "nicht", "para",
    "pero", "por", "pour", "que", "qui", "seine", "un", "una", "une", "und",
    "von", "zur",
}


def count_words(text: str) -> int:
    """Return a deterministic Unicode word count.

    Punctuation and standalone symbols do not count.  Decimal/grouped numbers
    count as one token.  The function is intentionally tokenizer-independent so
    persisted counts do not change when optional NLP packages are installed.
    """

    return sum(1 for _ in _WORD_RE.finditer(text or ""))


def count_characters(text: str) -> int:
    """Return the legacy-compatible Python code-point count.

    Whitespace is included because the existing Chinese draft pipeline uses
    ``len(text)``.  Changing that meaning here would silently alter old project
    thresholds and stored chapter statistics.
    """

    return len(text or "")


def count_content_units(text: str, language: str) -> int:
    """Count words for English and characters for all other languages."""

    if _language_base(language) == "en":
        return count_words(text)
    return count_characters(text)


def language_matches(text: str, language: str) -> bool:
    """Return whether the dominant script plausibly matches ``language``.

    This is a deterministic guard, not a probabilistic language detector.  It
    deliberately allows a small amount of cross-script text for names, quoted
    terms and system labels.  Empty/symbol-only content never matches.
    """

    value = text or ""
    if not value.strip():
        return False

    latin = len(_LATIN_RE.findall(value))
    cjk = len(_CJK_RE.findall(value))
    all_letters = len(_OTHER_LETTER_RE.findall(value))
    other = max(0, all_letters - latin - cjk)
    total = latin + cjk + other
    if total == 0:
        return False

    base = _language_base(language)
    if base == "en":
        # English prose may contain occasional CJK names/labels, but Latin must
        # remain overwhelmingly dominant.
        if latin <= 0 or latin / total < 0.70:
            return False
        words = [match.group(0).casefold() for match in _WORD_RE.finditer(value)]
        if len(words) >= 8:
            english_hits = sum(word in _ENGLISH_MARKERS for word in words)
            other_hits = sum(word in _OTHER_LATIN_MARKERS for word in words)
            if other_hits >= 3 and other_hits > english_hits * 1.5:
                return False
        return True
    if base == "zh":
        # Chinese technical prose often embeds ASCII identifiers, hence the
        # slightly more permissive threshold.
        return cjk > 0 and cjk / total >= 0.50
    # No deterministic implementation is promised for other locales yet.  A
    # non-empty letter-bearing text is the safest non-blocking fallback.
    return total > 0


def iter_words(text: str):
    """Yield regex matches for callers that also need evidence spans."""

    yield from _WORD_RE.finditer(text or "")


def _language_base(language: str | None) -> str:
    return str(language or "").strip().lower().replace("_", "-").split("-", 1)[0]


__all__ = [
    "count_words",
    "count_characters",
    "count_content_units",
    "language_matches",
]
