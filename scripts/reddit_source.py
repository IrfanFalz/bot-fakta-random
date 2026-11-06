"""Reddit source (ticket T4): weird questions from public `.json` endpoints.

Reality check (verified 2026-11-01, see research notes): Reddit escalated
blocking of unauthenticated `.json` for non-browser clients and datacenter
IPs — GitHub Actions runners get 403/429 most of the time. This source is
therefore a single polite attempt per pick (no retries: aggression only
earns IP bans), and every failure becomes a typed `SourceError` the
orchestrator maps to a green skip and another source. Nothing here requires
credentials; free-tier by construction.

Uniform contract: `{title, body, keywords, source_name, image_query}`.
"""
from __future__ import annotations

import random as _random
import string
from typing import Any, Dict

import requests

from wikipedia_source import SourceError  # same typed contract

SUBREDDITS = (
    "NoStupidQuestions",      # weird everyday questions
    "todayilearned",          # bite-size facts with sources
    "AskScienceFiction",      # playful pseudo-scientific takes
)

_TIMEOUT = 8  # polite, short


def _keywords_from(title: str, limit: int = 5) -> list:
    return _keywords_of(title, limit)


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was",
    "how", "why", "what", "do", "does", "did", "kenapa", "apakah", "itu",
    "yang", "kan", "ke", "di", "dari", "untuk", "ada", "bisa",
})


def _keywords_of(text: str, limit: int) -> list:
    words = [
        word.strip(string.punctuation).casefold()
        for word in (text or "").split()
    ]
    kept = [w for w in words if len(w) >= 3 and w not in _STOPWORDS]
    return kept[:limit]


def _top_post(payload: Dict[str, Any]) -> Dict[str, Any]:
    children = (payload or {}).get("data", {}).get("children") or []
    posts = []
    for child in children:
        data = (child or {}).get("data")
        if not isinstance(data, dict):
            continue
        if data.get("stickied"):
            continue
        title = str(data.get("title") or "").strip()
        if title:
            posts.append(data)
    if not posts:
        raise SourceError("reddit response has no usable posts")
    return _random.choice(posts)


def _compose_body(data: Dict[str, Any]) -> str:
    title = str(data.get("title") or "").strip()
    selftext = str(data.get("selftext") or "").strip()
    subreddit = str(data.get("subreddit") or "").strip()
    if selftext and selftext != "[removed]" and selftext != "[deleted]":
        body = f"{title}\n\n{selftext[:600]}"
    else:
        # The title IS the question; let the orchestrator's opener frame it.
        body = f"Dari r/{subreddit}: {title}"
    return body


class RedditSource:
    """One polite fetch of one random hot post per pick (uniform contract)."""

    def __init__(self, user_agent: str) -> None:
        if not user_agent or "yourname" in user_agent:
            raise ValueError(
                "RedditSource needs a distinctive user agent (see notes)")
        self._headers = {"User-Agent": user_agent}

    def fetch(self) -> Dict[str, Any]:
        subreddit = _random.choice(SUBREDDITS)
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=25"
        try:
            response = requests.get(url, headers=self._headers,
                                    timeout=_TIMEOUT)
            if response.status_code != 200:
                raise SourceError(
                    f"reddit returned HTTP {response.status_code} "
                    f"for r/{subreddit}")
            try:
                response.json()
            except ValueError:
                raise SourceError(
                    f"reddit for r/{subreddit} returned non-JSON body")
            post = _top_post(response.json())
        except requests.RequestException as exc:
            raise SourceError(f"reddit request failed: {exc}") from exc
        except KeyError as exc:
            raise SourceError(f"reddit payload shape unexpected: {exc}") from exc
        title = str(post.get("title") or "").strip()
        return {
            "title": title,
            "body": _compose_body(post),
            "keywords": _keywords_from(title),
            "source_name": "reddit",
            "image_query": " ".join(_keywords_from(title, 3)) or title,
        }
