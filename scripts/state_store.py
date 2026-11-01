"""Persistence of the repo-as-database JSON files (PRD section 6).

Field shapes (exactly as in the PRD, missing keys default to null):

    data/state.json    {"last_sent_timestamp": "2026-11-01T09:12:00Z",
                        "last_update_id": 123456789}
    data/history.json  [{"title": ..., "keywords": [...], "source": ...,
                         "timestamp": "2026-11-01T09:12:00Z"}, ...]

`JsonStateStore` is the port the orchestrator uses; pass explicit paths for
tests or a different working copy. History is capped at the last 50 entries.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_HISTORY_ENTRIES = 50

_EMPTY_STATE: Dict[str, Any] = {"last_sent_timestamp": None, "last_update_id": None}


def default_state() -> Dict[str, Any]:
    return json.loads(json.dumps(_EMPTY_STATE))  # fresh copy


def datetime_to_iso(moment: datetime) -> str:
    """Format a datetime as the PRD's UTC ISO-8601 'Z' timestamp."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class JsonStateStore:
    """Load/save of `data/state.json` and `data/history.json` (JSON files)."""

    def __init__(self, state_path: Optional[Path] = None,
                 history_path: Optional[Path] = None) -> None:
        data_dir = Path(__file__).resolve().parent.parent / "data"
        self.state_path = Path(state_path or data_dir / "state.json")
        self.history_path = Path(history_path or data_dir / "history.json")

    # -- state ------------------------------------------------------

    def load_state(self) -> Dict[str, Any]:
        state = self._load_json(self.state_path)
        if not isinstance(state, dict):
            return default_state()
        # Normalize to the PRD field shape.
        return {
            "last_sent_timestamp": state.get("last_sent_timestamp"),
            "last_update_id": state.get("last_update_id"),
        }

    def save_state(self, state: Dict[str, Any]) -> None:
        self._save_json(self.state_path, state)

    # -- history ----------------------------------------------------

    def load_history(self) -> List[Dict[str, Any]]:
        history = self._load_json(self.history_path)
        return history if isinstance(history, list) else []

    def save_history(self, history: List[Dict[str, Any]]) -> None:
        self._save_json(self.history_path, history[-MAX_HISTORY_ENTRIES:])

    # -- internals --------------------------------------------------

    @staticmethod
    def _load_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
