"""Repetition gate (ticket T3, spec "No-repeats").

Keeps recent topics from replaying: a candidate is compared against the
persisted history with two cheap, deterministic signals:

- **title similarity**: `difflib.SequenceMatcher` over normalized titles
  (lowercased, punctuation stripped, NFKC unicode);
- **keyword overlap**: Jaccard similarity of the candidate's keywords and
  a history entry's keywords, only when both sides de-duplicate non-vertex.

A candidate counts as a duplicate when `max(title_similarity, keyword_jaccard)`
reaches the threshold (`DEFAULT_THRESHOLD`, tunable with the
`DEDUPE_THRESHOLD` env var in production runs). Pure functions only — the
orchestrator owns the retry budget (max 3 attempts) and the
send-anyway fallback, per the ticket's acceptance criteria.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Dict, List, Sequence, Set

DEFAULT_THRESHOLD = 0.6
MIN_TITLE_LENGTH = 3  # junk shorter than this can't carry similarity meaning

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_text(text: Any) -> str:
    """Lowercased, punctuation-stripped, single-spaced text for comparison."""
    raw = unicodedata.normalize("NFKC", str(text or "")).casefold()
    stripped = _PUNCT.sub(" ", raw)
    return _SPACES.sub(" ", stripped).strip()


def _keyword_set(keywords: Any) -> Set[str]:
    """Normalized, non-empty keyword set (tolerates non-string entries)."""
    words: Set[str] = set()
    for word in keywords or []:
        normalized = normalize_text(word)
        if len(normalized) >= MIN_TITLE_LENGTH:
            words.add(normalized)
    return words


def title_similarity(candidate_title: Any, history_title: Any) -> float:
    """`difflib.SequenceMatcher` ratio over normalized titles (0..1)."""
    left = normalize_text(candidate_title)
    right = normalize_text(history_title)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def keyword_overlap(candidate_keywords: Sequence[Any],
                    history_keywords: Sequence[Any]) -> float:
    """Jaccard similarity of two keyword sets (0..1, 0 when either is empty)."""
    left = _keyword_set(candidate_keywords)
    right = _keyword_set(history_keywords)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def is_duplicate(candidate: Dict[str, Any],
                 history: List[Dict[str, Any]],
                 threshold: float = DEFAULT_THRESHOLD) -> bool:
    """True when `candidate` resembles ANY history entry beyond `threshold`.

    History entries follow the PRD section 6 shape above (though additional
    or missing keys are tolerated: a missing/empty field contributes 0 to
    the score, never a crash).
    """
    threshold = max(0.0, min(1.0, float(threshold)))
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        score = max(
            title_similarity(candidate.get("title"), entry.get("title")),
            keyword_overlap(candidate.get("keywords") or [],
                            entry.get("keywords") or []),
        )
        if score >= threshold:
            return True
    return False
