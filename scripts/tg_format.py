"""Telegram HTML formatting helpers (voice + bold + spoilers).

One parse mode for the whole bot: `parse_mode=HTML` — the well-tolerated
side of the Telegram parser (only &, <, > need escaping; MarkdownV2's
18-char escape dance is gone). Utilities:

- `escape_html(text)`   : blunt escape pass for free text;
- `plain_to_html(text)` : escape + `**bold**` markers -> `<b>bold</b>`
                          (the marker convention the Groq polisher outputs);
- `plain_text(text)`    : markers stripped — for photo captions and the
                          plain-port fallback;
- `split_for_html(text)`: word-boundary chunks (order preserved) safe for
   the sendMessage 4096-after-parsing limit;
- `spoiler_html(text)`  : tap-to-reveal wrapper for the ~10% spoiler sends.
"""
from __future__ import annotations

import re

SPOILER_CHANCE = 0.10  # PRD section 5.6: sesekali pakai spoiler

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def escape_html(text: str) -> str:
    """Escape the three HTML-sensitive characters (order matters: & first)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plain_text(text: str) -> str:
    """Bold markers stripped: the body as literally-visible characters."""
    return _BOLD_RE.sub(r"\1", text or "")


def plain_to_html(text: str) -> str:
    """Escape everything, then turn `**term**` into real `<b>` entities.

    Escape FIRST (asterisks survive escaping untouched), then convert the
    remaining markers — so user content that contains a literal `**` pair
    also renders as bold rather than leaking raw asterisks.
    """
    return _BOLD_RE.sub(r"<b>\1</b>", escape_html(text or ""))


def split_for_html(text: str, limit: int) -> "list[str]":
    """Word-boundary chunks whose RENDERED length stays under `limit`.

    The Telegram limit counts characters after entity parsing — that is the
    visible text (tags/escapes collapse). We conservatively measure the
    escaped+tagged form (`plain_to_html`), which is always >= the visible
    count, so chunks can never tip over. Splits land on spaces; a monster
    word still over the limit is hard-cut.
    """
    text = text or ""
    if len(plain_to_html(text)) <= limit:
        return [text]
    chunks: "list[str]" = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if len(plain_to_html(candidate)) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # monster word: shrink on rendered size, hard-cut pieces apart
        while len(plain_to_html(word)) > limit:
            cut = len(word)
            while cut > 1 and len(plain_to_html(word[:cut])) > limit:
                cut -= 1
            chunks.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def spoiler_html(text: str) -> str:
    """Wrap an ALREADY-html-safe chunk in a tap-to-reveal spoiler block."""
    return f"<tg-spoiler>{text}</tg-spoiler>"
