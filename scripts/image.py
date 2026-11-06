"""Unsplash image finder (ticket T6): best-effort photo attachment.

Per research notes (verified 2026-11-01): `source.unsplash.com` is DEAD
(503) — the search API is the living way: 
`GET https://api.unsplash.com/search/photos?query=...` with
`Authorization: Client-ID <key>`, take the first usable `urls.regular`
(≤5 MB for URL sends per the Telegram notes).

Free-tier friendliness: with no `UNSPLASH_ACCESS_KEY` the finder is
DISABLED (no request is ever attempted). Any failure — 403, rate limit,
empty results, network — returns None; the send is never blocked on an
image.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Optional

import requests

SEARCH_URL = "https://api.unsplash.com/search/photos"
_TIMEOUT = 8
_MAX_QUERY_LENGTH = 120  # keep the URL sane; longer queries just stop mattering


def _first_usable(payload: Any) -> Optional[Dict[str, Any]]:
    results = (payload or {}).get("results") or []
    for result in results:
        if not isinstance(result, dict):
            continue
        urls = result.get("urls") or {}
        regular = urls.get("regular")
        if regular:
            user = (result.get("user") or {}).get("name", "")
            return {"url": regular, "credit": str(user or "")}
    return None


class ImageFinder:
    """`find(content) -> {"url", "credit"} | None`. Failures are None."""

    def __init__(self, access_key: Optional[str] = None) -> None:
        self._key = access_key or os.environ.get("UNSPLASH_ACCESS_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def find(self, content: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None  # key missing: image path is simply off (no cost risk)
        query = str(content.get("image_query") or "").strip()[:_MAX_QUERY_LENGTH]
        if not query:
            return None
        try:
            response = requests.get(
                SEARCH_URL,
                params={"query": query, "per_page": 5},
                headers={"Authorization": f"Client-ID {self._key}"},
                timeout=_TIMEOUT,
            )
            if response.status_code != 200:
                return None
            return _first_usable(response.json())
        except (requests.RequestException, ValueError):
            return None
