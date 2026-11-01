"""Behavior tests for the delivery pipeline, exercised through the single
orchestrator seam `decide_and_send.run(...)` with injected ports.

Only true externals are faked: the Telegram API (network), content sources
(network), the clock, and randomness. The state/history store is the real
JSON store pointed at a temporary directory.
"""
import json
from datetime import datetime, timezone

import decide_and_send
import state_store


FIXED_NOW = datetime(2026, 11, 1, 9, 12, 0, tzinfo=timezone.utc)

PLACEHOLDER_CONTENT = {
    "title": "Halo, aku bot fakta random",
    "body": "Halo, aku bot fakta random. Tes delivery.",
    "keywords": ["tes", "delivery"],
}


class FakeTelegram:
    """Stands in for api.telegram.org; records what the pipeline sends."""

    def __init__(self, updates=None):
        self.updates = updates or []
        self.sent = []
        self.offset_received = None
        self.configured = True

    def is_configured(self):
        return self.configured

    def send_message(self, text):
        self.sent.append(text)
        return True

    def get_updates(self, offset=None):
        self.offset_received = offset
        return list(self.updates)


class FakeSource:
    def __init__(self, content=None):
        self.content = dict(content or PLACEHOLDER_CONTENT)
        self.fetch_called = False

    def fetch(self):
        self.fetch_called = True
        return self.content


class FakeClock:
    def __init__(self, now=FIXED_NOW):
        self._now = now

    def now(self):
        return self._now


class FakeRng:
    """Deterministic port over randomness: `roll` is what random() returns."""

    def __init__(self, roll=0.0):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, seq):
        return seq[0]


def build_pipeline(tmp_path, *, telegram=None, source=None, clock=None,
                   rng=None, **run_kwargs):
    store = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    telegram = telegram if telegram is not None else FakeTelegram()
    source = source if source is not None else FakeSource()
    clock = clock if clock is not None else FakeClock()
    rng = rng if rng is not None else FakeRng()
    result = decide_and_send.run(
        telegram=telegram, source=source, clock=clock, rng=rng,
        state=store, **run_kwargs,
    )
    return result, store, telegram, source


def test_force_send_delivers_message_and_persists_state(tmp_path):
    result, harness, telegram, source = build_pipeline(tmp_path, force=True)
    # The composed message reaches the chat: fixed first opener + placeholder body.
    assert telegram.sent == ["Fakta random hari ini: Halo, aku bot fakta random. Tes delivery."]
    assert result.sent is True

    # State records the moment we sent (PRD field shape).
    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_sent_timestamp"] == "2026-11-01T09:12:00Z"

    # Content is remembered in history with the PRD field shape.
    history = json.loads(harness.history_path.read_text(encoding="utf-8"))
    assert history == [{
        "title": "Halo, aku bot fakta random",
        "keywords": ["tes", "delivery"],
        "source": "placeholder",
        "timestamp": "2026-11-01T09:12:00Z",
    }]


def test_cooldown_active_skips_heavy_work_and_sends_nothing(tmp_path):
    # Last send was 31 minutes ago; cooldown is 90 minutes.
    harness_prev = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    harness_prev.save_state({
        "last_sent_timestamp": "2026-11-01T08:41:00Z",  # 31 min before FIXED_NOW
        "last_update_id": None,
    })
    clock = FakeClock(datetime(2026, 11, 1, 9, 12, 0, tzinfo=timezone.utc))

    result, harness, telegram, source = build_pipeline(tmp_path, clock=clock)

    assert result.sent is False
    assert result.reason == "cooldown"
    assert telegram.sent == []
    assert source.fetch_called is False  # heavy work never starts


def test_probability_roll_loss_skips_fetch_and_send(tmp_path):
    # Definite loss: random() returns 0.5 against the default 0.06.
    result, harness, telegram, source = build_pipeline(
        tmp_path, rng=FakeRng(roll=0.5))

    assert result.sent is False
    assert result.reason == "probability-roll"
    assert telegram.sent == []
    assert source.fetch_called is False

