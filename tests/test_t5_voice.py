"""Ticket T5 acceptance through the single orchestrator seam.

The voice pass and the AI-generated slot are asserted on externally
observable behavior: which text the fake telegram port received, which
parse mode, the fallback on Groq failures, opener rotation, and the 10%
spoiler injection (deterministic via injected rng). No real network: the
Groq transport is a fake object with the same `chat()` shape, NOT an http
monkeypatch — the GroqClient's own HTTP layer stays untested (T8 scope).

Voice semantics (per spec): polish used when wired & healthy; degrades to
raw on ANY failure; missing key = polish & AI slot absent entirely.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import decide_and_send
import ai_generated_source
import polish as polish_module
import source_pool
import state_store
import tg_format
from wikipedia_source import SourceError


# ---------------------------------------------------------------- fakes
class FakeClock:
    def now(self):
        import datetime as dt
        return dt.datetime(2026, 11, 1, 9, 12, 0, tzinfo=dt.timezone.utc)


class FakeRng:
    def __init__(self, rolls: Optional[List[float]] = None,
                 choices: Optional[List[str]] = None):
        self._rolls = list(rolls or [0.0])
        self._choices = list(choices or [])

    def random(self):
        if self._rolls:
            return self._rolls.pop(0)
        return 0.0

    def choice(self, sequence):
        if self._choices:
            return self._choices.pop(0)
        return sequence[0]


class FakeTelegram:
    """Plain-text-only port (no send_markdown_v2): fakes from T1..T4."""
    def is_configured(self):
        return True

    def __init__(self):
        self.sent: List[str] = []

    def send_message(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def get_updates(self, offset):
        return []


class MarkdownTelegram(FakeTelegram):
    """Port that CAN render MarkdownV2 (spoiler-capable)."""

    def __init__(self):
        super().__init__()
        self.sent_markdown: List[str] = []

    def send_markdown_v2(self, text: str) -> bool:
        self.sent_markdown.append(text)
        return True


class FakeGroq:
    """Same `chat()` shape as GroqClient, no HTTP."""

    def __init__(self, replies: Optional[List[str]] = None,
                 failure: Optional[Exception] = None):
        self._replies = list(replies or [])
        self._failure = failure
        self.calls: List[Dict[str, Any]] = []

    def chat(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        self.calls.append({"system": system_prompt, "user": user_prompt,
                           **kwargs})
        if self._failure is not None:
            raise self._failure
        return self._replies.pop(0) if self._replies else "polished."


def _content(title="Fakta serangga", body="Semut punya dua perut.",
             source_name="wikipedia"):
    return {"title": title, "body": body,
            "keywords": ["semut"], "source_name": source_name,
            "image_query": title}


def _store(tmp_path):
    return state_store.JsonStateStore(
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.json",
    )


def _run(tmp_path, sources, telegram=None, rng=None, polisher=None):
    return decide_and_send.run(
        telegram=telegram or FakeTelegram(), clock=FakeClock(),
        rng=rng or FakeRng(), state=_store(tmp_path),
        sources=sources, polisher=polisher)


class StaticSource:
    """Always returns the same content (like a healthy Wikipedia call)."""

    def __init__(self, content: Optional[Dict[str, Any]] = None):
        self._content = content or _content()
        self.fetch_calls = 0

    def fetch(self) -> Dict[str, Any]:
        self.fetch_calls += 1
        return dict(self._content)


# ---------------------------------------------------------------- tests
def test_polish_pass_rewrites_the_body_when_healthy(tmp_path):
    groq = FakeGroq(replies=["Versi santai dari semut dua perut, bro."])
    polisher = polish_module.Polisher(groq)
    telegram = FakeTelegram()

    result = _run(tmp_path, [(StaticSource(), 100.0)], telegram=telegram,
                  polisher=polisher)

    assert result.sent is True
    assert "Versi santai" in telegram.sent[0]
    assert "Semut punya dua perut" not in telegram.sent[0]
    assert "ATURAN WAJIB" in groq.calls[0]["system"]  # prompt steers style
    assert "santai" in groq.calls[0]["system"]


def test_polish_failure_degrades_to_raw_body(tmp_path):
    groq = FakeGroq(failure=RuntimeError("groq 429'd"))
    polisher = polish_module.Polisher(groq)
    telegram = FakeTelegram()

    result = _run(tmp_path, [(StaticSource(), 100.0)], telegram=telegram,
                  polisher=polisher)

    assert result.sent is True                    # the send survives
    assert "Semut punya dua perut" in telegram.sent[0]  # raw body here


def test_opener_rotation_varies_between_sends(tmp_path):
    telegram = FakeTelegram()
    source = type("S", (), {})()
    source.fetch = lambda: _content()

    _run(tmp_path, [(source, 100.0)], telegram=telegram,
         rng=FakeRng(rolls=[0.0],
                     choices=["Fakta random hari ini:"]))
    # a second, distinct send (fresh state so cooldown doesn't bite)
    telegram2 = FakeTelegram()
    state2 = _store(tmp_path / "two")
    decide_and_send.run(
        telegram=telegram2, clock=FakeClock(),
        rng=FakeRng(rolls=[0.0], choices=["Tau gak..."]),
        state=state2, sources=[(source, 100.0)])

    assert telegram.sent[0].startswith("Fakta random hari ini:")
    assert telegram2.sent[0].startswith("Tau gak...")


def test_spoiler_injection_on_markdown_capable_ports(tmp_path):
    telegram = MarkdownTelegram()
    source = type("S", (), {})()
    source.fetch = lambda: _content(body="Fakta agar telinga olok-olok. 1+1=2!")

    # roll < 0.10 → spoiler YES
    result = _run(tmp_path, [(source, 100.0)], telegram=telegram,
                  rng=FakeRng(rolls=[0.01]))
    assert result.sent is True
    assert telegram.sent_markdown, "spoiler send must use the markdown port"
    wrapped = telegram.sent_markdown[0]
    assert wrapped.startswith("||")
    assert wrapped.endswith("||")
    assert "1\+1" in wrapped or "1\\+1" in wrapped, "special chars escaped"
    assert not telegram.sent, "plain port must NOT receive the spoiler copy"

    # roll >= 0.10 → NO spoiler: prob, pick, spoiler = [0.0, 0.0, 0.50]
    telegram2 = MarkdownTelegram()
    _run(tmp_path / "two", [(source, 100.0)], telegram=telegram2,
         rng=FakeRng(rolls=[0.0, 0.0, 0.50]))
    assert not telegram2.sent_markdown
    assert telegram2.sent, "non-spoiler goes through the plain port"


def test_spoiler_failure_falls_back_to_plain(tmp_path):
    class BrokenMarkdown(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def send_markdown_v2(self, text: str) -> bool:
            self.attempts += 1
            raise RuntimeError("telegram 400: bad markdown")

    telegram = BrokenMarkdown()
    source = type("S", (), {})()
    source.fetch = lambda: _content()

    result = _run(tmp_path, [(source, 100.0)], telegram=telegram,
                  rng=FakeRng(rolls=[0.01]))

    assert result.sent is True
    assert telegram.attempts == 1
    assert telegram.sent == [result.message], "plain text fallback"
    assert "1\+" not in telegram.sent[0]


def test_ai_generated_source_slot_parses_and_maps(tmp_path):
    groq = FakeGroq(replies=[
        "PERTANYAAN: Kenapa kucing suka kotak kardus?\n"
        "JAWABAN: Kardus kasih rasa aman dan jadi panas, bro."
    ])
    source = ai_generated_source.AIGeneratedSource(groq)
    content = source.fetch()
    assert content["source_name"] == "ai"
    assert "kucing" in content["title"]
    assert content["body"].startswith("Pertanyaan random:")
    assert "Jawabannya ternyata" in content["body"]


def test_ai_generated_slot_dropped_without_key(tmp_path):
    # (None, weight) entries are redistributed — no Groq → no AI slot
    class S:
        def fetch(self):
            return _content(source_name="wikipedia")

    pool = source_pool.build_pool([(S(), 35.0), (None, 10.0)])
    result = _run(tmp_path, pool)
    assert result.sent is True


def test_unparsable_ai_output_is_typed(tmp_path):
    groq = FakeGroq(replies=["teks acak tanpa format"])
    source = ai_generated_source.AIGeneratedSource(groq)
    try:
        source.fetch()
        assert False, "unparsable generation must be a typed SourceError"
    except SourceError:
        pass


def test_escape_semantics_match_the_notes():
    escaped = tg_format.escape_markdown_v2("Fakta: 1+1=2. Ok!")
    for ch in ("\\.", "\\!", "\\+"):
        assert ch in escaped
    raw = tg_format.spoiler("Pesan.")
    assert raw.startswith("||") and raw.endswith("||")
