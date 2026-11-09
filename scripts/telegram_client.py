"""Telegram Bot API client: a thin wrapper over HTTPS with `requests`.

Ports exposed to the orchestrator:
- `is_configured()`  -> whether token/chat id are known
- `send_message(text)`
- `send_photo(photo_url, caption)`  (present for later tickets)
- `get_updates(offset)`             (present for command polling, ticket T7)

The token and chat id come from the environment only, never hardcoded:
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API answers with ok=false or errors out."""


class TelegramNotConfigured(TelegramError):
    """Raised when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing."""


class TelegramClient:
    """Minimal SDK-style port per operation (each mockable in isolation)."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None,
                 timeout: int = 10) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.timeout = timeout

    def is_configured(self) -> bool:
        return bool(self.token) and bool(self.chat_id)

    def send_message(self, text: str, parse_mode: Optional[str] = None) -> bool:
        payload: Dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)

    def send_html(self, text: str) -> bool:
        """HTML send (bold paragraphs + <tg-spoiler>); caller pre-escapes."""
        return self.send_message(text, parse_mode="HTML")

    def send_photo(self, photo_url: str, caption: Optional[str] = None) -> bool:
        payload: Dict[str, Any] = {"chat_id": self.chat_id, "photo": photo_url}
        if caption:
            payload["caption"] = caption
        return self._call("sendPhoto", payload)

    def get_updates(self, offset: Optional[int] = None) -> List[Dict[str, Any]]:
        """Instant poll (timeout=0): returns the pending updates list."""
        payload: Dict[str, Any] = {"timeout": 0}
        if offset is not None:
            payload["offset"] = offset
        updates = self._call("getUpdates", payload)
        return updates if isinstance(updates, list) else []

    def _call(self, method: str, payload: Dict[str, Any]) -> Any:
        if not self.is_configured():
            raise TelegramNotConfigured(
                "Telegram is not configured: TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID env vars are required."
            )
        try:
            response = requests.post(
                _API_BASE.format(token=self.token, method=method),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # network / DNS / timeout
            raise TelegramError(f"{method} request failed: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON: {response.text!r}") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body}")
        return body.get("result")
