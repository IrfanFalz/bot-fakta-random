"""Telegram MarkdownV2 formatting helpers (ticket T5 voice + spoilers).

Per research notes: spoiler = `||spoiler text||`; legacy Markdown lacks
it, so we always use `parse_mode=MarkdownV2`. The parser is picky: every
one of these 18 characters must be escaped when it is not deliberate
markup:

    _ * [ ] ( ) ~ ` > # + - = | { } . !

`escape_markdown_v2` is the blunt-but-safe escape pass for free text;
`markdown_v2 spoiler(text)` wraps the WHOLE payload in a spoiler block —
the tap-to-reveal bounce the PRD asks for on ~10% of sends.
"""
from __future__ import annotations

import re
from typing import List

SPOILER_CHANCE = 0.10  # PRD section 5.6: sesekali pakai spoiler
_MARKDOWNV2_CHARS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2 special character (blunt, always-correct)."""
    return _MARKDOWNV2_CHARS.sub(r"\\\1", text or "")


def spoiler(text: str) -> str:
    """Escape `text` fully, then wrap it in a MarkdownV2 spoiler block."""
    return f"||{escape_markdown_v2(text)}||"


def render(text: str, spoilered: bool) -> str:
    """Return the render-ready string for `parse_mode=MarkdownV2`."""
    return spoiler(text) if spoilered else escape_markdown_v2(text)
