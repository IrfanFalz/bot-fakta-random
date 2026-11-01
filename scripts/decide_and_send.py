"""Delivery pipeline orchestrator (the single test seam for T1).

`run(...)` takes injected ports only:
- telegram : send/update client port (test fake, printing fake in --dry-run,
             or `telegram_client.TelegramClient` in production)
- source   : content source port returning the uniform PRD shape
             {title, body, keywords, source_name, image_query}; it may raise
             `SourceError` (or any exception) or return None — the orchestrator
             treats every source failure as a clean skip-this-run so a red
             Actions run can never originate from a source
- sources  : optional weighted candidate list [(source_port, weight), ...]
             (ticket T2: a single source, Wikipedia, weight 100%; later
             tickets spread weights). Overrides the single `source` port; the
             pick rolls on the same injected rng (uniform over the weights).
- clock    : `now()` -> aware datetime (UTC)
- rng      : `random()` port, `choice()` for formatting variety
- state    : `state_store.JsonStateStore`

Decision order (fast exit, as specified in ticket #2):
    poll pending telegram updates (offset only; command handling is T7)
      -> cooldown check from state
      -> probability roll
      -> pick a source from the weighted list
      -> fetch content *only if we are actually sending*
      -> format & send via telegram port
      -> persist state + history

CLI:
    python scripts/decide_and_send.py [--dry-run] [--force-send]

`--dry-run` runs the same pipeline with a printing telegram stub, no polling,
and no state writes; it always reaches the format step so the would-be
message is shown. `--force-send` (flag or FORCE_SEND env) bypasses cooldown
and probability, for verification and the repository_dispatch trigger.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import state_store  # noqa: E402
from state_store import datetime_to_iso, iso_to_datetime  # noqa: E402

DEFAULT_PROBABILITY = 0.06
DEFAULT_COOLDOWN_MINUTES = 90

# PRD 5.6: casual openers, chosen at random so messages don't read scheduled.
OPENERS = (
    "Fakta random hari ini:",
    "Tau gak...",
    "Hal kecil yang jarang disadari:",
)

PLACEHOLDER_SOURCE = "placeholder"  # value of the offline --dry-run stand-in


@dataclass
class RunResult:
    """What one pipeline run did; drives CLI output and CI commit logic."""

    sent: bool
    message: str
    reason: str


class TelegramPort(Protocol):
    def is_configured(self) -> bool: ...
    def send_message(self, text: str) -> bool: ...
    def get_updates(self, offset: Optional[int]) -> List[Dict[str, Any]]: ...


class SourcePort(Protocol):
    def fetch(self) -> Dict[str, Any]: ...


class PlaceholderSource:
    """Offline stand-in content, used ONLY by --dry-run so the preview never
    touches the network (see main()). Real runs select from `sources`
    (Wikipedia in ticket T2, more sources in later tickets)."""

    def fetch(self) -> Dict[str, Any]:
        return {
            "title": "Halo, aku bot fakta random",
            "body": "Halo, aku bot fakta random. Tes delivery.",
            "keywords": ["tes", "delivery"],
            "source_name": "placeholder",
        }


class PrintingTelegramPort:
    """Dry-run telegram port: what would be sent goes to stdout instead."""

    def is_configured(self) -> bool:
        return True

    def send_message(self, text: str) -> bool:
        print("--- would be sent (dry-run) ---")
        print(text)
        print("-------------------------------")
        return True

    def get_updates(self, offset: Optional[int]) -> List[Dict[str, Any]]:
        return []


def _cooldown_active(last_sent_iso: Optional[str], now, cooldown_minutes: int) -> bool:
    last = iso_to_datetime(last_sent_iso)
    if last is None:
        return False
    return bool((now - last).total_seconds() < cooldown_minutes * 60)


def format_message(content: Dict[str, Any], rng: Any) -> str:
    """Compose the outbound text: random opener + source body."""
    opener = rng.choice(OPENERS)
    body = content.get("body") or ""
    return f"{opener} {body}"


def pick_source(sources: List[tuple], rng: Any) -> Any:
    """Weighted pick from [(source_port, weight), ...].

    Rolls once on the injected rng so tests stay deterministic; a single
    candidate is returned without consuming rng (uniform over the weights).
    """
    total = float(sum(weight for _, weight in sources))
    roll = rng.random() * total
    cumulative = 0.0
    for source, weight in sources:
        cumulative += float(weight)
        if roll < cumulative:
            return source
    return sources[-1][0]  # float edge (roll == total): last candidate


def run(telegram: TelegramPort, source: Optional[SourcePort] = None,
        clock: Any = None, rng: Any = None, state: Any = None, *,
        probability: float = DEFAULT_PROBABILITY,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES, force: bool = False,
        dry_run: bool = False, poll: bool = True,
        sources: Optional[List[tuple]] = None) -> RunResult:
    """One execution of the delivery decision pipeline.

    Exactly one content seam is expected: pass `sources` (weighted list, the
    production path) or the single `source` port (tests). Source failures —
    be it a `SourceError`, an unexpected exception, or None — always resolve
    to the skip reason `source-fetch-failed`: run stays green, nothing is
    sent, nothing is written.
    """
    if dry_run:
        telegram = PrintingTelegramPort()
        force = True  # dry-run always resolves to the would-be message
        poll = False  # zero network effects

    if not telegram.is_configured():
        return RunResult(False, "", "telegram-not-configured")

    current = state.load_state()
    loaded = dict(current)

    # 1) Poll pending telegram updates: record the newest offset so replies
    #    are not seen twice (full command handling arrives in ticket T7).
    if poll:
        offset = current.get("last_update_id")
        updates = telegram.get_updates(
            offset if offset is not None else None
        )
        if updates:
            current["last_update_id"] = max(
                int(update.get("update_id", 0)) for update in updates
            ) + 1

    now = clock.now()

    # 2) Cooldown, unless forced.
    if not force and _cooldown_active(
            current.get("last_sent_timestamp"), now, cooldown_minutes):
        _persist_state_if_changed(state, current, current != loaded)
        return RunResult(False, "", "cooldown")

    # 3) Probability roll, unless forced.
    if not force and rng.random() >= probability:
        _persist_state_if_changed(state, current, current != loaded)
        return RunResult(False, "", "probability-roll")

    # 4) Heavy work only happens when we are actually sending: pick a source
    #    from the weighted list, fetch, and swallow EVERY source failure so
    #    the run stays green (no send, no state write, no history append).
    candidates = sources if sources is not None else [(source, 100.0)]
    chosen = pick_source(candidates, rng)
    try:
        content = chosen.fetch()
    except Exception as exc:  # typed SourceError, network hiccups, odd shapes
        print(f"content source failed (skip this run): {exc}", file=sys.stderr)
        return RunResult(False, "", "source-fetch-failed")
    if not isinstance(content, dict) or not (content.get("body") or "").strip():
        print("content source returned an unusable/malformed result",
              file=sys.stderr)
        return RunResult(False, "", "source-fetch-failed")

    message = format_message(content, rng)

    try:
        if not telegram.send_message(message):
            return RunResult(False, "", "telegram-send-failed")
    except Exception as exc:  # keep the workflow green on API hiccups
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return RunResult(False, "", "telegram-send-failed")

    # 5) Persist state + history (PRD shapes) — but never in dry-run mode,
    #    which must have zero writes so CI commits happen only for real sends.
    if dry_run:
        return RunResult(False, message, "dry-run")
    now_iso = datetime_to_iso(now)
    current["last_sent_timestamp"] = now_iso
    history = state.load_history()
    history.append({
        "title": content.get("title", ""),
        "keywords": list(content.get("keywords", [])),
        # Uniform contract names it `source_name`; accept legacy `source`.
        "source": content.get("source_name") or content.get("source")
                  or "unknown",
        "timestamp": now_iso,
    })
    state.save_history(history)
    state.save_state(current)
    return RunResult(True, message, "sent")


def _persist_state_if_changed(state: Any, current: Dict[str, Any],
                              changed: bool) -> None:
    """Write state back only when something actually changed (no-op runs
    must be byte-identical on disk so CI has nothing to commit)."""
    if changed:
        state.save_state(current)


class SystemClock:
    """Clock port backed by the system UTC clock."""

    def now(self):
        return datetime.now(timezone.utc)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Decide whether to send a faktum now.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the would-be message; no network, no state writes")
    parser.add_argument("--force-send", action="store_true",
                        help="bypass cooldown and probability (verification / dispatch)")
    args = parser.parse_args(argv)

    force = args.force_send or os.environ.get("FORCE_SEND", "").lower() in {"1", "true", "yes"}
    probability = float(os.environ.get("PROBABILITY", DEFAULT_PROBABILITY))
    cooldown_minutes = int(os.environ.get("COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES))

    from telegram_client import TelegramClient
    from wikipedia_source import WikipediaSource

    if args.dry_run:
        # Dry-run is a zero-network preview: offline stand-in content instead
        # of the live Wikipedia fetch.
        candidate_sources = [(PlaceholderSource(), 100.0)]
    else:
        # Ticket T2: single weighted source (Wikipedia, 100%). Later tickets
        # add further candidates and spread the weights.
        candidate_sources = [(WikipediaSource(), 100.0)]

    result = run(
        telegram=TelegramClient(),
        sources=candidate_sources,
        clock=SystemClock(),
        rng=random,
        state=state_store.JsonStateStore(),
        probability=probability,
        cooldown_minutes=cooldown_minutes,
        force=force,
        dry_run=args.dry_run,
    )
    if result.sent:
        print("sent a message to the chat")
    elif args.dry_run:
        print("dry-run finished; no send, no state writes")
    else:
        print(f"skipped: {result.reason}")
    return 0  # green run is the default outcome; only fatal logic errors exit nonzero


if __name__ == "__main__":
    sys.exit(main())
