"""Open Trivia DB source (ticket T4): random trivia as a casual Q&A message.

Endpoint per research notes (no key, works as of 2026-11): 
`GET https://opentdb.com/api.php?amount=N&difficulty=easy&type=multiple`.
One IP: max one request per 5 seconds (we fetch at most one small batch per
send — the bot sends only ~14 times a day, so the throttle is a non-issue);
`response_code` 0 means success, any other value is a typed failure.

Questions and answers arrive HTML-entity encoded (`Don&#039;t`) — decoded
with stdlib `html.unescape`.

Uniform contract: `{title, body, keywords, source_name, image_query}`.
"""
from __future__ import annotations

import html
import random as _random
from typing import Any, Dict

import requests

import keywords
from wikipedia_source import SourceError  # same typed contract

API_URL = "https://opentdb.com/api.php"

# Casual territory only, per PRD 1 (`sains, alam, hewan, ... trivia aneh`).
# Category 9 = General Knowledge; other ids are pulled in by `category=`.
PREFERRED_CATEGORIES = (9, 18, 27, 22, 25, 19)

_TIMEOUT = 8


def _one_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("response_code") != 0:
        raise SourceError(
            f"opentdb response_code={payload.get('response_code')} "
            f"(0=ok, 1=no result, 5=rate limit)")
    results = payload.get("results") or []
    usable = [r for r in results
              if isinstance(r, dict) and r.get("question")
              and r.get("correct_answer")]
    if not usable:
        raise SourceError("opentdb returned no usable results")
    return _random.choice(usable)


class TriviaSource:
    """One small Open Trivia DB batch per pick, adapted to the uniform
    contract (question + its correct answer spliced into the body)."""

    def fetch(self) -> Dict[str, Any]:
        category = _random.choice(PREFERRED_CATEGORIES)
        params = {
            "amount": 6,
            "difficulty": "easy",
            "type": "multiple",
            "category": category,
        }
        try:
            response = requests.get(API_URL, params=params, timeout=_TIMEOUT)
            if response.status_code != 200:
                raise SourceError(f"opentdb returned HTTP {response.status_code}")
            payload = response.json()
        except requests.RequestException as exc:
            raise SourceError(f"opentdb request failed: {exc}") from exc
        selected = _one_result(payload)

        question = html.unescape(str(selected.get("question") or "")).strip()
        answer = html.unescape(str(selected.get("correct_answer") or "")).strip()
        trivia_category = html.unescape(str(selected.get("category") or "")).strip()
        title = question
        body = (f"Pertanyaan random ({trivia_category}): {question} "
                f"Jawabannya ternyata: {answer}.")
        card_keywords = keywords.keywords_of(f"{trivia_category} {question}")
        return {
            "title": title,
            "body": body,
            "keywords": card_keywords,
            "source_name": "trivia",
            "image_query": keywords.image_query(
                f"{trivia_category} {question}", 3) or trivia_category,
        }
