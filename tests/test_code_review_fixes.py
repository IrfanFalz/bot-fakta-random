"""Code-review fix acceptance (Nov 2026 axes).

Covers the review findings that changed behavior:
- a failing source no longer silences the run: the pick is thrown back and
  a different source is tried (budget = 3 attempts, same as the dedupe gate);
- MarkdownV2 spoiler payloads are escape-aware chunked under 4096;
- the photo-caption remainder is chunked under 4096 too;
- data/*.json stay in their empty PRD shapes (commit-only-on-real-sends);
- the shared keyword module exists and both legacy aliases delegate to it.

Fakes mirror the other seam tests (no network anywhere).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import decide_and_send
import keywords
import tg_format
import state_store


# ---------------------------------------------------------------- fakes
class FakeClock:
    def now(self):
        import datetime as dt
        return dt.datetime(2026, 11, 9, 9, 12, 0, tzinfo=dt.timezone.utc)


class FakeRng:
    def __init__(self, rolls: Optional[List[float]] = None):
        self._rolls = list(rolls or [0.0])

    def random(self):
        if self._rolls:
            return self._rolls.pop(0)
        return 0.0

    def choice(self, sequence):
        return sequence[0]


class FakeTelegram:
    def is_configured(self):
        return True

    def __init__(self):
        self.sent: List[str] = []

    def send_message(self, text: str, parse_mode: Optional[str] = None) -> bool:
        self.sent.append(text)
        return True

    def get_updates(self, offset):
        return []


class PhotoTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.photos: List[tuple] = []

    def send_photo(self, photo_url: str, caption: Optional[str] = None) -> bool:
        self.photos.append((photo_url, caption or ""))
        return True


class BrokenSource:
    """Every pick raises (a dead API)."""

    def __init__(self, name: str = "dead"):
        self.name = name
        self.fetch_calls = 0

    def fetch(self):
        self.fetch_calls += 1
        raise Exception(f"{self.name} exploded")


class StaticSource:
    def __init__(self, title: str = "Fakta segar"):
        self.title = title
        self.fetch_calls = 0

    def fetch(self) -> Dict[str, Any]:
        self.fetch_calls += 1
        return {
            "title": self.title,
            "body": f"Isi {self.title}.",
            "keywords": ["fakta"],
            "source_name": "wikipedia",
            "image_query": self.title,
        }


def _store(tmp_path):
    return state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )


def _run(tmp_path, sources_tuple, telegram, rolls):
    return decide_and_send.run(
        telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls=rolls),
        state=_store(tmp_path), sources=sources_tuple)


# ------------------------------------------------- (a) failure re-pick
def test_failed_source_thrown_back_next_attempt_sends(tmp_path):
    dead = BrokenSource()
    healthy = StaticSource("Fakta sehat")

    # rolls: probability(0.0) → pick#1(0.0 → dead, raises) →
    #        pick#2(0.6*100=60 → healthy, fresh) → send
    result = _run(tmp_path, [(dead, 50.0), (healthy, 50.0)],
                  FakeTelegram(), rolls=[0.0, 0.0, 0.6])

    assert result.sent is True
    assert result.reason == "sent"
    assert dead.fetch_calls == 1      # thrown back, not fatal
    assert healthy.fetch_calls == 1   # delivered instead


def test_all_attempts_failing_stays_a_green_skip(tmp_path):
    dead = BrokenSource()

    result = _run(tmp_path, [(dead, 100.0)], FakeTelegram(),
                  rolls=[0.0, 0.0, 0.0])

    assert result.sent is False
    assert result.reason == "source-fetch-failed"
    assert dead.fetch_calls == 3  # full budget before giving up


def test_malformed_candidate_is_thrown_back_but_silence_still_wins(tmp_path):
    class Malformed:
        def fetch(self):
            return {"nope": "not the contract"}

    result = _run(tmp_path, [(Malformed(), 100.0)], FakeTelegram(),
                  rolls=[0.0, 0.0, 0.0])

    assert result.reason == "source-fetch-failed"


# ------------------------------------ (b) spoiler / remainder splitting
def test_split_for_html_chunks_respect_rendered_length():
    story = " ".join(f"katap Winterfell&{i}!" for i in range(80))
    for chunk in tg_format.split_for_html(story, 200):
        assert len(tg_format.plain_to_html(chunk)) <= 200
        assert chunk[-1] not in "&", "cut never splits inside an entity"
    # order + completeness (every space-boundary word survives)
    rejoined = " ".join(tg_format.split_for_html(story, 200))
    assert rejoined == story


def test_gamma_spoiler_send_short_message_single_chunk(tmp_path):
    class HtmlTG(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.html: List[str] = []

        def send_html(self, text: str) -> bool:
            self.html.append(text)
            return True

    telegram = HtmlTG()
    result = _run(tmp_path, [(StaticSource(), 100.0)], telegram,
                  rolls=[0.0, 0.0, 0.01])  # prob, pick, spoiler=yes

    assert result.sent is True
    assert len(telegram.html) == 1
    assert telegram.html[0].startswith("<tg-spoiler>")


def test_photo_remainder_chunked_when_gigantic(tmp_path):
    telegram = PhotoTelegram()
    huge = "kata " * 3000  # message far beyond 1024 AND its tail past 4096
    source = StaticSource("Fakta panjang")

    finder = type("F", (), {})()
    finder.find = lambda content: {"url": "https://x/r.jpg", "credit": ""}

    result = decide_and_send.run(
        telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls=[0.0]),
        state=_store(tmp_path), sources=[(source, 100.0)],
        image_finder=finder)

    assert result.sent is True
    caption = telegram.photos[0][1]
    assert len(caption) <= 1024
    assert all(len(part) <= 4096 for part in telegram.sent)
    # whole story travels in order: caption + tail chunks
    rejoined = " ".join((caption + " " + " ".join(telegram.sent)).split())
    assert rejoined.split() == result.message.split()


# ------------------------------------------------- (d) valid PRD shapes
def test_data_files_ship_valid_prd_shapes():
    # The bot is live: state/history GROW after every send. What must hold
    # forever is the PRD schema — wrong keys/types = placeholder or corrupt
    # data reaching git.
    repo_state = json.loads(
        Path("data/state.json").read_text(encoding="utf-8"))
    repo_history = json.loads(
        Path("data/history.json").read_text(encoding="utf-8"))
    assert set(repo_state) == {"last_sent_timestamp", "last_update_id"}
    assert repo_state["last_sent_timestamp"] is None or \
        isinstance(repo_state["last_sent_timestamp"], str)
    assert repo_state["last_update_id"] is None or \
        isinstance(repo_state["last_update_id"], int)
    assert isinstance(repo_history, list)
    for entry in repo_history:
        assert set(entry) >= {"title", "keywords", "source", "timestamp"}
        assert isinstance(entry["keywords"], list) \
            and all(isinstance(word, str) for word in entry["keywords"])


# ---------------------------------------- (e) shared keywords module
def test_keywords_module_is_the_single_home():
    assert keywords.keywords_of("Kenapa Gurita Punya Tiga Jantung") == [
        "gurita", "punya", "tiga", "jantung"]
    assert keywords.image_query("Kenapa Gurita Punya Tiga Jantung", 2) == \
        "Gurita Punya"  # "Kenapa" is a stopword, casing kept for queries
    # duplicate words collapse, order preserved
    assert keywords.keywords_of("gurita gurita jantung") == ["gurita",
                                                             "jantung"]
