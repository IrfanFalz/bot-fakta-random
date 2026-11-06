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
    poll pending telegram updates (scheduled path only; /lagi handling is T7)
      -> handle pending /lagi commands (cooldown/probability bypassed, T7)
      -> cooldown check from state
      -> probability roll
      -> pick a source from the weighted list
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
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import state_store  # noqa: E402
import dedupe  # noqa: E402
from state_store import datetime_to_iso, iso_to_datetime  # noqa: E402
from wikipedia_source import SourceError  # noqa: E402

DEFAULT_PROBABILITY = 0.06
DEFAULT_COOLDOWN_MINUTES = 90
# Ticket T3: don't resend a topic that resembles every recent one. The
# threshold can be tuned with the DEDUPE_THRESHOLD env var.
DEFAULT_DEDUPE_THRESHOLD = dedupe.DEFAULT_THRESHOLD
DEFAULT_DEDUPE_ATTEMPTS = 3

# PRD 5.6: casual openers, chosen at random so messages don't read scheduled.
OPENERS = (
    "Fakta random hari ini:",
    "Tau gak...",
    "Hal kecil yang jarang disadari:",
)

PLACEHOLDER_SOURCE = "placeholder"  # value of the offline --dry-run stand-in

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


def pick_source(sources: List[tuple], rng: Any) -> Any:
    """Weighted pick from [(source_port, weight), ...].

    Rolls once on the injected rng (uniform over relative weights, so tests
    stay deterministic); a single candidate is therefore returned too, just
    with a fruitless roll.
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
        sources: Optional[List[tuple]] = None,
        trigger: Optional[str] = None,
        dedupe_threshold: float = DEFAULT_DEDUPE_THRESHOLD) -> RunResult:
    """One execution of the delivery decision pipeline.

    Exactly one content seam is expected: pass `sources` (weighted list, the
    production path) or the single `source` port (tests). Source failures —
    be it a `SourceError`, an unexpected exception, or a malformed result —
    are raised out of the shared pipeline and mapped per path: the scheduled/
    forced path resolves to `source-fetch-failed` (run stays green, nothing
    is sent, nothing is written), the queued /lagi handler to
    `on-demand-pipeline-failed`.

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
        forks. Source failures RAISE (the caller maps them per path —
        scheduled: `source-fetch-failed`, queued /lagi:
        `on-demand-pipeline-failed`); telegram send failures return
        (message, False) instead so the offset/roll plumbing still persists.
        Mutates `current` and persists on success only. Returns
        (message, sent).
        """
        # Heavy work only when we are actually sending: pick from the
        # weighted candidate list, fetch, and retry against the repetition
        # gate (ticket T3) — a near-duplicate candidate is thrown back and a
        # different source is picked, up to `DEFAULT_DEDUPE_ATTEMPTS` tries;
        # after that the best candidate in hand is sent anyway rather than
        # the send silently disappearing.
        candidates = sources if sources is not None else [(source, 100.0)]
        history_snapshot = [] if dry_run else state.load_history()
        content: Optional[Dict[str, Any]] = None
        for attempt in range(1, DEFAULT_DEDUPE_ATTEMPTS + 1):
            chosen = pick_source(candidates, rng)
            candidate = chosen.fetch()
            if not isinstance(candidate, dict) or not (candidate.get("body") or "").strip():
                raise SourceError(
                    "content source returned an unusable/malformed result"
                )
            if (attempt >= DEFAULT_DEDUPE_ATTEMPTS
                    or not dedupe.is_duplicate(candidate, history_snapshot,
                                               dedupe_threshold)):
                content = candidate
                break
            print(f"candidate looks like a recent topic (attempt {attempt}), "
                  "re-picking a different source", file=sys.stderr)
        if content is None:  # pragma: no cover - loop always assigns or raises
            raise SourceError("no usable content after all dedupe attempts")
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
            # Uniform contract names it `source_name`; accept legacy `source`.
            "source": content.get("source_name") or content.get("source")
                      or "unknown",
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

    # 5) Heavy work only happens when we are actually sending: the ONE
    #    shared pipeline picks a source from the weighted list, fetches,
    #    formats and sends (scheduled rolls, forced dispatch and dry-run all
    #    come through here). Source failures map to a green skip.
    try:
        message, sent = _run_send_pipeline(now)
    except Exception as exc:  # typed SourceError, network hiccups, odd shapes
        print(f"content source failed (skip this run): {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _persist_state_if_changed(state, current, current != loaded)
        return RunResult(False, "", "source-fetch-failed")
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
    dedupe_threshold = float(os.environ.get(
        "DEDUPE_THRESHOLD", DEFAULT_DEDUPE_THRESHOLD))

    from telegram_client import TelegramClient
    from wikipedia_source import WikipediaSource
    from reddit_source import RedditSource
    from trivia_source import TriviaSource
    from numbers_source import NumbersSource
    from source_pool import build_pool

    # Ticket T4 desired mix (weights by spec section 5.2); slot entries that
    # are None (unconfigured/absent) get their weight redistributed by the
    # pool builder — the bot never mentions its plumbing.
    desired_mix = [
        (WikipediaSource(), 35.0),
        (RedditSource(
            user_agent=os.environ.get(
                "REDDIT_USER_AGENT",
                "bot-fakta-random/1.0 (personal Telegram bot, "
                "github.com/IrfanFalz/bot-fakta-random)")), 25.0),
        (TriviaSource(), 20.0),
        (NumbersSource(), 10.0),
        # AI-generated weird-question slot: wired in ticket T5 when a Groq
        # key exists; for now the slot is (None, weight) → its weight gets
        # redistributed by the pool builder.
        (None, 10.0),
    ]
    candidate_sources = build_pool(desired_mix)
    if args.dry_run:
        # Dry-run is a zero-network preview: offline stand-in content instead
        # of any live source fetch.
        candidate_sources = [(PlaceholderSource(), 100.0)]

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
        trigger=trigger,
        dedupe_threshold=dedupe_threshold,
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
