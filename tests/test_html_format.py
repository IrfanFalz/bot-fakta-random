"""Unit tests for the HTML formatting pass (conversational readability).

Together with test_t5_voice.py this pins the ONE parse-mode contract:
- `**bold**` markers from the Groq polisher become real `<b>` entities;
- every `& < >` in user-facing text is escaped (never raw);
- `plain_text` strips the markers for captions / plain-port fallback;
- `split_for_html` measures the escaped+tagged form so no sendMessage
  ever tips over the 4096-after-parsing cap.
"""
import re
from typing import List

import polish
import tg_format
from decide_and_send import run

import pytest


class HtmlOnlyTelegram:
    """Port WITHOUT send_message: forces the HTML path to be exercised."""

    def __init__(self):
        self.html: List[str] = []

    def is_configured(self) -> bool:
        return True

    def send_html(self, text: str) -> bool:
        self.html.append(text)
        return True

    def get_updates(self, offset):
        return []


class _MemoryState:
    """Reuse _store-like clock + state fixtures across tests."""

    pass


class FakeClock:
    def now(self):
        import datetime
        return datetime.datetime(2026, 11, 9, 6, 0, 0,
                                 tzinfo=datetime.timezone.utc)


class FakeRng:
    def __init__(self, rolls=None):
        self.rolls = list(rolls or [])
        self.i = 0

    def random(self):
        value = self.rolls[min(self.i, len(self.rolls) - 1)]
        self.i += 1
        return value


class StaticSource:
    def fetch(self):
        return {
            "title": "Fakta HTML",
            "keywords": ["html"],
            "body": "Isi dengan **istilah penting**.",
            "source_name": "placeholder",
        }


def _run(tmp_path, telegram, rolls):
    import state_store
    state = state_store.make_store(tmp_path)
    return run(telegram=telegram, clock=FakeClock(), rng=FakeRng(rolls),
               state=state, sources=[(StaticSource(), 100.0)])


# --- tg_format primitives -------------------------------------------------

def test_plain_to_html_converts_bold_and_escapes():
    assert tg_format.plain_to_html("a **RAM** b") == "a <b>RAM</b> b"
    html = tg_format.plain_to_html("i & i < 3")
    assert "i &amp; i &lt; 3" == html


def test_plain_to_html_leaves_unpaired_markers_alone():
    # a single stray asterisk is literal text, never swallowed
    assert tg_format.plain_to_html("bintang * puluh") == "bintang * puluh"


def test_plain_text_strips_markers_for_captions():
    assert tg_format.plain_text("**kunci**: definisi") == "kunci: definisi"


def test_spoiler_html_wraps_the_chunk():
    wrapped = tg_format.spoiler_html("teks")
    assert wrapped == "<tg-spoiler>teks</tg-spoiler>"


def test_split_for_html_respects_limit_and_order():
    story = " ".join(f"kata{i} panjang&{i}!" for i in range(120))
    chunks = tg_format.split_for_html(story, 300)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(tg_format.plain_to_html(chunk)) <= 300
    assert " ".join(chunks) == story, "order + every word survive"


def test_split_for_html_short_text_is_single_chunk():
    assert tg_format.split_for_html("pendek", 300) == ["pendek"]


def test_split_for_html_cuts_monster_word():
    monster = "A" * 700 + " ekor"
    chunks = tg_format.split_for_html(monster, 300)
    assert all(len(tg_format.plain_to_html(chunk)) <= 300 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == monster.replace(" ", "")


# --- polisher PROMPT contract (readability rules live here) ---------------

def test_polisher_prompt_demands_paragraphs_and_bounded_bold():
    prompt = polish._SYSTEM_PROMPT
    assert "ATURAN WAJIB" in prompt
    assert "PARAGRAF PENDEK" in prompt
    assert "baris kosong" in prompt
    assert "**" in prompt, "bold-marker convention pinned in the prompt"
    assert "maksimal 3" in prompt


def test_polisher_prompt_keeps_the_voice_rules():
    prompt = polish._SYSTEM_PROMPT
    assert "santai" in prompt
    assert "FAKTA HARUS AKURAT" in prompt
