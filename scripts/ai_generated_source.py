"""AI-generated source (ticket T5): weird-question slot, the spec's 10%.

When the Groq key is configured, production adds this to the weighted
pool: a single chat call generates a unique, weird-but-real-science
question and its answer in casual Indonesian (the PRD's "Generated
pertanyaan nyeleneh"). Missing key = the slot is not in the pool at all
(decide_and_send.main builds the pool; the EmptyKey means no call is ever
made, zero cost).

Same failure semantics as every source: the typed `SourceError` (the
groq client raises GroqError which is mapped to the skip path).
"""
from __future__ import annotations

import string
from typing import Any, Dict

from wikipedia_source import SourceError  # same typed contract

GENERATION_PROMPT = (
    "Kamu kreator konten untuk bot Telegram fakta random (bahasa Indonesia "
    "santai). Bikin SATU pertanyaan nyeleneh yang jawabannya nyata secara "
    "sains (bukan opini, bukan template abal). Kualitas: topik jarang "
    "dibahas orang, wow faktual. Format balasan EXACT seperti ini:\n"
    "PERTANYAAN: <pertanyaannya>\n"
    "JAWABAN: <1 paragraf 3-6 kalimat, santai, akurat, maksimal 1 emoji>\n"
)


def _parse_exports(text: str) -> Dict[str, str]:
    question = ""
    answer = ""
    for line in text.splitlines():
        line = line.strip()
        upper = line.upper()
        if upper.startswith("PERTANYAAN:") and not question:
            question = line.split(":", 1)[1].strip()
        elif upper.startswith("JAWABAN:") and not answer:
            answer = line.split(":", 1)[1].strip()
    if not question or not answer:
        raise SourceError("ai generation output unparsable")
    return {"question": question, "answer": answer}


def _keywords_from(text: str, limit: int = 5) -> list:
    words = [word.strip(string.punctuation).casefold()
             for word in (text or "").split()]
    return [w for w in words if len(w) >= 3][:limit]


class AIGeneratedSource:
    """One chat call per pick (uniform contract, `source_name:"ai"`)."""

    def __init__(self, groq: Any) -> None:
        self._groq = groq

    def fetch(self) -> Dict[str, Any]:
        try:
            content = self._groq.chat(GENERATION_PROMPT,
                                      "Bikin satu pertanyaan nyeleneh baru.",
                                      max_tokens=500)
        except Exception as exc:
            raise SourceError(f"ai generation failed: {exc}") from exc
        parsed = _parse_exports(content)
        question = parsed["question"]
        answer = parsed["answer"]
        return {
            "title": question.lower().rstrip(".?"),
            "body": f"Pertanyaan random: {question}\n\nJawabannya ternyata: {answer}",
            "keywords": _keywords_from(question),
            "source_name": "ai",
            "image_query": " ".join(_keywords_from(question, 3)) or "science",
        }
