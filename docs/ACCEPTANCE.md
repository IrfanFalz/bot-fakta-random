# Acceptance checklist — PRD §11 (v1)

Verifikasi manual terhadap kriteria terima PRD. Yang tandai ✅ itu sudah
terbukti lewat test suite (66 test, semua lewat satu seam orkestrator);
yang ⬜ butuh live trial setelah provisioning.

## 1. Kirim ≥1 pesan dalam 24 jam, waktu kirim tidak berpola ⬜

Procedure (setelah provisioning selesai):

1. Jalankan di Actions: **Run workflow** → wait→ force via `--force-send`
   tidak diperlukan; cukup biarkan cron jalan.
2. Setelah 24 jam: bandingkan `data/history.json` timestamps antar commit.
   Expectasi: timestamps BUKAN kelipatan pola tetap satu jam (beda jeda).
3. Confidence tambahan: `PROBABILITY` default 0.02 × 96 run/day ≈ 1.9 hari
   → ~1-2 kirim per 24 jam (aritmetika diperbaiki di code-review: nilai
   lama 0.06 memberi ~5.8/hari, meleset dari target tiket). Kalau mau
   trial yang lebih pasti naikkan `PROBABILITY` ke `0.15` via repo
   variables untuk trial, kembalikan setelahnya.

## 2. 10 pesan berturut-turut tak ada near-duplicate ✅ (test) / ⬜ (live)

- Gate dedupe (threshold 0.6) sudah diuji di `tests/test_t3_dedupe.py`
  (semantik + budget retry).
- Live: cek 10 entry terakhir `data/history.json` — judulnya wajar bervariasi.
- Kalau ada yang mirip (misal dari sumber beda tapi satu artikel), naikkan
  `DEDUPE_THRESHOLD`? Tidak — `DEDUPE_THRESHOLD` LEBIH KECIL itu lebih ketat
  (threshold = batas "dianggap duplikat"). Turunkan ke 0.5 untuk trial.

## 3. `/lagi` direspons < 5 menit ⬜ (butuh worker live)

- Worker path: realtime (detik), cooldown di-bypass, offset selalu maju
  (test: `tests/test_lagi_on_demand.py`).
- Fallback path (worker mati): scheduled run memanen `/lagi` dalam ≤15 menit.
  **PENTING (catatan code-review):** selama webhook Bot API masih aktif
  (Worker sudah `setWebhook`), `getUpdates` dari Actions akan selalu 409 —
  fallback hanya bisa jalan kalau webhook dihapus (`deleteWebhook`) atau
  worker memang belum pernah `setWebhook`. Dua jalur tidak bisa jalan
  bersamaan; ini by-design dari Telegram, bukan bug bot.

## 4. Groq/Unsplash gagal atau tak di-set → bot tetap jalan ✅

- Polisher 429/key hilang → raw body (test `test_polish_failure_degrades_to_raw_body`).
- Image finder tanpa key → tanpa request; gagal fetch → text-only (tests).
- Dihapus secret-nya → build ulang? Tidak perlu: env kosong = fitur off.

## 5. Zero biaya ✅ (design audit) / ⬜ (opsi live)

- GitHub Actions unlimited di repo publik; repo ini publik.
- Groq 1,000 req/day >> kebutuhan (~14 polish + 2 slot AI per day).
- Unsplash demo 50/h >> ~14/day. Cloudflare 100k/day >> webhook hit/hour.
- Tak ada komponen berbayar di stack (lihat README "Zero-cost statement").

## 6. Riwayat persisten across workflow runs ✅

- `state_store.JsonStateStore` + commit-if-changed di `bot.yml`
  (test no-op run tak menulis file sama sekali; hanya kirim nyata yang
  menghasilkan commit data).
- Live: cek commit `data/state.json` di repo — hanya commit ketika kirim.

---

## Cara menjalankan sekali keduanya (sentence ringkas)

```powershell
python -m pytest tests/ -q     # semua berwarna hijau
python scripts/decide_and_send.py --dry-run    # preview, zero network & zero write
```

## Bila bermasalah? (troubleshooting maraton)

| Gejala | Cek |
|---|---|
| Run merah di Actions tab | Seharusnya tidak pernah: semua jalur egres di-swallow. Report issue |
| Bot kirim tapi tanpa polisher | `GROQ_API_KEY` belum di-set (fitur off, bukan rusak) |
| `/lagi` lambat | Worker path mati? cek `docs/SETUP_lagi.md` bagian verifikasi |
| Pesan selalu plain text, tak ada foto | `UNSPLASH_ACCESS_KEY` kosong atau query nyasar; cek `--dry-run` |
