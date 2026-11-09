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


def split_escaped(text: str, limit: int) -> "list[str]":
    """Word-boundary chunks whose ESCAPED length stays under `limit`.

    MarkdownV2 escaping can nearly double a string (`1+1=2` -> `1\\+1\\=2`),
    so chunks are measured AFTER escaping (code-review fix: a polished
    ~400-word story once escaped can blow past the 4096 sendMessage cap).
    Splits always land on spaces (an escape sequence never contains one), so
    a chunk can never begin or end mid-escape. A single monster word still
    over the limit is hard-cut on its escaping-aware size.
    """
    text = text or ""
    if len(escape_markdown_v2(text)) <= limit:
        return [text]
    chunks: "list[str]" = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if len(escape_markdown_v2(candidate)) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # monster word: shrink on escaped size, hard-cut pieces apart
        while len(escape_markdown_v2(word)) > limit:
            cut = len(word)
            while cut > 1 and len(escape_markdown_v2(word[:cut])) > limit:
                cut -= 1
            chunks.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]
