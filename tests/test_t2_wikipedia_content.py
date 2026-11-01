"""Ticket T2 tests: unpredictable real content via the Wikipedia source.

All behavior is exercised through the single orchestrator seam
`decide_and_send.run(...)`; the only external touchpoints (HTTP to
Wikipedia, HTTP to Telegram) are faked with in-memory stand-ins — the test
suite never touches the real network.
"""
import json
from datetime import datetime, timezone

import pytest
import requests

import decide_and_send
import state_store
import wikipedia_source
from wikipedia_source import WikipediaSource, SourceError

FIXED_NOW = datetime(2026, 11, 1, 9, 12, 0, tzinfo=timezone.utc)

# A captured (trimmed) random-summary response from
# https://en.wikipedia.org/api/rest_v1/page/random/summary — verified shape.
CAPTURED_SUMMARY = {
    "type": "standard",
    "title": "Octopus Centrality Mixer Benchmark",
    "displaytitle": "Octopus Centrality Mixer Benchmark",
    "description": "2011 publication",
    "extract": "Octopus Centrality Mixer Benchmark is a 2011 study about "
               "vertebrate physiology and network theory.",
    "pageid": 73162214,
    "lang": "en",
    "thumbnail": {"source": "https://upload.wikimedia.org/…/60px-test.png",
                  "width": 60, "height": 60},
    "originalimage": {"source": "https://upload.wikimedia.org/…/test.png",
                      "width": 1200, "height": 900},
    "content_urls": {"desktop": {
        "page": "https://en.wikipedia.org/wiki/Octopus_centrality…"}},
    "meta": {"dt": "2026-11-01T06:00:00Z"},
}


class FakeResponse:
    """Stands in for a `requests` response object."""

    def __init__(self, status=200, payload=None, content_type="application/json"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self._payload = payload

    def json(self):
        return self._payload


class FakeClock:
    def __init__(self, now=FIXED_NOW):
        self._now = now

    def now(self):
        return self._now


class FakeRng:
    """random() = `roll` for both the probability roll and the source pick."""

    def __init__(self, roll=0.0):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, seq):
        return seq[0]


class RecordingTelegram:
    """Stands in for api.telegram.org; records what the pipeline sends."""

    def __init__(self):
        self.sent = []

    def is_configured(self):
        return True

    def send_message(self, text):
        self.sent.append(text)
        return True

    def get_updates(self, offset=None):
        return []


def patch_wikipedia(monkeypatch, response=None, exc=None):
    """Point `requests.get` at an inline fake (payload) or a raised error."""
    def fake_get(*args, **kwargs):
        if exc is not None:
            raise exc
        return response
    monkeypatch.setattr(wikipedia_source.requests, "get", fake_get)


def build(tmp_path, *, source, rng=None, telegram=None, **run_kwargs):
    store = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    telegram = telegram if telegram is not None else RecordingTelegram()
    result = decide_and_send.run(
        telegram=telegram, source=source, clock=FakeClock(),
        rng=rng if rng is not None else FakeRng(), state=store, **run_kwargs,
    )
    return result, store, telegram


# -- Wikipedia adapter, through the full pipeline --------------------------

def test_wikipedia_summary_flows_to_send_as_uniform_content(tmp_path, monkeypatch):
    # The REAL WikipediaSource adapts the captured response; only the network
    # call is faked, everything else (orchestrator, formatting, history) is
    # the production code path.
    patch_wikipedia(monkeypatch, response=FakeResponse(payload=CAPTURED_SUMMARY))
    telegram = RecordingTelegram()

    result, store, _ = build(
        tmp_path, source=WikipediaSource(),
        telegram=telegram,
        rng=FakeRng(roll=0.0),  # probability roll passes (roll < 0.06)
    )

    assert result.sent is True
    assert result.reason == "sent"
    message = telegram.sent[0]
    assert message.startswith(decide_and_send.OPENERS[0])  # casual opener
    assert CAPTURED_SUMMARY["extract"] in message  # the body essence arrives

    # History records title + keywords + source + timestamp (PRD 6 shape).
    history = json.loads(store.history_path.read_text(encoding="utf-8"))
    assert history == [{
        "title": "Octopus Centrality Mixer Benchmark",
        "keywords": ["octopus", "centrality", "mixer", "benchmark"],
        "source": "wikipedia",
        "timestamp": "2026-11-01T09:12:00Z",
    }]


def test_wikipedia_adaptation_matches_uniform_contract(tmp_path, monkeypatch):
    patch_wikipedia(monkeypatch, response=FakeResponse(payload=CAPTURED_SUMMARY))

    content = WikipediaSource().fetch()

    assert set(content) == {
        "title", "body", "keywords", "source_name", "image_query"}
    assert content["title"] == "Octopus Centrality Mixer Benchmark"
    assert content["body"] == CAPTURED_SUMMARY["extract"]
    assert content["source_name"] == "wikipedia"
    # Stopwords are gone; remaining title words survive in order.
    assert content["keywords"] == ["octopus", "centrality", "mixer", "benchmark"]
    assert content["image_query"] == "Octopus Centrality Mixer Benchmark"


# -- Weighted candidate list -----------------------------------------------

def test_single_weighted_entry_is_always_picked(tmp_path):
    class OnlySource:
        calls = 0

        def fetch(self):
            OnlySource.calls += 1
            return {"title": "T", "body": "only-source body", "keywords": [],
                    "source_name": "fake"}

    result, store, _ = build(
        tmp_path, source=OnlySource(), rng=FakeRng(roll=0.3),  # > 0.06; forced
        force=True, sources=[(OnlySource(), 100.0)],
    )
    assert result.sent is True
    assert OnlySource.calls == 1


def test_weighted_pick_follows_the_roll(tmp_path):
    class NamedSource:
        def __init__(self, body):
            self.body = body

        def fetch(self):
            return {"title": "T", "body": self.body, "keywords": [],
                    "source_name": "fake"}

    low = NamedSource("low-weight body")
    high = NamedSource("high-weight body")
    result, store, _ = build(
        # roll 0.5 * total weight 10 = 5 -> after the weight-1 candidate.
        tmp_path, source=low, rng=FakeRng(roll=0.5), force=True,
        sources=[(low, 1.0), (high, 9.0)],
    )
    assert result.sent is True
    assert "high-weight body" in result.message


# -- Source failure is always a clean skip ---------------------------------

def test_wikipedia_non_200_is_a_typed_clean_failure(tmp_path, monkeypatch):
    patch_wikipedia(monkeypatch, response=FakeResponse(status=503))

    result, store, _ = build(
        tmp_path, source=WikipediaSource(), rng=FakeRng(roll=0.0))

    assert result.sent is False
    assert result.reason == "source-fetch-failed"
    # No send, no state write, no history append: a byte-level no-op run.
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "history.json").exists()


def test_wikipedia_timeout_is_swallowed(tmp_path, monkeypatch):
    patch_wikipedia(monkeypatch, exc=requests.Timeout("boom"))

    result, store, _ = build(
        tmp_path, source=WikipediaSource(), rng=FakeRng(roll=0.0))

    assert result.sent is False
    assert result.reason == "source-fetch-failed"


def test_wikipedia_missing_fields_are_swallowed(tmp_path, monkeypatch):
    # Shape without extract/description must not crash the run.
    patch_wikipedia(monkeypatch, response=FakeResponse(
        payload={"title": "", "type": "standard"}))

    result, store, _ = build(
        tmp_path, source=WikipediaSource(), rng=FakeRng(roll=0.0))

    assert result.sent is False
    assert result.reason == "source-fetch-failed"


def test_wikipedia_non_json_gateway_page_is_swallowed(tmp_path, monkeypatch):
    patch_wikipedia(monkeypatch, response=FakeResponse(
        payload="<!doctype html>…", content_type="text/html"))

    result, store, _ = build(
        tmp_path, source=WikipediaSource(), rng=FakeRng(roll=0.0))

    assert result.sent is False
    assert result.reason == "source-fetch-failed"


def test_source_returning_none_is_swallowed(tmp_path):
    class NoneSource:
        def fetch(self):
            return None

    result, store, _ = build(
        tmp_path, source=NoneSource(), rng=FakeRng(roll=0.0))

    assert result.sent is False
    assert result.reason == "source-fetch-failed"
    assert not (tmp_path / "state.json").exists()


# -- Typed error contract of the adapter itself ----------------------------

def test_non_200_raises_typed_source_error(monkeypatch):
    patch_wikipedia(monkeypatch, response=FakeResponse(status=404))
    with pytest.raises(SourceError):
        WikipediaSource().fetch()


def test_timeout_raises_typed_source_error(monkeypatch):
    patch_wikipedia(monkeypatch, exc=requests.Timeout("slow"))
    with pytest.raises(SourceError):
        WikipediaSource().fetch()


# -- Cooldown / probability behavior still holds (unchanged from T1) -------

def test_cooldown_still_gates_real_source(tmp_path):
    store = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    store.save_state({"last_sent_timestamp": "2026-11-01T08:41:00Z",
                      "last_update_id": None})

    class SpySource:
        called = False

        def fetch(self):
            SpySource.called = True
            return {"title": "T", "body": "b", "keywords": [],
                    "source_name": "spy"}

    result = decide_and_send.run(
        telegram=RecordingTelegram(), source=SpySource(), clock=FakeClock(),
        rng=FakeRng(), state=store,
    )
    assert result.reason == "cooldown"
    assert SpySource.called is False  # fetch never starts inside cooldown
