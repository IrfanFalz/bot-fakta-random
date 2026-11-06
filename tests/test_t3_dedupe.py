"""Ticket T3 acceptance through the single orchestrator seam.

The repetition gate keeps recent topics from replaying: near-duplicate
candidates are thrown back (attempt budget 3), a different source is
picked, and after exhausting the budget the message still goes out — the
send never silently disappears. Assertions target externally observable
orchestrator behavior only (sends, fetch counts, persisted history); the
similarity maths itself is covered by the pure-function tests at the end.

No real network anywhere: Telegram and sources are fakes.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import decide_and_send
import dedupe
import state_store

SEED_ENTRY = {
    "title": "Kenapa Gurita Punya Tiga Jantung",
    "keywords": ["gurita", "jantung", "biologi"],
    "source": "wikipedia",
    "timestamp": "2026-10-25T09:00:00Z",
}


# ---------------------------------------------------------------- fakes
class FakeClock:
    """Fixed clock."""

    def __init__(self, now=None):
        from datetime import datetime, timezone
        self._now = now or datetime(2026, 11, 1, 9, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self._now


class FakeRng:
    """Seeded rng: `random()` pops a queue (low roll = probability wins),
    `choice()` pops a queue (openers)."""

    def __init__(self, rolls: Optional[List[float]] = None,
                 choices: Optional[List[str]] = None):
        self._rolls = list(rolls or [0.0])
        self._choices = list(choices or ["Fakta random hari ini:"])

    def random(self):
        if self._rolls:
            return self._rolls.pop(0)
        return 0.0  # queue exhausted: further rolls stay deterministic

    def choice(self, sequence):
        if self._choices:
            return self._choices.pop(0)
        return sequence[0]


class FakeTelegram:
    """Stands in for api.telegram.org."""

    def __init__(self):
        self.sent: List[str] = []

    def is_configured(self):
        return True

    def send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def get_updates(self, offset):
        return []


class RecordingSource:
    """Yields queued candidates in order; counts fetch calls."""

    def __init__(self, name: str, candidates: List[Dict[str, Any]]):
        self.name = name
        self.candidates = list(candidates)
        self.fetch_calls = 0

    def fetch(self) -> Dict[str, Any]:
        self.fetch_calls += 1
        if not self.candidates:
            raise Exception(f"{self.name} exhausted")
        return self.candidates.pop(0)


def _content(title: str, keywords: List[str]) -> Dict[str, Any]:
    return {
        "title": title,
        "body": f"Isi tentang {title}.",
        "keywords": keywords,
        "source_name": "wikipedia",
        "image_query": title,
    }


def _gurita() -> Dict[str, Any]:
    return _content("Kenapa Gurita Punya Tiga Jantung",
                    ["gurita", "jantung", "biologi"])


def _gurita_mirror() -> Dict[str, Any]:
    """Nearly identical to the seeded gurita entry (well past 0.6)."""
    return _content("Kenapa Gurita Punya Tiga Jantung?",
                    ["gurita", "jantung", "biologi"])


def _papaya() -> Dict[str, Any]:
    return _content("Kenapa Papaya Bisa Mematangkan Buah Lain",
                    ["papaya", "etilena", "buah"])


def _run(tmp_path: Path, sources, rolls: Optional[List[float]] = None,
         seed_history: Optional[List[Dict[str, Any]]] = None,
         dedupe_threshold: float = decide_and_send.DEFAULT_DEDUPE_THRESHOLD):
    state = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    if seed_history:
        state.save_history(seed_history)
    telegram = FakeTelegram()
    result = decide_and_send.run(
        telegram=telegram,
        clock=FakeClock(),
        rng=FakeRng(rolls=rolls),
        state=state,
        sources=sources,
        dedupe_threshold=dedupe_threshold,
    )
    return result, telegram


# ---------------------------------------------------------------- tests
def test_near_duplicate_thrown_back_next_source_sends(tmp_path):
    gate = RecordingSource("a", [_gurita_mirror()])
    other = RecordingSource("b", [_papaya()])

    result, telegram = _run(tmp_path, [(gate, 50.0), (other, 50.0)],
                            rolls=[0.0, 0.2, 0.6], seed_history=[SEED_ENTRY])

    assert result.sent is True
    assert result.reason == "sent"
    assert gate.fetch_calls == 1          # candidate thrown back...
    assert other.fetch_calls == 1         # ...next source delivered instead
    assert "Papaya" in telegram.sent[0]
    recorded = json.loads((tmp_path / "history.json").read_text("utf-8"))
    titles = [entry["title"] for entry in recorded]
    assert titles == [SEED_ENTRY["title"], _papaya()["title"]]


def test_all_duplicates_still_sends_after_budget(tmp_path):
    source = RecordingSource("only", [_gurita_mirror(), _gurita_mirror(),
                                      _gurita_mirror()])

    result, telegram = _run(tmp_path, [(source, 100.0)], rolls=[0.0, 0.0, 0.0],
                            seed_history=[SEED_ENTRY])

    assert result.sent is True, "the send must never disappear silently"
    assert result.reason == "sent"
    assert source.fetch_calls == 3        # budget exhausted, candidate sent
    assert "Gurita" in telegram.sent[0]


def test_fresh_candidate_sends_on_first_attempt(tmp_path):
    source = RecordingSource("only", [_gurita()])
    telegram = FakeTelegram()
    state = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    result = decide_and_send.run(
        telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls=[0.0]),
        state=state, sources=[(source, 100.0)],
    )

    assert result.sent is True
    assert source.fetch_calls == 1            # clean history: no retry debt
    assert telegram.sent == [result.message]


def test_history_read_sees_the_capped_50(tmp_path):
    filling = [_content(f"Topik ke-{i}", [f"topik-{i}"]) for i in range(60)]
    seeding = filling + [dict(SEED_ENTRY)]  # 61: the 50-entry SAVE cap keeps
    # only the LAST 50, which still holds the gurita seed entry → gate fires
    source = RecordingSource("only", [_gurita_mirror(), _gurita_mirror(),
                                      _gurita_mirror()])
    result, _ = _run(tmp_path, [(source, 100.0)], rolls=[0.0, 0.0, 0.0],
                     seed_history=seeding)

    assert result.reason == "sent"
    assert source.fetch_calls == 3


# ---------------------------------------------------------------- maths
def test_title_similarity_catches_near_duplicates_above_threshold():
    mirror = dedupe.title_similarity(
        "Kenapa Gurita Punya Tiga Jantung?", SEED_ENTRY["title"])
    compared = dedupe.title_similarity(
        _papaya()["title"], SEED_ENTRY["title"])
    assert mirror > 0.6
    assert compared < 0.6


def test_keyword_overlap_is_a_jaccard():
    full = dedupe.keyword_overlap(["gurita", "jantung", "biologi"],
                                  SEED_ENTRY["keywords"])
    none_ = dedupe.keyword_overlap(["papaya", "etilena"],
                                   SEED_ENTRY["keywords"])
    empty = dedupe.keyword_overlap([], SEED_ENTRY["keywords"])
    assert full == 1.0
    assert none_ == 0.0
    assert empty == 0.0


def test_is_duplicate_respects_the_threshold():
    # at the default threshold the mirror is a duplicate; pushed beyond the
    # clamp it isn't (mirror scores 1.0 only for the exact normalized match)
    assert dedupe.is_duplicate(
        _gurita_mirror(), [SEED_ENTRY], threshold=0.6) is True
    assert dedupe.is_duplicate(
        _mirror_only_upgraded(), [SEED_ENTRY], threshold=0.999) is False
    assert dedupe.is_duplicate(
        _gurita_mirror(), [SEED_ENTRY], threshold=1.1) is True   # clamped 1.0
    assert dedupe.is_duplicate(
        _gurita_mirror(), [SEED_ENTRY], threshold=-2) is True    # clamped 0.0


def _mirror_only_upgraded() -> dict:
    """A candidate near-but-not-at 1.0 similarity to the seed entry — the
    title is suggestive but its keywords are disjoint (score driven by the
    title signal alone)."""
    return _content("Kok Gurita Bisa Punya Tiga Jantung, Sih?",
                    ["gurita", "sifat unik"])


def test_malformed_history_entries_never_crash_the_gate():
    junk: List[Dict[str, Any]] = [{"nope": 1}, {}, _gurita_mirror()]
    assert dedupe.is_duplicate(_gurita_mirror(), junk) is True
    assert dedupe.is_duplicate(_papaya(), [None, "not-a-dict"]) is False
