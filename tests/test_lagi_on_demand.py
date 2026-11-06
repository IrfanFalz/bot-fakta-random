"""Behavior tests for the T7 on-demand /lagi paths, exercised through the
single orchestrator seam `decide_and_send.run(...)` with injected ports.

Two on-demand paths:
- dispatch trigger (`trigger="lagi"`: what the Cloudflare webhook worker's
  repository_dispatch run uses) — forces a send, never polls updates;
- the scheduled poll fallback — reads pending updates with the stored offset
  (offset = last_update_id + 1 per the Bot API rule) and handles the /lagi
  commands found there, always advancing the offset past everything returned
  so updates are never processed twice.

Documented bounded choice: one send per pending /lagi command, at most
`decide_and_send.MAX_LAGI_PER_RUN` per run (extras are swallowed with the
offset still advanced).

No real network anywhere: Telegram and sources are fakes.
"""
import json

import decide_and_send
import state_store


PLACEHOLDER_CONTENT = {
    "title": "Halo, aku bot fakta random",
    "body": "Halo, aku bot fakta random. Tes delivery.",
    "keywords": ["tes", "delivery"],
    # Uniform contract (T2): the field is `source_name`.
    "source_name": "placeholder",
}


class FakeClock:
    """Fixed clock; `now()` is overridden per test where needed."""

    def __init__(self, now=None):
        from datetime import datetime, timezone
        self._now = now or datetime(2026, 11, 1, 9, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self._now


class FakeTelegram:
    """Stands in for api.telegram.org with a pending-updates queue."""

    def __init__(self, updates=None, send_error=None):
        self.updates = updates or []
        self.sent = []
        self.offset_received = None
        self.get_updates_calls = 0
        self.send_error = send_error

    def is_configured(self):
        return True

    def send_message(self, text):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)
        return True

    def get_updates(self, offset=None):
        self.get_updates_calls += 1
        self.offset_received = offset
        return list(self.updates)


class FakeSource:
    def __init__(self, content=None, fetch_error=None):
        self.content = dict(content or PLACEHOLDER_CONTENT)
        self.fetch_error = fetch_error
        self.fetch_calls = 0
        self.fetch_called = False

    def fetch(self):
        self.fetch_calls += 1
        self.fetch_called = True
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.content


class FakeRng:
    def __init__(self, roll=0.0):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, seq):
        return seq[0]


def build_pipeline(tmp_path, *, updates=None, send_error=None, fetch_error=None,
                   roll=0.99, state_now=None, **run_kwargs):
    """Run one orchestrator execution against fakes and the real JSON store.

    Defaults stack the cards against a normal send (cooldown usually active
    is set explicitly by tests; the default roll 0.99 loses any probability
    roll), so a real send only happens when a bypass is genuinely at play.
    """
    from datetime import datetime, timezone

    before = state_now or datetime(2026, 11, 1, 9, 7, 0, tzinfo=timezone.utc)
    harness = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    harness.save_state({
        "last_sent_timestamp": decide_and_send.datetime_to_iso(before),
        "last_update_id": None,
    })
    harness.save_history([])

    telegram = FakeTelegram(updates=updates, send_error=send_error)
    source = FakeSource(fetch_error=fetch_error)
    clock = FakeClock()
    rng = FakeRng(roll=roll)

    result = decide_and_send.run(
        telegram=telegram, source=source, clock=clock, rng=rng, state=harness,
        **run_kwargs,
    )
    return result, harness, telegram, source


def lagi_update(update_id, text="/lagi"):
    return {"update_id": update_id, "message": {"text": text}}


# -- dispatch-triggered path (webhook worker -> repository_dispatch) ---------


def test_dispatch_trigger_bypasses_cooldown_and_probability(tmp_path):
    # Last send 5 minutes ago (cooldown very much active) + guaranteed
    # probability loss; a trigger="lagi" run must still send.
    result, harness, telegram, source = build_pipeline(tmp_path, trigger="lagi")

    assert result.sent is True
    assert result.reason == "sent"
    assert telegram.sent == [
        "Fakta random hari ini: Halo, aku bot fakta random. Tes delivery."
    ]

    # Same pipeline: history + state recorded exactly like a normal send,
    # with source_name recorded from the chosen source.
    history = json.loads(harness.history_path.read_text(encoding="utf-8"))
    assert history == [{
        "title": "Halo, aku bot fakta random",
        "keywords": ["tes", "delivery"],
        "source": "placeholder",
        "timestamp": "2026-11-01T09:12:00Z",
    }]
    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_sent_timestamp"] == "2026-11-01T09:12:00Z"


def test_dispatch_trigger_never_calls_get_updates(tmp_path):
    # getUpdates is the scheduled fallback's job; dispatch runs must not poll
    # (and while a webhook is set, getUpdates errors anyway).
    result, harness, telegram, source = build_pipeline(tmp_path, trigger="lagi")

    assert result.sent is True
    assert telegram.get_updates_calls == 0
    assert telegram.offset_received is None


# -- scheduled poll fallback ------------------------------------------------


def test_poll_fallback_handles_lagi_bypassing_cooldown_and_probability(tmp_path):
    # Cooldown active (5 min ago) + guaranteed probability loss: the /lagi
    # pending update must still get answered through the same pipeline.
    updates = [lagi_update(1001)]
    result, harness, telegram, source = build_pipeline(
        tmp_path, updates=updates, roll=0.99)

    assert result.sent is True
    assert result.reason == "sent"
    assert telegram.sent == [
        "Fakta random hari ini: Halo, aku bot fakta random. Tes delivery."
    ]
    assert source.fetch_called is True

    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_sent_timestamp"] == "2026-11-01T09:12:00Z"


def test_poll_fallback_asks_telegram_with_stored_offset(tmp_path):
    # The fallback must use one cheap getUpdates call carrying the stored
    # offset. Repo convention (since T1): state.last_update_id already stores
    # the NEXT poll offset (= last received update_id + 1, the Bot API rule
    # from the notes).
    store = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    store.save_state({"last_sent_timestamp": None, "last_update_id": 44})
    telegram = FakeTelegram(updates=[])
    result = decide_and_send.run(
        telegram=telegram, source=FakeSource(), clock=FakeClock(),
        rng=FakeRng(roll=0.99), state=store,
    )

    assert telegram.get_updates_calls == 1
    assert telegram.offset_received == 44
    assert result.reason == "probability-roll"  # pending updates were swallowed


def test_poll_fallback_advances_offset_even_when_send_fails(tmp_path):
    # Send raises: the run bails green but the update is consumed — the
    # advanced offset must be persisted so nothing is processed twice.
    updates = [lagi_update(2001)]
    result, harness, telegram, source = build_pipeline(
        tmp_path, updates=updates, send_error=RuntimeError("telegram down"))

    assert telegram.sent == []
    assert result.sent is False
    assert result.reason == "telegram-send-failed"
    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_update_id"] == 2002  # advanced despite the failure

    # …and the next run picks up exactly where this one left off.
    telegram_next = FakeTelegram(updates=[])
    store_next = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    decide_and_send.run(
        telegram=telegram_next, source=FakeSource(), clock=FakeClock(),
        rng=FakeRng(roll=0.99), state=store_next,
    )
    assert telegram_next.offset_received == 2002


def test_poll_fallback_advances_offset_even_when_source_crashes(tmp_path):
    # Any pipeline failure on the on-demand path keeps the run green and the
    # offset stored.
    result, harness, telegram, source = build_pipeline(
        tmp_path, updates=[lagi_update(3001)],
        fetch_error=RuntimeError("wikipedia exploded"))

    assert result.sent is False
    assert result.reason == "on-demand-pipeline-failed"
    assert telegram.sent == []
    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_update_id"] == 3002


def test_poll_fallback_swallows_non_command_updates(tmp_path):
    # Normal chatter must not trigger anything; the offset still advances so
    # the chat isn't re-read forever.
    updates = [lagi_update(4001, "halo bro"),
               lagi_update(4002, "tolong /lagi dong")]  # not a text prefix
    result, harness, telegram, source = build_pipeline(
        tmp_path, updates=updates, roll=0.99)

    assert result.sent is False
    assert result.reason == "cooldown"  # cooldown was seeded: decision ran normally
    assert telegram.sent == []
    assert source.fetch_called is False
    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_update_id"] == 4003


def test_multiple_pending_lagi_commands_are_bounded_per_run(tmp_path):
    # Documented choice: one send per /lagi command, capped at
    # MAX_LAGI_PER_RUN per run; extras are swallowed with the offset advanced.
    updates = [lagi_update(i) for i in range(10, 15)]  # five /lagi commands
    result, harness, telegram, source = build_pipeline(
        tmp_path, updates=updates)

    assert result.sent is True
    assert len(telegram.sent) == decide_and_send.MAX_LAGI_PER_RUN
    assert telegram.sent == [telegram.sent[0]] * decide_and_send.MAX_LAGI_PER_RUN

    # History keeps every actually-sent entry with its source name.
    history = json.loads(harness.history_path.read_text(encoding="utf-8"))
    assert len(history) == decide_and_send.MAX_LAGI_PER_RUN
    assert all(entry["source"] == "placeholder" for entry in history)

    # Offset advanced past ALL five updates (never processed twice).
    raw_state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert raw_state["last_update_id"] == 15


def test_poll_fallback_get_updates_failure_stays_green(tmp_path):
    # A flaky Telegram network must not fail the scheduled run; the stored
    # offset simply stays put and the normal decision continues.
    class FlakyTelegram(FakeTelegram):
        def get_updates(self, offset=None):
            self.get_updates_calls += 1
            raise RuntimeError("network blip")

    store = state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )
    store.save_state({"last_sent_timestamp": None, "last_update_id": None})
    telegram = FlakyTelegram()
    result = decide_and_send.run(
        telegram=telegram, source=FakeSource(), clock=FakeClock(),
        rng=FakeRng(roll=0.99), state=store,
    )

    assert result.sent is False
    assert result.reason == "probability-roll"  # decision continued normally
    assert telegram.get_updates_calls == 1
