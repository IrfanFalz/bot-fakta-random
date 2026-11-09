"""Numbers API source (ticket T4): number/date facts, kept with a cane.

Research notes (verified 2026-11-01): numbersapi.com is degraded/down
(404s on https and http alike). The source is implemented to its
documented contract so it self-heals if the service returns, but assume
it fails in practice: a short timeout, a typed error, and the orchestrator
moves on to another source. Never a credential, never a cost.

Uniform contract: `{title, body, keywords, source_name, image_query}`.
"""
from __future__ import annotations

import random as _random
from typing import Any, Dict

import requests

import keywords
from wikipedia_source import SourceError  # same typed contract

_TRIVIA_URL = "https://numbersapi.com/{number}/trivia?json"

# keeps the runner off IP ban territory if the service comes back:
_TIMEOUT = 3  # research note: short (2-3s), do not block the run on it


class NumbersSource:
    """Small typed wrapper around numbersapi.com (best-effort, graceful)."""

    def fetch(self) -> Dict[str, Any]:
        number = _random.randint(11, 999)
        url = _TRIVIA_URL.format(number=number)
        try:
            response = requests.get(url, timeout=_TIMEOUT)
            if response.status_code != 200:
                raise SourceError(
                    f"numbersapi returned HTTP {response.status_code}")
            payload = response.json()
        except requests.RequestException as exc:
            raise SourceError(f"numbersapi request failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError(f"numbersapi returned non-JSON body: {exc}") from exc
        text = str(payload.get("text") or "").strip()
        if not text or not payload.get("found"):
            raise SourceError("numbersapi returned no fact")

        title = f"Fakta angka {number}"
        body = f"Angka {number}: {text}"
        fact_keywords = keywords.keywords_of(f"angka {number} " + text)
        return {
            "title": title,
            "body": body,
            "keywords": fact_keywords,
            "source_name": "numbers",
            "image_query": "numbers",
        }
