"""Voice pass (ticket T5): raw source text -> casual Indonesian story.

PRD section 10.6 demands the polish prompt explicitly steer toward
casual/santai Bahasa Indonesia (NOT encyclopedic), storytelling tone,
150-400 words, no markdown headings — it lands in a chat, not a document.
Facts must stay accurate: never invent details.

Fallback semantics per the spec: ANY Groq failure (429 limit, 401 key,
dead model, network flake) returns the ORIGINAL body unchanged — the
message still goes out, the voice just degrades to encyclopedic. A missing
key means polish is simply off.
"""
from __future__ import annotations

import re
from typing import Any, Dict

_SYSTEM_PROMPT = (
    "Kamu editor konten santai untuk bot Telegram personal di Indonesia. "
    "Tugasmu: tulis ulang fakta mentah yang diberikan jadi satu pesan chat "
    "yang hangat dan bercerita, Panjang 150-400 kata (diukur dalam kata, "
    "bukan karakter). ATURAN WAJIB:\n"
    "1. Bahasa Indonesia santai sehari-hari (pakai 'kamu/gue' boleh, "
    "silahkan ayakan bahasa baku).\n"
    "2. FAKTA HARUS AKURAT — jangan mengarang detail, angka, atau nama baru.\n"
    "3. Tanpa heading markdown (#, ##), tanpa bullet list, tanpa emoji "
    "berlebihan (maksimal 1 emoji).\n"
    "4. Tanpa kalimat pembuka basa-basi seperti 'As an AI' atau "
    "'Tentu, ini dia'. Langsung masuk cerita.\n"
    "5. Akhiri dengan satu kalimat yang mengundang penasaran atau penutup "
    "enak, bukan moralisasi.\n"
    "Balas HANYA dengan isi pesannya."
)


def _trim_source_text(body: str, words: int = 800) -> str:
    """Keep the user prompt bounded (TPM headroom per research notes)."""
    text = body or ""
    split = text.split()
    if len(split) <= words:
        return text
    return " ".join(split[:words]) + " …"


class Polisher:
    """Voice port: `polish(content) -> str` with zero-risk fallback."""

    def __init__(self, groq: Any) -> None:
        self._groq = groq  # GroqClient

    def polish(self, content: Dict[str, Any]) -> str:
        """Return the polished body, or the raw body on any failure."""
        raw = str(content.get("body") or "").strip()
        title = str(content.get("title") or "").strip()
        user_prompt = (
            f"Fakta mentah:\nTitle: {title}\nIsi:\n{_trim_source_text(raw)}\n\n"
            "Tulis ulang jadi pesan chat santai sesuai aturan."
        )
        try:
            return self._groq.chat(_SYSTEM_PROMPT, user_prompt)
        except Exception:
            # voice degrades, send survives — raw, unmodified
            return raw


def strip_markdown_heading(text: str) -> str:
    """Safety net for stray AI markdown headings (PRD 10.6)."""
    return re.sub(r"^#{1,6}\s.*$", "", text or "", flags=re.M).strip()
