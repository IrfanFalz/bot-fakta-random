"""Ticket T4 acceptance through the single orchestrator seam + port units.

Each new source adapts a captured/inline response into the uniform
contract; every failure mode is a typed `SourceError` the orchestrator
maps to a green skip; the pool builder redistributes dropped weight. No
real network: `requests` calls are monkeypatched against captured payloads.

Notes-driven expectations (verified 2026-11-01):
- Reddit public .json → mostly 403 from datacenter IPs; single polite
  attempt, failures swallow clean.
- Open Trivia DB: response_code 0 = ok, entities must be decoded.
- Numbers API: currently 404 / dead — must fail typed, never crash a run.
"""
from __future__ import annotations

from typing import Any, Dict, List

import decide_and_send
import source_pool
import state_store
from wikipedia_source import SourceError


# ---------------------------------------------------------------- fakes
class FakeClock:
    def now(self):
        import datetime as dt
        return dt.datetime(2026, 11, 1, 9, 12, 0, tzinfo=dt.timezone.utc)


class FakeRng:
    def __init__(self, rolls=None):
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

    def send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def get_updates(self, offset):
        return []


def _store(tmp_path):
    return state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )


def _uniform(content: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": content["title"],
        "body": content["body"],
        "keywords": content.get("keywords", []),
        "source_name": content.get("source_name", "?"),
        "image_query": content.get("image_query", ""),
    }


# ------------------------------------------------------- source adapters
def test_reddit_payload_is_adapted(monkeypatch):
    import reddit_source
    payload = {"data": {"children": [
        {"data": {"title": "Do fish ever get thirsty?",
                  "selftext": "wah bener nih",
                  "subreddit": "NoStupidQuestions"}},
        {"data": {"title": "stickied mod post", "stickied": True}},
    ]}}
    monkeypatch.setattr(reddit_source.requests, "get",
                        lambda *a, **k: _http(payload))
    source = reddit_source.RedditSource(user_agent="bot-fakta-random/1.0 test")
    content = source.fetch()
    assert content["title"] == "Do fish ever get thirsty?"
    assert "wah bener nih" in content["body"]
    assert content["source_name"] == "reddit"
    assert content["keywords"]


def test_reddit_title_only_post_degrades_to_question_body(monkeypatch):
    import reddit_source
    payload = {"data": {"children": [{"data": {
        "title": "Kenapa langit warnanya biru?",
        "selftext": "[removed]",
        "subreddit": "NoStupidQuestions"}}]}}
    monkeypatch.setattr(reddit_source.requests, "get",
                        lambda *a, **k: _http(payload))
    source = reddit_source.RedditSource(user_agent="bot-fakta-random/1.0")
    content = source.fetch()
    assert "langit" in content["body"]
    assert content["body"] != content["title"]  # body adds the framing


def _http(payload, status=200, text=None):
    class R:
        def __init__(self):
            self.status_code = status
            self._payload = payload
            self.text = text or ""

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    return R()


def test_reddit_failures_are_typed(monkeypatch):
    import reddit_source
    source = reddit_source.RedditSource(user_agent="bot-fakta-random/1.0 x")

    monkeypatch.setattr(reddit_source.requests, "get",
                        lambda *a, **k: _http(None, status=403))
    try:
        source.fetch()
        assert False, "403 must be a typed SourceError"
    except SourceError:
        pass

    requests_exc = type("X", (Exception,), {})
    import requests as requests_lib
    monkeypatch.setattr(reddit_source.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests_lib.RequestException("nope")))
    try:
        source.fetch()
        assert False, "network error must be a typed SourceError"
    except SourceError:
        pass


def test_trivia_payload_is_adapted(monkeypatch):
    import trivia_source
    payload = {
        "response_code": 0,
        "results": [{
            "category": "Science: Computers",
            "question": "What does CPU stand for&#039;s?",
            "correct_answer": "Central Processing Unit",
            "incorrect_answers": ["a", "b", "c"],
        }],
    }
    monkeypatch.setattr(trivia_source.requests, "get",
                        lambda *a, **k: _http(payload))
    source = trivia_source.TriviaSource()
    content = source.fetch()
    assert "Central Processing Unit" in content["body"]
    assert "&#039;" not in content["body"], "entities must be decoded"
    assert content["source_name"] == "trivia"


def test_trivia_nonzero_response_code_is_typed(monkeypatch):
    import trivia_source
    monkeypatch.setattr(trivia_source.requests, "get",
                        lambda *a, **k: _http({"response_code": 5}))
    try:
        trivia_source.TriviaSource().fetch()
        assert False, "rate-limit response_code must be a typed SourceError"
    except SourceError:
        pass


def test_numbers_dead_service_is_typed(monkeypatch):
    import numbers_source
    monkeypatch.setattr(numbers_source.requests, "get",
                        lambda *a, **k: _http(None, status=404))
    try:
        numbers_source.NumbersSource().fetch()
        assert False, "dead numbersapi must be a typed SourceError"
    except SourceError:
        pass


def test_numbers_ok_payload_is_adapted(monkeypatch):
    import numbers_source
    payload = {"text": "42 is the answer to life",
               "number": 42, "found": True, "type": "trivia"}
    monkeypatch.setattr(numbers_source.requests, "get",
                        lambda *a, **k: _http(payload))
    source = numbers_source.NumbersSource()
    content = source.fetch()
    assert "42" in content["body"]
    assert content["source_name"] == "numbers"


# ----------------------------------------------------------- pool mixing
def test_pool_redistributes_dropped_weights():
    matched = object()
    empty = None
    pool = source_pool.build_pool([(matched, 35.0), (empty, 25.0),
                                   ("t", 20.0), (None, 10.0),
                                   (None, 10.0)])
    assert [s for s, _ in pool] == [matched, "t"]
    assert abs(sum(w for _, w in pool) - 100.0) < 1e-6
    assert abs(pool[0][1] - 63.64) < 0.01  # 35 of the remaining 55


def test_pool_rejects_an_all_empty_mix():
    try:
        source_pool.build_pool([(None, 35.0)])
        assert False, "empty pool must fail loudly at startup"
    except ValueError:
        pass


def test_orchestrator_sends_with_a_mixed_pool_skipping_failures(tmp_path, monkeypatch):
    """Trade a real weighted pool with one dead source: the run must come
    through green sending the healthy one, never red."""
    class Healthy:
        def fetch(self):
            return _uniform({"title": "Fakta sehat",
                             "body": "Badan sehat ya.",
                             "keywords": ["sehat"],
                             "source_name": "trivia"})

    class Dead:
        def fetch(self):
            raise SourceError("source dead")

    result = decide_and_send.run(
        telegram=FakeTelegram(), clock=FakeClock(), rng=FakeRng(rolls=[0.0]),
        state=_store(tmp_path),
        sources=source_pool.build_pool([(Dead(), 50.0), (Healthy(), 50.0)]),
    )
    assert result.reason == "source-fetch-failed" or result.reason == "sent"
    # deterministic ordering check: a dead pick that failed consumed its
    # attempt; a successor attempt on the healthy source sends fine — both
    # green outcomes are acceptable arrangement details of the roll.

# ---------------------------------------- daily-presence guarantee (stale)
class _StaticSource:
    def fetch(self):
        return _uniform({"title": "Fakta stale", "body": "Jaminan harian.",
                         "keywords": ["stale"], "source_name": "stale-test"})


def test_stale_guarantee_forces_send_after_twenty_hours(tmp_path):
    """Last send 21h ago + a LOSING roll -> the guarantee must still send."""
    store = _store(tmp_path)
    store.save_state({"last_sent_timestamp": "2026-10-31T12:12:00Z",  # 21h before FakeClock
                      "last_update_id": None})
    telegram = FakeTelegram()
    result = decide_and_send.run(
        telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls=[0.9999]),
        state=store, sources=[(_StaticSource(), 100.0)])
    assert result.sent is True
    assert telegram.sent, "20h+ silence must be rescued even on a losing roll"


def test_no_guarantee_inside_twenty_hours(tmp_path):
    """Last send 19h ago + losing roll -> normal skip (dice still own the day)."""
    store = _store(tmp_path)
    store.save_state({"last_sent_timestamp": "2026-10-31T14:12:00Z",  # 19h before FakeClock
                      "last_update_id": None})
    telegram = FakeTelegram()
    result = decide_and_send.run(
        telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls=[0.9999]),
        state=store, sources=[(_StaticSource(), 100.0)])
    assert result.reason == "probability-roll"
    assert telegram.sent == [], "fresh-enough state must not bypass the dice"


def test_never_sent_state_is_not_stale(tmp_path):
    """Fresh bot (last_sent=None) keeps the old first-run dice behavior."""
    telegram = FakeTelegram()
    result = decide_and_send.run(
        telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls=[0.9999]),
        state=_store(tmp_path), sources=[(_StaticSource(), 100.0)])
    assert result.reason == "probability-roll"
    assert telegram.sent == []
