"""Weighted source pool builder (ticket T4).

`build_pool(slots)` takes the desired mix — [(source_or_None, weight), ...]
where a `None` entry means the source is UNAVAILABLE this run (missing API
key, source physically dead, disabled by config) — and returns a cleaned
list whose weights are re-normalized so they sum to 100: the weight of a
dropped source is redistributed proportionally across the remaining
healthy ones. Later tickets (T5: the AI-generated slot) reuse this.

A pool with zero usable entries raises `ValueError` so a misconfigured
production build fails loudly at startup, not at send time.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple


def build_pool(slots: Sequence[Tuple[Optional[object], float]]) -> List[tuple]:
    usable = [(source, float(weight)) for source, weight in slots
              if source is not None and float(weight) > 0]
    if not usable:
        raise ValueError("source pool: no usable sources configured")
    total = sum(weight for _, weight in usable)
    if total <= 0:
        raise ValueError("source pool: total weight must be positive")
    return [(source, round(weight * 100.0 / total, 6)) for source, weight in usable]
