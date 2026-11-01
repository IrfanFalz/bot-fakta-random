# PRD & Arsitektur: Bot Telegram Fakta/Ilmu Random

*Dokumen ini ditulis untuk dikonsumsi oleh AI coding agent (Claude Code, Cursor, dsb) sebagai acuan implementasi penuh.*

## 1. Ringkasan Produk

Bot Telegram personal yang mengirim pesan berisi fakta, jawaban atas pertanyaan unik/nyeleneh, atau ilmu pengetahuan dari kategori apapun (sains, alam, hewan, sejarah, psikologi, makanan, astronomi, trivia aneh, dll) secara **tidak terjadwal** — waktu kirim acak, topik acak, format acak. Tujuannya meniru sensasi "nemu hal menarik secara tidak sengaja", bukan newsletter terjadwal yang predictable.

Semua komponen menggunakan layanan gratis (tanpa kartu kredit, tanpa biaya bulanan).

## 2. Tujuan & Non-Tujuan

**Tujuan:**

- Mengirim 1 pesan "pengetahuan" ke chat Telegram tertentu pada interval waktu yang tidak dapat diprediksi pengguna.
- Variasi topik seluas mungkin, tanpa kategori kaku yang dirotasi berurutan.
- Variasi format pesan (fakta singkat, jawaban pertanyaan nyeleneh, mitos vs fakta, cerita asal-usul, dst).
- Menghindari pengulangan topik dalam jangka waktu tertentu.
- Mendukung command on-demand (`/lagi`) untuk meminta 1 fakta random kapan saja.
- Zero biaya operasional.

**Non-tujuan (di luar scope v1):**

- Multi-user/multi-tenant (v1 hanya untuk 1 chat ID pribadi).
- Dashboard web atau UI admin.
- Personalisasi berbasis machine learning terhadap preferensi user.

## 3. Arsitektur Tingkat Tinggi

```
[GitHub Actions Scheduler] --(cron tiap 15 menit)--> [Python Script: decide_and_send.py]
                                                              |
                              +-------------------------------+-------------------------------+
                              |                                |                               |
                     [Probabilitas Kirim?]            [Pilih Sumber Konten]           [Cek Histori Topik]
                     (misal 5-8% tiap cek)             (Wikipedia/Reddit/                (history.json di repo)
                              |                         OpenTDB/NumbersAPI)                      |
                              v                                |                               |
                      Jika YA -> lanjut <----------------------+-------------------------------+
                              |
                              v
                   [Polishing Bahasa via Groq/Gemini API] (opsional, fallback ke raw text)
                              |
                              v
                   [Ambil Gambar via Unsplash API] (opsional)
                              |
                              v
                   [Kirim ke Telegram Bot API] (sendMessage / sendPhoto)
                              |
                              v
                   [Update history.json + commit ke repo]
```

Seluruh sistem **stateless di luar GitHub repo** — tidak ada server yang harus hidup 24/7. GitHub Actions berperan sebagai scheduler sekaligus compute, dan repo itu sendiri berperan sebagai database ringan (file JSON).

## 4. Komponen & Teknologi

| Komponen | Pilihan | Alasan |
| --- | --- | --- |
| Bot messaging | Telegram Bot API (langsung via `requests`, tanpa framework berat) | Gratis, simpel, tidak perlu polling 24/7 karena hanya kirim (push), command `/lagi` ditangani via webhook GitHub Actions terpisah atau polling ringan |
| Scheduler | GitHub Actions (`schedule` cron) | Gratis unlimited untuk repo publik, tidak perlu hosting |
| Bahasa | Python 3.11+ | Ekosistem library matang, mudah di-maintain AI agent |
| Sumber konten utama | Wikipedia REST API (`/page/random/summary`) | Gratis, unlimited, sudah termasuk ringkasan + gambar |
| Sumber konten sekunder | Reddit API (read-only, via `praw` atau requests ke `.json` endpoint publik) | Pertanyaan nyeleneh dari r/NoStupidQuestions, r/todayilearned, r/AskScienceFiction |
| Sumber trivia | Open Trivia DB (opentdb.com) | Gratis, kategori luas |
| Sumber fakta angka | Numbers API (numbersapi.com) | Variasi tambahan |
| Polishing bahasa (opsional) | Groq API (model Llama 3.x, free tier) atau Gemini Flash free tier | Supaya nada bahasa konsisten santai, bukan ensiklopedia kaku |
| Gambar | Unsplash API (free tier 50 req/jam) | Gambar pendukung sesuai keyword topik |
| Penyimpanan histori | `history.json` di repo, di-commit otomatis oleh GitHub Actions | Tidak perlu database eksternal |
| Secrets | GitHub Actions Secrets | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`, `UNSPLASH_ACCESS_KEY` |

## 5. Alur Kerja Detail

### 5.1 Trigger & Probabilitas Waktu

- GitHub Actions workflow berjalan tiap 15 menit (`*/15 * * * *`) — catatan: GitHub Actions cron punya keterlambatan natural beberapa menit, ini justru menambah unpredictability.
- Setiap eksekusi, script melakukan roll probabilitas (misal `random.random() < 0.06` -> sekitar 6% peluang kirim tiap cek, menghasilkan rata-rata \~1-2 pesan/hari dengan jarak waktu tidak beraturan).
- Tambahkan **cooldown minimum** (misal tidak kirim lagi jika pesan terakhir < 90 menit lalu) agar tidak ada 2 pesan beruntun terlalu dekat kecuali memang by design sesekali (mini-thread).
- Simpan `last_sent_timestamp` di `state.json` untuk cek cooldown.

### 5.2 Pemilihan Sumber Konten

Pilih sumber secara random tiap eksekusi (weighted random, bukan round-robin):

- 35% Wikipedia random summary
- 25% Reddit (ambil top/hot post dari subreddit pertanyaan unik, parsing judul + top comment sebagai "jawaban")
- 20% Open Trivia DB
- 10% Numbers API
- 10% Generated pertanyaan nyeleneh via prompt AI terbuka (jika Groq/Gemini tersedia)

### 5.3 Pengecekan Duplikasi

- `history.json` menyimpan 50 entri terakhir: `{title, keywords[], source, timestamp}`.
- Sebelum kirim, bandingkan judul/keyword baru dengan histori menggunakan simple string similarity (misal `difflib.SequenceMatcher`) — jika similarity > 0.6, buang dan coba sumber/topik lain (maks 3 percobaan ulang, lalu kirim apa adanya jika tetap gagal dapat yang unik).

### 5.4 Polishing Bahasa (opsional, fallback-safe)

- Jika `GROQ_API_KEY` tersedia: kirim teks mentah ke Groq dengan prompt yang menginstruksikan gaya santai, storytelling, 150-400 kata, tanpa heading, bahasa Indonesia casual.
- Jika API gagal/limit habis: kirim teks asli dari sumber (Wikipedia summary dkk) apa adanya, tetap jalan tanpa AI.

### 5.5 Pengambilan Gambar (opsional)

- Ambil keyword utama dari judul/topik, query ke Unsplash API.
- Jika gagal atau tidak relevan: kirim tanpa gambar (jangan blocking).

### 5.6 Format Pesan

Variasikan gaya pembuka secara random dari beberapa template, misal:

- "Tau gak kenapa..."
- "Fakta random hari ini:"
- "Pertanyaan random: ... Jawabannya ternyata..."
- "Hal kecil yang jarang disadari:"

Sesekali (misal 10% kemungkinan) kirim dalam format **spoiler** menggunakan Telegram `MarkdownV2` spoiler tag (`||teks||`) agar user harus tap dulu untuk reveal jawaban — menambah efek penasaran.

### 5.7 Command On-Demand `/lagi`

Karena GitHub Actions tidak cocok untuk long-polling realtime, command ini ditangani lewat salah satu dari dua opsi:

- **Opsi A (direkomendasikan, tetap gratis):** Telegram Webhook diarahkan ke GitHub Actions via `repository_dispatch` trigger menggunakan layanan perantara gratis seperti Cloudflare Workers (free tier) yang menerima webhook Telegram lalu memicu GitHub Actions API.
- **Opsi B (lebih simpel, sedikit delay):** Workflow kedua berjalan tiap 2-3 menit mengecek `getUpdates` Telegram API (polling ringan) untuk command baru, lalu proses sama seperti alur utama.

Untuk v1, gunakan **Opsi B** demi kesederhanaan implementasi oleh AI agent, dengan catatan delay respons 2-3 menit dapat diterima.

## 6. Struktur Data

### `history.json`

```json
[
  {
    "title": "Kenapa Gurita Punya Tiga Jantung",
    "keywords": ["gurita", "jantung", "biologi"],
    "source": "wikipedia",
    "timestamp": "2026-11-01T09:12:00Z"
  }
]
```

### `state.json`

```json
{
  "last_sent_timestamp": "2026-11-01T09:12:00Z",
  "last_update_id": 123456789
}
```

## 7. Struktur Repo

```
/bot-fakta-random
  /scripts
    decide_and_send.py      # logic utama: probabilitas, pilih sumber, kirim
    sources/
      wikipedia_source.py
      reddit_source.py
      trivia_source.py
      numbers_source.py
      ai_generated_source.py
    polish.py                # pemanggilan Groq/Gemini, dengan fallback
    image.py                 # pemanggilan Unsplash, dengan fallback
    telegram_client.py       # wrapper sendMessage, sendPhoto, getUpdates
    dedupe.py                # cek similarity vs history.json
  /data
    history.json
    state.json
  /.github/workflows
    send_fact.yml            # cron tiap 15 menit
    handle_commands.yml      # cron tiap 2-3 menit untuk /lagi
  requirements.txt
  README.md
```

## 8. Environment Variables / Secrets

| Nama | Wajib? | Keterangan |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Ya | Dari @BotFather |
| `TELEGRAM_CHAT_ID` | Ya | Chat ID tujuan pengiriman |
| `GROQ_API_KEY` | Opsional | Untuk polishing bahasa |
| `UNSPLASH_ACCESS_KEY` | Opsional | Untuk gambar pendukung |

## 9. GitHub Actions Workflow (ringkasan logic)

**`send_fact.yml`:**

1. Checkout repo.
2. Setup Python, install dependencies.
3. Jalankan `decide_and_send.py`.
4. Jika script menandai bahwa pesan terkirim, commit perubahan `history.json` dan `state.json` kembali ke repo (`git commit` + `git push` menggunakan `GITHUB_TOKEN` bawaan Actions).
5. Jadwal: `*/15 * * * *`.

**`handle_commands.yml`:**

1. Checkout repo.
2. Jalankan script cek `getUpdates` dengan `offset` dari `last_update_id`.
3. Jika ada command `/lagi`, trigger alur kirim fakta yang sama seperti `send_fact.yml` (reuse fungsi, bukan duplikasi kode) — bypass cooldown khusus untuk command ini.
4. Update `last_update_id`, commit balik ke repo.
5. Jadwal: `*/3 * * * *`.

## 10. Instruksi Khusus untuk AI Agent saat Implementasi

1. **Mulai dari `telegram_client.py`** — fungsi dasar `send_message`, `send_photo`, `get_updates` menggunakan `requests` langsung ke `https://api.telegram.org/bot<TOKEN>/<METHOD>`, tanpa library tambahan agar ringan.
2. **Setiap source module** (`wikipedia_source.py`, dll) harus mengembalikan struktur data seragam: `{title, body, keywords, source_name, image_query}` — agar `decide_and_send.py` tidak perlu tahu detail tiap sumber.
3. **Semua pemanggilan API eksternal harus dibungkus try/except dengan fallback aman** — jika satu sumber gagal, coba sumber lain, jangan sampai workflow gagal total (exit code tetap 0 kecuali error fatal di logic inti).
4. **Jangan hardcode kategori/rotasi berurutan** — gunakan `random.choices()` dengan weights sesuai poin 5.2, bukan round-robin index.
5. **Commit balik ke repo harus menggunakan bot identity Git** (`github-actions[bot]`) dan hanya commit jika ada perubahan (`git diff --quiet || git commit ...`) untuk menghindari commit kosong.
6. **Gaya bahasa dalam prompt AI polishing harus eksplisit diarahkan ke Bahasa Indonesia santai/casual**, bukan formal, dan panjang 150-400 kata, tanpa markdown heading berlebihan (karena akan dikirim sebagai pesan chat, bukan dokumen).
7. **Validasi panjang pesan Telegram** (maks 4096 karakter untuk `sendMessage`, caption foto maks 1024 karakter) — jika hasil polishing terlalu panjang untuk caption foto, kirim foto dan teks sebagai 2 pesan terpisah.
8. **Testing lokal**: sediakan mode `--dry-run` di `decide_and_send.py` yang mencetak hasil ke terminal tanpa benar-benar mengirim ke Telegram atau commit ke repo, agar AI agent/pengguna bisa verifikasi logic sebelum deploy.

## 11. Kriteria Penerimaan (Acceptance Criteria) v1

- [ ] Bot berhasil kirim minimal 1 pesan ke chat Telegram dalam 24 jam percobaan, dengan waktu kirim yang tidak mengikuti pola jadwal tetap (misal selalu jam X).
- [ ] Topik antar pesan dalam 10 pesan berturut-turut tidak ada yang duplikat/sangat mirip.
- [ ] Command `/lagi` berhasil direspons dalam <5 menit sejak dikirim user.
- [ ] Jika Groq/Unsplash API gagal atau tidak dikonfigurasi, bot tetap berfungsi mengirim fakta (fallback bekerja).
- [ ] Tidak ada biaya yang timbul dari seluruh komponen yang digunakan (semua dalam batas free tier).
- [ ] Riwayat topik tersimpan persisten di repo (tidak hilang antar eksekusi workflow).

## 12. Roadmap Lanjutan (opsional, di luar v1)

- Format "mini-thread" 2-3 pesan berurutan untuk 1 topik.
- Statistik topik favorit berdasarkan reaksi/balasan user (jika Telegram reaction API dimanfaatkan).
- Tambah sumber: NASA APOD API (astronomi harian), PubMed abstract acak (sains lebih berat), Atlas Obscura scraping (tempat/fenomena unik dunia).
