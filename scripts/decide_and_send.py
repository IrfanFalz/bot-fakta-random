"""Delivery pipeline orchestrator (the single test seam for T1).

`run(...)` takes injected ports only:
- telegram : send/update client port (test fake, printing fake in --dry-run,
             or `telegram_client.TelegramClient` in production)
- source   : content source port returning the uniform PRD shape
             {title, body, keywords, source, ...}
- clock    : `now()` -> aware datetime (UTC)
- rng      : `random()` port, `choice()` for formatting variety
- state    : `state_store.JsonStateStore`

Decision order (fast exit, as specified in ticket #2):
    poll pending telegram updates (scheduled path only; /lagi handling is T7)
      -> handle pending /lagi commands (cooldown/probability bypassed, T7)
      -> cooldown check from state
      -> probability roll
      -> fetch content *only if we are actually sending*
      -> format & send via telegram port (the ONE shared pipeline)
      -> persist state + history

On-demand paths (ticket #8): a repository_dispatch run (client_payload
trigger == "lagi", set by the Cloudflare webhook worker) or `--lagi` runs
the exact same pipeline with cooldown and probability bypassed; those runs
never poll updates (the webhook owns realtime and getUpdates errors while
a webhook is set anyway). The scheduled run is the fallback: it reads at
most ONE zero-timeout getUpdates batch and handles the /lagi commands it
finds there, always advancing the stored offset past everything returned
(even if a later step fails) so updates are never processed twice.

CLI:
    python scripts/decide_and_send.py [--dry-run] [--force-send] [--lagi]

`--dry-run` runs the same pipeline with a printing telegram stub, no polling,
and no state writes; it always reaches the format step so the would-be
message is shown. `--force-send` (flag or FORCE_SEND env) bypasses cooldown
and probability, for verification and the repository_dispatch trigger.
`--lagi` is the on-demand mode (also selected by TRIGGER=lagi env, what the
worker-triggered dispatch run uses); it behaves like --force-send without
polling.
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

PLACEHOLDER_SOURCE = "placeholder"

# T7 on-demand path. `/lagi` is matched as a text prefix (so `/lagi@botname`
# works too). Documented bounded choice: one send per pending /lagi command,
# but at most MAX_LAGI_PER_RUN per scheduled run — extras beyond the cap are
# swallowed with the offset still advanced, so the run stays cheap and no
# update is ever re-processed.
LAGI_COMMAND = "/lagi"
ON_DEMAND_TRIGGER = "lagi"  # webhook worker sends client_payload {trigger: "lagi"}
MAX_LAGI_PER_RUN = 3


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
    """T1 placeholder source so tests exercise a full uniform content shape.

    Real sources (Wikipedia etc.) arrive in later tickets.
    """

    def fetch(self) -> Dict[str, Any]:
        return {
            "title": "Halo, aku bot fakta random",
            "body": "Halo, aku bot fakta random. Tes delivery.",
            "keywords": ["tes", "delivery"],
            "source": PLACEHOLDER_SOURCE,
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


def _update_text(update: Dict[str, Any]) -> str:
    """The text of an update's `message`, empty when absent or wrong shape."""
    message = update.get("message")
    if not isinstance(message, dict):
        return ""
    text = message.get("text")
    return text if isinstance(text, str) else ""


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


def run(telegram: TelegramPort, source: SourcePort, clock: Any, rng: Any,
        state: Any, *, probability: float = DEFAULT_PROBABILITY,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES, force: bool = False,
        dry_run: bool = False, poll: bool = True,
        trigger: Optional[str] = None) -> RunResult:
    """One execution of the delivery decision pipeline.

    `trigger` carries the repository_dispatch client_payload.trigger value
    ("lagi" for the T7 on-demand path): such a run forces the send and never
    polls updates. Scheduled runs leave it None/falsy.
    """
    if trigger == ON_DEMAND_TRIGGER:
        force = True  # on-demand sends always deliver: no cooldown, no roll

    if dry_run:
        telegram = PrintingTelegramPort()
        force = True  # dry-run always resolves to the would-be message
        poll = False  # zero network effects

    if force:
        poll = False  # forced/on-demand runs don't poll: getUpdates is the
                      # scheduled fallback's job and errors while a webhook
                      # is set anyway

    if not telegram.is_configured():
        return RunResult(False, "", "telegram-not-configured")

    current = state.load_state()
    loaded = dict(current)

    def _run_send_pipeline(moment: Any) -> "tuple[str, bool]":
        """Fetch -> format -> send -> record history/state.

        THE one send pipeline (PRD D22): scheduled rolls, on-demand /lagi,
        forced dispatch runs and dry-run all land here, so behavior never
        forks. Mutates `current` and persists on success only.
        Returns (message, sent).
        """
        content = source.fetch()
        message = format_message(content, rng)
        try:
            send_ok = telegram.send_message(message)
        except Exception as exc:  # keep the workflow green on API hiccups
            print(f"telegram send failed: {exc}", file=sys.stderr)
            return message, False
        if not send_ok:
            return message, False

        if dry_run:
            return message, True  # caller decides the dry-run outcome

        # Persist state + history (PRD shapes) — only for real sends, so CI
        # commits happen only when something actually happened.
        now_iso = datetime_to_iso(moment)
        current["last_sent_timestamp"] = now_iso
        history = state.load_history()
        history.append({
            "title": content.get("title", ""),
            "keywords": list(content.get("keywords", [])),
            "source": content.get("source", PLACEHOLDER_SOURCE),
            "timestamp": now_iso,
        })
        state.save_history(history)
        state.save_state(current)
        return message, True

    # 1) Poll pending telegram updates (scheduled path only, ONE cheap
    #    zero-timeout call): advance the stored offset past everything
    #    returned — always, even when handling later fails, so no update is
    #    ever processed twice — and fish out the /lagi commands. Every other
    #    update is swallowed here; command handling continues below.
    lagi_updates: List[Dict[str, Any]] = []
    if poll:
        offset = current.get("last_update_id")
        try:
            updates = telegram.get_updates(
                offset if offset is not None else None
            )
        except Exception as exc:  # fallback-safe: the next run retries anyway
            print(f"getUpdates failed (continuing): {exc}", file=sys.stderr)
            updates = []
        if updates:
            current["last_update_id"] = max(
                int(update.get("update_id", 0)) for update in updates
            ) + 1
            lagi_updates = [
                update for update in updates
                if _update_text(update).startswith(LAGI_COMMAND)
            ]

    now = clock.now()

    # 2) On-demand handling of pending /lagi commands (poll fallback, T7):
    #    the same send pipeline with cooldown and probability bypassed.
    #    Bounded by MAX_LAGI_PER_RUN; a failing pipeline run still leaves the
    #    (already advanced) offset persisted before we bail out green.
    if lagi_updates and not dry_run:
        for _ in range(min(len(lagi_updates), MAX_LAGI_PER_RUN)):
            try:
                message, sent = _run_send_pipeline(now)
            except Exception as exc:  # source/network crash: keep the run green
                print(f"on-demand pipeline failed: {exc}", file=sys.stderr)
                _persist_state_if_changed(state, current, current != loaded)
                return RunResult(False, "", "on-demand-pipeline-failed")
            if not sent:
                # send failed but the updates are consumed: persist the
                # advanced offset so nothing is processed twice
                _persist_state_if_changed(state, current, current != loaded)
                return RunResult(False, "", "telegram-send-failed")
        return RunResult(True, message, "sent")

    # 3) Cooldown, unless forced.
    if not force and _cooldown_active(
            current.get("last_sent_timestamp"), now, cooldown_minutes):
        _persist_state_if_changed(state, current, current != loaded)
        return RunResult(False, "", "cooldown")

    # 4) Probability roll, unless forced.
    if not force and rng.random() >= probability:
        _persist_state_if_changed(state, current, current != loaded)
        return RunResult(False, "", "probability-roll")

    # 5) Heavy work only happens when we are actually sending — the one
    #    shared pipeline (forced dispatch runs, dry-run and a lucky
    #    scheduled roll all come through here).
    message, sent = _run_send_pipeline(now)
    if dry_run:
        return RunResult(False, message, "dry-run")
    if not sent:
        _persist_state_if_changed(state, current, current != loaded)
        return RunResult(False, "", "telegram-send-failed")
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
    parser.add_argument("--lagi", action="store_true",
                        help="on-demand run: same pipeline, cooldown/probability "
                             "bypassed, never polls updates (T7 webhook path)")
    args = parser.parse_args(argv)

    force = args.force_send or os.environ.get("FORCE_SEND", "").lower() in {"1", "true", "yes"}
    # resolve the on-demand trigger (T7) once here; run() also honors it directly
    trigger = ON_DEMAND_TRIGGER if args.lagi or os.environ.get(
        "TRIGGER", "") == ON_DEMAND_TRIGGER else None
    probability = float(os.environ.get("PROBABILITY", DEFAULT_PROBABILITY))
    cooldown_minutes = int(os.environ.get("COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES))

    from telegram_client import TelegramClient

    result = run(
        telegram=TelegramClient(),
        source=PlaceholderSource(),
        clock=SystemClock(),
        rng=random,
        state=state_store.JsonStateStore(),
        probability=probability,
        cooldown_minutes=cooldown_minutes,
        force=force,
        dry_run=args.dry_run,
        trigger=trigger,
    )
    if result.sent:
        print("sent a message to the chat"
              + (" (on-demand /lagi)" if trigger == ON_DEMAND_TRIGGER else ""))
    elif args.dry_run:
        print("dry-run finished; no send, no state writes")
    else:
        print(f"skipped: {result.reason}")
    return 0  # green run is the default outcome; only fatal logic errors exit nonzero


if __name__ == "__main__":
    sys.exit(main())
