# Setup `/lagi` (on-demand) — checklist buat manusia

Halo! Ini langkah setup yang **cuman bisa dilakuin kamu** (bukan bot, bukan
agent). Ikuti urut dari atas ke bawah, lalu tes di bagian paling bawah.
Semua gratis 100% — tidak ada kartu kredit, tidak ada server yang perlu
menyala.

Yang kita bangun:

```
kamu ketik /lagi di Telegram
  → Telegram kirim webhook ke Cloudflare Worker (realtime, < 1 menit)
    → Worker ngecek secret, tembak GitHub repository_dispatch
      → Actions jalanin bot (bypass cooldown) → fakta muncul di chat

Kalau worker lagi down: run scheduled tiap 15 menit bakal ngecek update
perlu /lagi juga (fallback lambat, tetap nyala).
```

---

## 1. Bikin bot di @BotFather → `TELEGRAM_BOT_TOKEN`

1. Buka Telegram, cari **@BotFather**, kirim `/newbot`.
2. Kasih nama & username (bebas, misal `FaktaRandomTester_bot`).
3. BotFather kasih token bentuknya `123456789:ABCdefGHI...`. **Salin token itu.**
4. Buka repo GitHub → **Settings → Secrets and variables → Actions →
   New repository secret**:
   - Name: `TELEGRAM_BOT_TOKEN`
   - Secret: (paste token tadi)

## 2. Dapetin chat ID kamu → `TELEGRAM_CHAT_ID`

1. Dari akun kamu, **kirim satu pesan apa aja ke bot** yang barusan dibuat
   (misal `halo`) — supaya chat-nya muncul di getUpdates.
2. Buka PowerShell di laptop, paste ini (ganti token):

   ```powershell
   $token = "123456789:ABCdefGHI..."
   (Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates").result |
     ForEach-Object { "$($_.update_id) => chat $($_.message.chat.id) [$($_.message.text)]" }
   ```

   Çare simpel alternatif: buka browser ke
   `https://api.telegram.org/bot<TOKEN>/getUpdates` dan lihat `chat.id`.
3. Cari baris chat_ID milikmu (yang `halo` tadi). **Salin angka chat id-nya.**
4. GitHub Secrets lagi: **New repository secret**
   - Name: `TELEGRAM_CHAT_ID`
   - Secret: (angka chat id, tulis angkanya doang)

## 3. Bikin GitHub PAT (fine-grained) buat si Worker

PAT ini **dipake worker** buat `POST /repos/{owner}/{repo}/dispatches`,
bukan buat Actions (Actions sudah pakai default `GITHUB_TOKEN`).

1. GitHub kanan atas → foto profil → **Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token**.
2. Isi:
   - **Repository access**: *Only select repositories* → pilih **cuma**
     `IrfanFalz/bot-fakta-random`.
   - **Permissions → Repository permissions → Contents**: *Read and write*
     (minimum yang dibutuhin buat dispatch; jangan kasih scope lain).
   - **Expiration**: pilih masa pendek, mis. **90 hari** — ini token
     personal-single-user, lebih aman pendek + di-roll. Catat tanggal
     kadaluarsanya di kalender/kamus; pas mau abis, buat lagi (ulangi
     langkah ikh 4 & 5 di bawah aja, sisanya gak perlu diulang).
3. Generate, **salin sekali** (setelah itu gak bisa dilihat lagi).
   Simpan di password manager.
4. Token ini akan jadi secret worker di langkah 5, ambil dari sana
   (dari password manager).

## 4. Deploy worker (paste di dashboard — gak perlu Node, gak perlu install)

1. Isi file JS yang bakal di-paste: ambil isi **`worker/src/worker.js`**
   dari repo ini (raw view: buka file itu di GitHub → Raw → copy semua).
2. Dasbor Cloudflare (dash.cloudflare.com, akun free plan) →
   **Workers & Pages → Create application → Start with Hello World**.
3. Kasih nama (mis. `bot-fakta-lagi`) → **Deploy** → klik **Edit code**
   (Quick Editor) → hapus stub → paste isi `worker/src/worker.js` →
   **Deploy** lagi.
4. Route worker-nya jadi `https://bot-fakta-lagi.<subdomain-kamu>.workers.dev`
   — **catat URL itu** (dipake di setWebhook nanti).

*(Alternatif nanti kalau mau deploys dari CI: ada juga jalur GitHub Action +
wrangler yang butuh `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`; skip
dulu — paste dashboard udah cukup dan sekali set-up aja.)*

## 5. Set secrets worker (di dashboard, bukan di kode)

Di worker **Settings → Variables & Secrets → Add variable**:

| Nama | Type | Value | Keterangan |
| --- | --- | --- | --- |
| `GITHUB_PAT` | **Secret** | (token GitHub dari langkah 3) | Dibaca kode `worker.js` |
| `WEBHOOK_SECRET` | Secret | string random | Hanya huruf `A-Z a-z 0-9 _ -` (aturan Bot API), min. 8 karakter, contoh: `fakta-secret-2026_k7`. **Simpan juga** dipisah, dipake di langkah 6. |
| `TELEGRAM_CHAT_ID` | Secret | (angka chat id dari langkah 2) | *Optional* tapi disarankan — worker auto-abai update dari chat lain |

**Gak sedekat...** jangan naruh secret di kode, di commit, atau di
`wrangler.toml`. Dashboard "Secret" = gak pernah keliatan lagi setelah
disave.

## 6. SetWebhook (sekali saja) — ngasih tau Telegram ke mana webhookmu

PowerShell (ganti token + URL worker + secret sama yang di langkah 5):

```powershell
$token  = "123456789:ABCdefGHI..."
$secret = "fakta-secret-2026_k7"
$url    = "https://bot-fakta-lagi.<subdomain>.workers.dev/"

Invoke-RestMethod -Method Post `
  -Uri "https://api.telegram.org/bot$token/setWebhook" `
  -ContentType "application/json" `
  -Body (@{ url = $url; secret_token = $secret } | ConvertTo-Json)
```

jawabannya: `{"ok":true,...}`.

### ⚠️ Catatan webhook vs fallback poll (penting, dari code-review)

Setelah `setWebhook` aktif, jalur **fallback poll di Actions tidak berfungsi** —
`getUpdates` dibatasi Telegram kalau webhook terset (error 409). Itu normal:

- Webhook aktif → `/lagi` realtime via Worker (jalur utama).
- Kalau worker bermasalah dan kamu mau balik ke fallback poll 15-menit,
  hapus webhook dulu: `Invoke-RestMethod -Method Post
  "https://api.telegram.org/bot$token/deleteWebhook"` — lalu beri silang
  catatan bahwa `/lagi` sementara via fallback.
- Balik lagi nanti: jalankan `setWebhook` dari langkah 6 sekali lagi.

Selama webhook terset dan worker down, `/lagi` tidak dibales scheduler
(update tidak bisa dibaca dari Actions). Rencana cadangan terbaik:
deploy ulang worker.

## 7. Tes end-to-end

1. (Opsional cek worker nyala) Buka URL worker di browser → harus muncul
   tulisan `bot-fakta webhook up`.
2. Cek webhook: `Invoke-RestMethod "https://api.telegram.org/bot$token/getWebhookInfo"`
   → `url` terisi, `last_error_message` kosong (kalau pernah salah secret,
   TLG nyatanya di sini).
3. Kirim `/lagi` ke bot di Telegram.
4. Harusnya:
   - Job baru muncul di tab **Actions** repo (`repository_dispatch: send-fact`).
   - Fakta nongol di chat kamu. Realtime-nya biasanya < 1–2 menit
     (spesifikasi: < 5 menit, jauh di bawah itu).
5. Kalau kirim `/lagi` dua-tiga-kali cepat, boleh dapat beberapa fakta
   (satu per perintah), tapi *cooldown gak dihitung* buat jalur ini.

**Catatan penting:** setelah setWebhook aktif, aplikasi bot gak lagi
ngambil update pakai `getUpdates` (aturan Telegram: webhook & polling
saling eksklusif). Itu normal — jalur *polling fallback* otomatis nyala
lagi setelah deleteWebhook (lihat rollback di bawah).

## 8. Rollback / matiin jalur realtime

Kapan pun (mis. worker error, mau maintenance):

```powershell
Invoke-RestMethod -Method Post `
  -Uri "https://api.telegram.org/bot$token/deleteWebhook" `
  -ContentType "application/json" -Body (@{ drop_pending_updates = $false } | ConvertTo-Json)
```

Setelah itu jalur realtime mati (worker masih jalan tapi gak terima update),
dan **jalur polling fallback** di run scheduled 15-menit otomatis yang
nge-nangkep `/lagi` — tetap dapet fakta on-demand, cuma lebih lambat
(maks. 15 menit + delay cron).

Untuk pasang lagi: ulangi langkah 6 (setWebhook).

---

## Cheatsheet urutan lengkap

- [ ] 1. BotFather → token → secret `TELEGRAM_BOT_TOKEN`
- [ ] 2. Chat ID (getUpdates) → secret `TELEGRAM_CHAT_ID`
- [ ] 3. Fine-grained PAT: Contents: read/write, cuma repo ini, expiry pendek → disimpan
- [ ] 4. Deploy worker (paste `worker/src/worker.js` di dashboard) → catat URL
- [ ] 5. Secrets worker: `GITHUB_PAT`, `WEBHOOK_SECRET` (+ optional `TELEGRAM_CHAT_ID`)
- [ ] 6. setWebhook (dengan `secret_token`)
- [ ] 7. Kirim `/lagi` → fakta nyampe di chat (< 5 menit) ✔
- [ ] 8. (Calamity) deleteWebhook buat kembali ke polling fallback
