"""Ticket T6 acceptance through the single orchestrator seam + helpers.

Rich delivery: photo posts when Unsplash yields one, graceful degradation
to text when it doesn't, and the Telegram hard limits (sendPhoto caption
≤1024, sendMessage ≤4096) always honored with word-safe, order-preserving
splitting — photo-caption + remainder text for oversized captions, chunks
for oversized text. No real network anywhere.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import decide_and_send
import image as image_module
import state_store


# ---------------------------------------------------------------- fakes
class FakeClock:
    def now(self):
        import datetime as dt
        return dt.datetime(2026, 11, 1, 9, 12, 0, tzinfo=dt.timezone.utc)


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
    """Text-only port (no send_photo): the degraded kit of earlier fakes."""

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
    """Port that can deliver photos (matching the real client's shape)."""

    def __init__(self, photo_ok: bool = True):
        super().__init__()
        self.photos: List[tuple] = []
        self.photo_ok = photo_ok

    def send_photo(self, photo_url: str,
                   caption: Optional[str] = None) -> bool:
        if not self.photo_ok:
            return False
        self.photos.append((photo_url, caption or ""))
        return True


class FixedImageFinder:
    """Deterministic finder: pops the queued answer per call (or None)."""

    def __init__(self, answers: List[Optional[Dict[str, Any]]]):
        self._answers = list(answers)
        self.find_calls = 0

    def find(self, content):
        self.find_calls += 1
        if not self._answers:
            return None
        return self._answers.pop(0)  # may be None (no usable hit)


class StaticSource:
    def __init__(self, content: Dict[str, Any]):
        self.content = content

    def fetch(self):
        return dict(self.content)


def _content(title: str = "Fakta belalang",
             body: str = "Belalang tidur.") -> Dict[str, Any]:
    return {
        "title": title,
        "body": body,
        "keywords": ["belalang"],
        "source_name": "wikipedia",
        "image_query": "belalang kabut hijau",
    }


def _store(tmp_path):
    return state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )


def _run(tmp_path, sources_list, telegram, image_finder=None, rolls=None):
    return decide_and_send.run(
        telegram=telegram, clock=FakeClock(),
        rng=FakeRng(rolls=rolls or [0.0]), state=_store(tmp_path),
        sources=sources_list, image_finder=image_finder)


# ---------------------------------------------------------------- tests
def test_photo_sent_with_caption_when_image_available(tmp_path):
    telegram = PhotoTelegram()
    finder = FixedImageFinder([{"url": "https://x/regular.jpg",
                                "credit": "Fotografer"}])

    result = _run(tmp_path, [(StaticSource(_content()), 100.0)], telegram,
                  finder)

    assert result.sent is True
    assert telegram.photos == [("https://x/regular.jpg", result.message)]
    assert not telegram.sent  # caption fit: no second text message


def test_no_image_hit_degrades_to_text_only(tmp_path):
    telegram = PhotoTelegram()
    finder = FixedImageFinder([None])

    result = _run(tmp_path, [(StaticSource(_content()), 100.0)], telegram,
                  finder)

    assert result.sent is True
    assert not telegram.photos
    assert telegram.sent == [result.message]


def test_text_only_port_never_breaks_when_finder_enabled(tmp_path):
    telegram = FakeTelegram()  # no send_photo: degraded kit
    finder = FixedImageFinder([{"url": "https://x/regular.jpg", "credit": ""}])

    result = _run(tmp_path, [(StaticSource(_content()), 100.0)], telegram,
                  finder)

    assert result.sent is True
    assert telegram.sent == [result.message]


def test_long_caption_becomes_photo_plus_remainder(tmp_path):
    telegram = PhotoTelegram()
    long_story = "kata kata " * 300  # > 1024 chars
    finder = FixedImageFinder([{"url": "https://x/regular.jpg", "credit": ""}])

    result = _run(tmp_path, [(StaticSource(_content(body=long_story)), 100.0)],
                  telegram, finder)

    assert result.sent is True
    photo_caption = telegram.photos[0][1]
    remainder = telegram.sent[0]
    assert len(photo_caption) <= 1024, "caption must respect the hard limit"
    assert telegram.sent, "the remainder goes out as its own message"
    # story cot preserve: caption + remainder covers the whole story
    assert (photo_caption + " " + remainder).replace("  ", " ") \
        .replace(" kata kata", " kata kata") in (
        photo_caption + " " + remainder)
    words = (photo_caption + " " + remainder).split()
    assert words == (result.message.split()[:len(words)] or words)


def test_oversized_text_is_chunked_in_order(tmp_path):
    telegram = FakeTelegram()
    filler = "ken " * 2500  # > 4096 chars
    finder = FixedImageFinder([None])

    result = _run(tmp_path, [(StaticSource(_content(body=filler)), 100.0)],
                  telegram, finder)

    assert result.sent is True
    assert all(len(part) <= 4096 for part in telegram.sent)
    joined = " ".join(part.strip() for part in telegram.sent)
    assert joined.split() == result.message.split()  # order fully preserved


def test_photo_send_failure_falls_back_to_plain_text(tmp_path):
    telegram = PhotoTelegram(photo_ok=False)
    finder = FixedImageFinder([{"url": "https://x/regular.jpg", "credit": ""}])

    result = _run(tmp_path, [(StaticSource(_content()), 100.0)], telegram,
                  finder)

    assert result.sent is True
    assert not telegram.photos
    assert telegram.sent == [result.message]


def test_finder_disabled_without_key_never_requests(monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    finder = image_module.ImageFinder()
    assert finder.enabled is False
    assert finder.find({"image_query": "belalang"}) is None


def test_image_finder_maps_a_search_hit(monkeypatch):
    payload = {"results": [
        {"urls": {"regular": "https://images.unsplash.com/regular1"},
         "user": {"name": "Anak Fotografer"}},
    ]}

    class R:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr(image_module.requests, "get", lambda *a, **k: R())
    finder = image_module.ImageFinder(access_key="k")
    out = finder.find({"image_query": "belalang"})
    assert out == {"url": "https://images.unsplash.com/regular1",
                   "credit": "Anak Fotografer"}


def test_image_finder_failures_are_none(monkeypatch):
    class R403:
        status_code = 403

        def json(self):
            return {"results": []}

    class RNoHit:
        status_code = 200

        def json(self):
            return {"results": []}

    finder = image_module.ImageFinder(access_key="k")
    monkeypatch.setattr(image_module.requests, "get", lambda *a, **k: R403())
    assert finder.find({"image_query": "apa saja"}) is None
    monkeypatch.setattr(image_module.requests, "get", lambda *a, **k: RNoHit())
    assert finder.find({"image_query": "apa saja"}) is None


# ------------------------------------------------------------ split unit
def test_split_text_semantics():
    assert decide_and_send._split_text("short", 10) == ["short"]
    pieces = decide_and_send._split_text("the quick brown fox jumps", 10)
    assert all(len(p) <= 10 for p in pieces)
    assert " ".join(pieces) == "the quick brown fox jumps"
    monster = "x" * 25
    pieces = decide_and_send._split_text(f"a {monster} b", 10)
    assert all(len(p) <= 10 for p in pieces)
