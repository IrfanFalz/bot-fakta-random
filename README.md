# About

**Bot Fakta Random (English name: `bot-fakta-random`)** — a personal Telegram bot that sends random facts, answers to weird questions and science nuggets to your chat on an **unpredictable schedule**, in casual Bahasa. Built 100% on free tiers, no credit card, no always-on server.

It runs on **GitHub Actions** (repo-as-database: state lives in `data/*.json`, committed back only when something is sent) and gets its `/lagi` (means "again") on-demand command via a tiny **Cloudflare Worker** webhook. Your laptop can be off forever after provisioning.

<!-- Architecture: see "PRD & Arsitektur — Bot Telegram Fakta Random.md" (§3) -->

## What it does per run (every 15 minutes)

```
poll /lagi updates (one cheap getUpdates)  → cooldown check → probability roll
   → pick source (weighted) → fetch → dedupe vs history → voice pass (Groq)
   → deliver (photo or text, length-split) → persist state (commit-if-changed)
```

Every step is fallback-safe: a broken source just means *another source*, a dead Groq means *raw text*, a missing Unsplash key means *no image*, and nothing above ever makes a run red.

## Content sources (weighted random, never a fixed rotation)

| Slot | Weight | Notes |
|---|---|---|
| Wikipedia random summary | 35% | the backbone, always available |
| Reddit weird questions (`NoStupidQuestions`, `todayilearned`, …) | 25% | public `.json` is often 403 from datacenter IPs → drops out when blocked, weight redistributed |
| Open Trivia DB | 20% | easy-mode trivia as Q&A |
| Numbers API | 10% | currently degraded (404s) → drops out when down |
| AI-generated weird question (Groq) | 10% | only when `GROQ_API_KEY` secret is set |

~10% of messages arrive as a **MarkdownV2 spoiler** (`||…||`) you tap to reveal.

## Repo layout

```
scripts/
  decide_and_send.py   # the single orchestrator seam (injected ports)
  telegram_client.py   # Bot API wrapper (sendMessage/sendPhoto/getUpdates)
  wikipedia_source.py &friends   # 5 sources, uniform contract
  source_pool.py       # weighted pick + redistribution of dead sources
  dedupe.py            # title similarity + keyword Jaccard vs recent history
  groq_client.py       # chat completions (gpt-oss-120b, reasoning_effort low)
  polish.py            # casual-Indonesian voice pass (fallback: raw text)
  ai_generated_source.py  # the weird-question slot
  image.py             # Unsplash search (disabled without a key)
  tg_format.py         # MarkdownV2 escaping + spoilers
  state_store.py       # data/history.json + data/state.json (PRD shape)
  tg_* / worker/       # Cloudflare Worker app for the /lagi webhook
data/                  # history.json (50-entry cap) + state.json
.github/workflows/bot.yml  # schedule */15 + repository_dispatch send-fact
tests/                 # orchestrator-seam pytest suite (no network anywhere)
```

## Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | Required | What it powers |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | delivery (from `@BotFather`) |
| `TELEGRAM_CHAT_ID` | yes | delivery |
| `GROQ_API_KEY` | optional | voice pass + weird-question slot |
| `UNSPLASH_ACCESS_KEY` | optional | photo posts |

The `/lagi` worker additionally needs its own Cloudflare secrets (`GITHUB_PAT` fine-grained, `Contents: r/w` on this repo only; `WEBHOOK_SECRET`). **`docs/SETUP_lagi.md` is the guided operator checklist** — follow it top to bottom once.

## Local verification (no laptop needed after provisioning)

```powershell
python -m pytest tests/ -q                          # 66 tests, zero network
python scripts/decide_and_send.py --dry-run         # prints the would-be message
```

Dry-run uses an offline placeholder source by design: it never touches the network or the data files.

## Tuning knobs (env vars / GitHub Actions variables)

| Knob | Default | Effect |
|---|---|---|
| `PROBABILITY` | `0.02` | send chance per 15-min check (≈1–2 msg/day) |
| `COOLDOWN_MINUTES` | `90` | minimum gap between scheduled messages |
| `DEDUPE_THRESHOLD` | `0.6` | similarity gate for "near-duplicate" topics |
| `SPOILER_CHANCE` (constant in `tg_format.py`) | `0.10` | how often messages are tap-to-reveal |
| Source weights (in `decide_and_send.py` main) | 35/25/20/10/10 | mix; dead slots auto-redistribute |

## Watching it live — no laptop involved

- **Actions tab** on this repo: every 15-min decision is a green (or skipped-quiet) run; forced `/lagi` runs show as `repository_dispatch`.
- `data/history.json` at any commit = the topic trail.
- Telegram-specific options and per-message behavior live in the pinned `tests/` suite and the PRD.

## Zero-cost statement

Every component sits inside a free tier with no card on file: GitHub Actions on a **public repo** (unlimited), Telegram Bot API (free), Cloudflare Workers (100k req/day), Wikipedia / Open Trivia DB / Numbers API (free), Groq (1,000 requests/day — the bot needs ~14), Unsplash (50/hour demo — the bot needs ~14). If a provider cruelly vanishes, the run degrades and adapts; nothing bills.

## Fix the slash command if it ever lags

`/lagi` responds within seconds after the worker is deployed; if the worker is down (or `deleteWebhook` was run), scheduled runs pick `/lagi` up within 15 minutes — same message, no bespoke code path. **Note:** these two paths are mutually exclusive by Telegram design — while the webhook is set, `getUpdates` from Actions is rejected with 409, so the poll fallback only works after `deleteWebhook` (see `docs/SETUP_lagi.md`).
