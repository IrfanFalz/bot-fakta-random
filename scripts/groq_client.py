"""Groq chat-completions client (ticket T5 voice).

OpenAI-compatible endpoint per research notes (verified 2026-11-01):
`POST https://api.groq.com/openai/v1/chat/completions`,
`Authorization: Bearer <GROQ_API_KEY>`.

Free-plan reality: 30 RPM / 1,000 RPD on `openai/gpt-oss-120b` — plenty for
~a dozen sends a day; 429 (`retry_after` only present on 429), 401 (bad
key), 400 (model id gone), and 413/TPM blowups all surface as the typed
`GroqError` so callers fall back to raw source text. No SDK; stdlib +
requests. Key lives only in the `GROQ_API_KEY` env (GitHub Secrets in CI).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
_TIMEOUT = 30  # generous: reasoning models can stream slowly to completion


class GroqError(RuntimeError):
    """Any Groq failure: rate limit, bad key, dead model, network flake."""


def _extract_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise GroqError("groq response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise GroqError("groq returned an empty completion")
    return content


class GroqClient:
    """Minimal chat-completions port (single method, single call)."""

    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None) -> None:
        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self._model = model or os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
        if not self._api_key:
            raise GroqError("GROQ_API_KEY is not configured")

    @property
    def model(self) -> str:
        return self._model

    def chat(self, system_prompt: str, user_prompt: str,
             max_tokens: int = 900, temperature: float = 0.8) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            # gpt-oss is a reasoning model; cut latency token use per notes.
            "reasoning_effort": "low",
        }
        try:
            response = requests.post(
                ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=_TIMEOUT,
            )
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after", "unknown")
                raise GroqError(f"groq rate limited (retry after {retry_after}s)")
            if response.status_code != 200:
                raise GroqError(
                    f"groq returned HTTP {response.status_code}: "
                    f"{response.text[:200]}")
            payload = response.json()
        except requests.RequestException as exc:
            raise GroqError(f"groq request failed: {exc}") from exc
        try:
            jsonpayload = response.json()
        except ValueError as exc:
            raise GroqError(f"groq returned non-JSON body: {exc}") from exc
        return _extract_content(jsonpayload)
