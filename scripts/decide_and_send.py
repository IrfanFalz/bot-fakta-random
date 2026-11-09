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
import re
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
import tg_format  # noqa: E402
from state_store import datetime_to_iso, iso_to_datetime  # noqa: E402
from wikipedia_source import SourceError  # noqa: E402

DEFAULT_PROBABILITY = 0.02  # ~1-2 sends/day over 96 scheduled checks (code-review recalibration)
DEFAULT_COOLDOWN_MINUTES = 90
# Ticket T3: don't resend a topic that resembles every recent one. The
# threshold can be tuned with the DEDUPE_THRESHOLD env var.
DEFAULT_DEDUPE_THRESHOLD = dedupe.DEFAULT_THRESHOLD
DEFAULT_DEDUPE_ATTEMPTS = 3
# Telegram hard limits (research notes; ticket T6 enforces them here).
TEXT_LIMIT = 4096     # sendMessage: 1-4096 chars AFTER entity parsing
PHOTO_CAPTION_LIMIT = 1024  # sendPhoto captions: max 1024 chars

# PRD 5.6: casual openers, chosen at random so messages don't read scheduled.
OPENERS = (
    "Fakta random hari ini:",
    "Tau gak...",
    "Hal kecil yang jarang disadari:",
    "Nemu hal seru nih:",
    "Baru kepikiran nih:",
    "Fakta kecil yang keren:",
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

    def send_html(self, text: str) -> bool:
        """Pretty HTML preview: unescape entities, show bold visually."""
        import html as _html
        mirror = _html.unescape(text or "")
        mirror = re.sub(r"</?b>", "*", mirror)
        return self.send_message(mirror)

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


def _split_text(text: str, limit: int) -> List[str]:
    """Word-boundary chunking that never exceeds `limit` (no mid-word cuts;
    a single monster word over the limit gets hard-cut). Order preserved."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current = ""
    for word in text.split(" "):
        candidate = current or word
        if current:
            candidate = f"{current} {word}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = word if len(word) <= limit else ""
        if len(word) > limit:
            while len(word) > limit:  # monster word: hard cut it apart
                chunks.append(word[:limit])
                word = word[limit:]
            current = word
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def _deliver(telegram: Any, image_finder: Any, message: str,
             content: Dict[str, Any], dry_run: bool = False) -> bool:
    """Text-or-photo delivery with the Telegram hard limits (ticket T6).

    - photo when the finder yields one: ≤1024 chars go out as the caption;
      a longer story becomes photo (first chunk) + the remainder as separate
      text messages, order preserved;
    - plain text over 4096 chars → sequential ordered chunks under the limit;
    - HTML-capable ports receive the polished rendering (`plain_to_html`:
      bold paragraphs), plain-only fakes the raw text — unchanged history;
    - every send failure surfaces as a False return (the run stays green).
    """
    html_sender = getattr(telegram, "send_html", None)

    def _send_text(chunk: str) -> bool:
        if html_sender is not None:
            try:
                return bool(html_sender(tg_format.plain_to_html(chunk)))
            except Exception:
                # render/HTML glitch: degrade to raw text, never silent
                return bool(telegram.send_message(chunk))
        return bool(telegram.send_message(chunk))

    if image_finder is not None and not dry_run:
        photo = image_finder.find(content)
        photo_sender = getattr(telegram, "send_photo", None)
        if photo and photo_sender is not None:
            caption, remainder = message, ""
            if len(message) > PHOTO_CAPTION_LIMIT:
                parts = _split_text(message, PHOTO_CAPTION_LIMIT)
                caption = parts[0]
                remainder = " ".join(parts[1:])
            if bool(photo_sender(photo["url"],
                                 caption=tg_format.plain_text(caption))):
                if remainder:
                    # code-review fix: the remainder has its own hard limit —
                    # chunk it (in order) like any other oversized text
                    try:
                        for part in _split_text(remainder, TEXT_LIMIT):
                            if not _send_text(part):
                                print("photo tail message failed",
                                      file=sys.stderr)
                                return False
                        return True
                    except Exception as exc:  # photo landed, tail failed
                        print(f"photo tail message failed: {exc}",
                              file=sys.stderr)
                        return False
                return True
            print("sendPhoto failed; falling back to plain text",
                  file=sys.stderr)
        elif photo is None:
            print("no image found for this topic", file=sys.stderr)

    for chunk in _split_text(message, TEXT_LIMIT):
        if not _send_text(chunk):
            return False
    return True


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
        dedupe_threshold: float = DEFAULT_DEDUPE_THRESHOLD,
        polisher: Optional[Any] = None,
        image_finder: Optional[Any] = None) -> RunResult:
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
        # weighted candidate list and retry against BOTH failure kinds —
        # a fetch that raises (typed SourceError, network hiccup, dead API)
        # and a near-duplicate candidate (ticket T3 gate) each throw the
        # pick back for a different source, up to DEFAULT_DEDUPE_ATTEMPTS
        # tries; after that the best evidence in hand decides: a duplicate
        # is sent anyway, while a still-failing pipeline lets the caller
        # map it to a green skip (`source-fetch-failed` /
        # `on-demand-pipeline-failed`). One flaky source can no longer
        # silence a whole run when other sources are healthy.
        candidates = sources if sources is not None else [(source, 100.0)]
        history_snapshot = [] if dry_run else state.load_history()
        content: Optional[Dict[str, Any]] = None
        last_error: Optional[Exception] = None
        for attempt in range(1, DEFAULT_DEDUPE_ATTEMPTS + 1):
            chosen = pick_source(candidates, rng)
            try:
                candidate = chosen.fetch()
            except Exception as exc:
                last_error = exc
                if attempt >= DEFAULT_DEDUPE_ATTEMPTS:
                    raise
                print(f"source fetch failed (attempt {attempt}), "
                      "trying another source: " + str(exc), file=sys.stderr)
                continue
            if not isinstance(candidate, dict) or not (candidate.get("body") or "").strip():
                last_error = SourceError(
                    "content source returned an unusable/malformed result")
                print(str(last_error) + f" (attempt {attempt}), "
                      "trying another source", file=sys.stderr)
                continue
            if (attempt >= DEFAULT_DEDUPE_ATTEMPTS
                    or not dedupe.is_duplicate(candidate, history_snapshot,
                                               dedupe_threshold)):
                content = candidate
                break
            print(f"candidate looks like a recent topic (attempt {attempt}), "
                  "re-picking a different source", file=sys.stderr)
        if content is None:
            raise (last_error if last_error is not None
                   else SourceError("no usable content after all attempts"))

        # Voice pass (ticket T5): when a polisher is wired, the raw body is
        # rewritten in casual Indonesian — any Groq failure inside the
        # polisher degrades to the raw body, the send still survives.
        if polisher is not None:
            content = {**content,
                       "body": polisher.polish(content)}
        message = format_message(content, rng)

        # Spoilers (ticket T5): ~10% of sends wrap the payload in a tap-to-
        # reveal spoiler — but ONLY ports that can render HTML roll for it,
        # so test fakes keep their exact single-consumption rng contract.
        # Chunks are measured post-escaping (4096-after-parsing cap).
        send_ok = False
        html_sender = getattr(telegram, "send_html", None)
        if html_sender is not None and not dry_run and \
                rng.random() < tg_format.SPOILER_CHANCE:
            sent_parts = 0
            chunks: List[str] = []
            try:
                chunks = tg_format.split_for_html(message, TEXT_LIMIT)
                for chunk in chunks:
                    if bool(html_sender(tg_format.spoiler_html(
                            tg_format.plain_to_html(chunk)))):
                        sent_parts += 1
                    else:
                        break
            except Exception as exc:
                print(f"spoiler send failed: {exc}", file=sys.stderr)
            if chunks and sent_parts == len(chunks):
                send_ok = True
            elif sent_parts > 0:
                # partial delivery: re-sending plain text would duplicate
                # the already-delivered chunks on the user's screen
                print(f"spoiler partially delivered ({sent_parts}/{len(chunks)}); "
                      "not falling back to plain", file=sys.stderr)
                return message, False
            # sent_parts == 0: nothing went out, plain deliver below handles it
        if not send_ok:
            try:
                send_ok = _deliver(telegram, image_finder, message, content,
                                   dry_run)
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
    import ai_generated_source  # noqa: E402  (T5 slot, used above when keyed)
    from wikipedia_source import WikipediaSource
    from reddit_source import RedditSource
    from trivia_source import TriviaSource
    from numbers_source import NumbersSource
    from source_pool import build_pool

    # Ticket T4 desired mix (weights by spec section 5.2); slot entries that
    # are None (unconfigured/absent) get their weight redistributed by the
    # pool builder — the bot never mentions its plumbing.
    groq = None
    polisher = None
    if os.environ.get("GROQ_API_KEY"):
        # Ticket T5: one shared Groq client powers BOTH the voice pass and
        # the AI-generated weird-question slot. Failures inside either
        # degrade gracefully (raw text / slot dropped), never raise out.
        from groq_client import GroqClient
        from polish import Polisher
        from ai_generated_source import AIGeneratedSource
        groq = GroqClient()
        polisher = Polisher(groq)

    from image import ImageFinder
    image_finder = ImageFinder()  # disabled internally without a key

    desired_mix = [
        (WikipediaSource(), 35.0),
        (RedditSource(
            user_agent=os.environ.get(
                "REDDIT_USER_AGENT",
                "bot-fakta-random/1.0 (personal Telegram bot, "
                "github.com/IrfanFalz/bot-fakta-random)")), 25.0),
        (TriviaSource(), 20.0),
        (NumbersSource(), 10.0),
        # AI-generated weird-question slot (ticket T5): present only with a
        # Groq key; its weight is redistributed when absent (source_pool).
        (ai_generated_source.AIGeneratedSource(groq), 10.0) if groq else (None, 10.0),
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
        polisher=polisher,
        image_finder=image_finder,
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
