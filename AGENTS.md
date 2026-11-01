# AGENTS.md — bot-fakta-random

Bot Telegram personal yang mengirim fakta/ilmu random secara tidak terjadwal, gratis 100%, tanpa server 24/7 (GitHub Actions + Cloudflare Worker). PRD dan arsitektur penuh ada di `PRD & Arsitektur — Bot Telegram Fakta Random.md` — baca itu sebagai acuan utama.

## Project constraints (wajib dipegang)

- **Personal use, 1 chat ID.** Tidak boleh ada biaya; semua komponen harus tetap dalam free tier.
- **Repo GitHub publik** — Actions unlimited; jangan pernah matiriskin secret eksplisit ke dalam file biasa (semua via GitHub Secrets).
- **Desktop user tidak perlu menyala** — semua runtime serverless: GitHub Actions cron + Cloudflare Worker.
- **Hemat Actions**: run harus early-exit cepat (±20 detik), `timeout-minutes: 2`, tanpa `pip install` (hanya stdlib + `requests` yang preinstalled), commit balik hanya saat pesan terkirim.

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues on this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles with default label strings. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` at repo root + `docs/adr/`. See `docs/agents/domain.md`.

## Language

Komunikasi dengan user dalam Bahasa Indonesia santai; konten bot dalam Bahasa Indonesia casual (bukan formal).
