"""Shared keyword/stopword helper (single home — code-review fix).

Previously four source modules each carried a near-identical `_keywords_of`
and there were TWO divergent `_STOPWORDS` frozensets. This module is now
the single home: the tokenizer regex (letters/digits/apostrophes), the
stopword list (English + a few Indonesian words — titles come from
en.wikipedia.org and casual sources alike), and dedupe-in-order semantics
borrowed from the original wikipedia implementation.
"""
from __future__ import annotations

import re
from typing import List

STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "it",
    "its", "his", "her", "that", "this", "why", "how", "what", "who",
    "do", "does", "did",
    "apa", "itu", "yang", "kenapa", "dan", "dari", "di", "ke", "untuk",
})

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")


def keywords_of(text: str, limit: int = 5) -> List[str]:
    """Top few non-stopword words of `text`, lowercased, in order, deduped."""
    keys: List[str] = []
    for word in _WORD_RE.findall(text or ""):
        clean = word.lower().strip("'")
        if not clean or clean in STOPWORDS:
            continue
        if clean not in keys:
            keys.append(clean)
        if len(keys) >= limit:
            break
    return keys


def image_query(text: str, limit: int = 0) -> str:
    """Title minus stopwords — a sane subject query for the image ticket.

    Original casing is preserved (wikipedia's image queries keep the title's
    capitalization; searches don't care).
    """
    words = [
        word.strip("'")
        for word in _WORD_RE.findall(text or "")
        if word.lower().strip("'") not in STOPWORDS
    ]
    if limit:
        words = words[:limit]
    return " ".join(words)
