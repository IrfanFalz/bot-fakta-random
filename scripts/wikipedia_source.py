"""Wikipedia random-article source (ticket T3/T2 line of work: first real source).

Fetches `GET https://en.wikipedia.org/api/rest_v1/page/random/summary`
(no auth) and adapts the response to the uniform PRD contract:

    {title, body, keywords, source_name, image_query}

Failure handling (per the ticket: a red Actions run must NEVER result from a
source failure): every failure mode — timeout, non-200, non-JSON, missing or
unexpectedly shaped fields — raises `SourceError`, a typed error the
orchestrator treats as a clean skip-this-run.

Note on etiquette (verified 2026-11, see repo notes): Wikimedia blocks
UA-less script-like clients, so a descriptive User-Agent is mandatory.
`thumbnail` / `originalimage` extraction is deliberately skipped here —
images arrive with ticket T6; this module only maps what the uniform
contract needs today.

Production code stays stdlib + `requests` (frugality rule from AGENTS.md).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import requests

_RANDOM_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
_REQUEST_TIMEOUT_SECONDS = 8

# Wikimedia etiquette: descriptive, contactable UA — throttles/403s otherwise.
_USER_AGENT = (
    "bot-fakta-random/1.0 (personal Telegram fact bot; "
    "https://github.com/IrfanFalz/bot-fakta-random)"
)


class SourceError(RuntimeError):
    """Any failure of a content source; the orchestrator skips the run."""


# Very small stopword list for deriving title keywords / image queries.
# English + a few Indonesian words — titles come from en.wikipedia.org.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "it",
    "its", "his", "her", "that", "this", "why", "how", "what", "who",
    "apa", "itu", "yang", "kenapa", "dan", "dari", "di", "ke", "untuk",
})


def extract_keywords(text: str, limit: int = 5) -> List[str]:
    """Top few non-stopword words of `text`, lowercased, in order."""
    words = re.findall(r"[a-zA-Z0-9']+", text or "")
    keywords: List[str] = []
    for word in words:
        clean = word.lower().strip("'")
        if not clean or clean in _STOPWORDS:
            continue
        if clean not in keywords:
            keywords.append(clean)
        if len(keywords) >= limit:
            break
    return keywords


def make_image_query(text: str) -> str:
    """Title minus stopwords — a sane subject query for the later image ticket."""
    words = [
        word for word in re.findall(r"[a-zA-Z0-9']+", text or "")
        if word.lower().strip("'") not in _STOPWORDS
    ]
    return " ".join(words)


class WikipediaSource:
    """Real content source port: one call returns one random article summary."""

    def __init__(self, url: str = _RANDOM_SUMMARY_URL,
                 timeout: int = _REQUEST_TIMEOUT_SECONDS) -> None:
        self.url = url
        self.timeout = timeout

    def fetch(self) -> Dict[str, Any]:
        """Return the uniform contract or raise `SourceError` (never None)."""
        data = self._fetch_raw_data()
        return self._adapt(data)

    # -- internals: keep _adapt pure-ish so it is trivially testable --------

    def _fetch_raw_data(self) -> Dict[str, Any]:
        try:
            response = requests.get(
                self.url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # timeout / DNS / refused
            raise SourceError(f"wikipedia request failed: {exc}") from exc

        if response.status_code != 200:
            raise SourceError(
                f"wikipedia answered {response.status_code}, expected 200")

        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            # Gateway hiccups (e.g. 503 HTML pages) must not crash the run.
            raise SourceError(
                f"wikipedia returned non-JSON content type {content_type!r}")

        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"wikipedia returned non-JSON body: {exc}") from exc

        if not isinstance(data, dict):
            raise SourceError("wikipedia response is not a JSON object")
        return data

    def _adapt(self, data: Dict[str, Any]) -> Dict[str, Any]:
        title = str(data.get("title") or "").strip()
        # Notes (2026-11): `extract` is the summary text; `description` is a
        # short one-liner. Body prefers the richer extract.
        body = str(data.get("extract") or data.get("description") or "").strip()
        if not title or not body:
            raise SourceError(
                "wikipedia summary is missing title/body fields; shape is "
                "unexpected -> skip this run")
        return {
            "title": title,
            "body": body,
            "keywords": extract_keywords(title),
            "source_name": "wikipedia",
            # Full mapping of the response's image fields arrives with T6;
            # for now only the query string the image ticket will need.
            "image_query": make_image_query(title),
        }
