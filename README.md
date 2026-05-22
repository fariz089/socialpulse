# Slaytics — Social Media Monitoring + JARVIS

Multi-monitor command center untuk monitoring social media (Instagram, TikTok, Twitter, Facebook, YouTube, **Threads**, News) dengan AI agent ber-voice yang jalan 24/7. Fully local, zero dependency Apify.

## Yang ada di project ini

- **6-monitor command center** — Dashboard, Analysis (Sentiment), Mentions, SNA Graph, **Locations Map (Leaflet)**, AI Chat
- **JARVIS AI agent** — 13 tool, voice input, text-to-speech, wake word "jarvis", auto-greeting
- **PC Scraper** — Instagram, Facebook, Twitter, YouTube, Threads, News (port android-scraper, jalan di Docker)
- **TikTok-PC service** — TikTok scraping via Playwright (Chromium)
- **24/7 Scheduler** — auto-scrape semua project tiap N menit (default 60), hot-reload tanpa restart
- **4 Alert Detector** — negative spike, volume spike, viral velocity, crisis keyword
- **Multi-channel Notifier** — Telegram + Discord + WebSocket banner
- **Daily Briefing** — otomatis tiap pagi jam 7 WIB via Telegram
- **Settings UI** — semua konfigurasi runtime di web, tanpa restart container

---

# SETUP DARI NOL — di Docker Desktop

## 1. Prasyarat

- **Docker Desktop** terinstall + running. Download: https://www.docker.com/products/docker-desktop/
- **RAM minimum 6GB dialokasikan ke Docker** (Settings → Resources → Memory). TikTok service butuh Chromium yang berat.
- **OpenRouter API key** (untuk JARVIS) — daftar di https://openrouter.ai/, top up minimal $5.

## 2. Extract project + isi `.env`

```bash
cd /path/ke/socialpulse
cp .env.example .env       # Mac/Linux
# atau:  Copy-Item .env.example .env       (Windows PowerShell)
```

Edit `.env`. **Cuma 5 hal yang wajib** (sisanya nanti di UI):

```bash
MONGO_USERNAME=slaytics_admin
MONGO_PASSWORD=password_kuat_kamu_disini      # ganti dari default

JWT_SECRET=string_random_64_karakter          # generate: openssl rand -hex 32

API_PORT=3001
WEB_PORT=80
```

> Untuk JWT_SECRET: di Mac/Linux jalankan `openssl rand -hex 32`. Di Windows buka https://generate-secret.vercel.app/64 dan copy.

**Itu saja.** OpenRouter, Telegram, Discord, scheduler interval, alert thresholds — semua atur lewat UI nanti.

## 3. Build & run

```bash
docker compose up -d --build
```

First build ~3-5 menit. Cek di Docker Desktop, harus ada 5 container running:
- `slaytics-mongo` (port 27017)
- `slaytics-api` (port 3001)
- `slaytics-web` (port 80)
- `slaytics-pc-scraper` (port 5005)
- `slaytics-tiktok-pc` (port 5006)

## 4. Buka aplikasi & register akun

Buka browser: **http://localhost** → register akun pertama → login.

## 5. Konfigurasi via UI Settings

Klik **Settings** di sidebar. Ada 6 section:

### A. AI Agent (JARVIS)
- **OpenRouter API Key** — paste key dari openrouter.ai
- **Model** — pilih dari dropdown (Gemini 2.5 Flash recommended untuk start)
- Klik tombol 🧪 di sebelahnya untuk test

### B. Scheduler 24/7
- **Status** — Aktif / Mati
- **Interval (menit)** — berapa lama antar cycle scrape. Bisa 1 menit literal kalau ngotot, tapi UI akan kasih warning kalau < 5 menit (resiko rate-limit)
- **Max post/platform** — limit per scrape

> Perubahan interval **hot-reload**: scheduler akan restart pakai value baru tanpa perlu restart container.

### C. Alert Thresholds
- **Negative spike %** — trigger alert kalau sentimen negatif > nilai ini (default 30)
- **Volume multiplier** — trigger kalau volume > X× rata-rata baseline (default 3)
- **Cooldown (menit)** — minimum jeda antar alert sejenis (default 120)

### D. Telegram
- Setup bot: Chat **@BotFather** di Telegram → `/newbot` → kasih nama → catat token
- Chat bot kamu sekali (kirim apa aja), lalu buka `https://api.telegram.org/bot<TOKEN>/getUpdates` → cari `"chat":{"id":XXX}`
- Isi **Bot Token** + **Chat ID** di Settings → klik 🧪 untuk test (Telegram harus bunyi)

### E. Discord (opsional)
- Server settings → Integrations → Webhooks → New Webhook → Copy URL
- Paste di Settings → klik 🧪 untuk test

### F. Scraper Override (opsional)
- Kosongkan untuk default (pakai pc-scraper Docker internal)
- Isi kalau punya scraper di host/HP lain

Klik **💾 Simpan Semua** di bawah.

## 6. Setup akun scraper per platform

Tanpa akun aktif per platform, scraper return `no_active_accounts`. Setup di **Akun Scraper** di sidebar, atau lewat curl:

```bash
# Instagram (cookie-based v3 — pakai storage_state. RECOMMENDED: pakai
#   multi_capture untuk capture session lengkap dari Chromium real.
#   Manual: copy sessionid + ds_user_id dari DevTools Application > Cookies.)
curl -X POST http://localhost:5005/accounts \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"instagram\",\"username\":\"akun_kamu\",\"password\":\"{\\\"sessionid\\\":\\\"xxx%3Axxx\\\",\\\"ds_user_id\\\":\\\"xxx\\\",\\\"csrftoken\\\":\\\"xxx\\\"}\"}"

# TikTok (cookie-based — paling reliable: pakai multi_capture untuk capture
#   storage_state lengkap dari Chromium real, lalu auto-push ke endpoint ini.
#   Manual: paste header Cookie dari DevTools Network tab.)
curl -X POST http://localhost:5005/accounts \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"tiktok\",\"username\":\"akun_kamu\",\"password\":\"{\\\"sessionid\\\":\\\"xxx\\\",\\\"ms_token\\\":\\\"xxx\\\"}\"}"

# Facebook (cookie-based — sama, recommended pakai multi_capture buat dapat
#   storage_state lengkap dengan localStorage + atribut cookies.)
curl -X POST http://localhost:5005/accounts \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"facebook\",\"username\":\"akun_kamu\",\"password\":\"{\\\"c_user\\\":\\\"xxx\\\",\\\"xs\\\":\\\"xxx\\\"}\"}"

# YouTube (anonymous untuk public videos)
curl -X POST http://localhost:5005/accounts \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"youtube\",\"username\":\"anon1\",\"password\":\"anonymous\"}"

# Twitter / X (cookie-based v3 — pakai storage_state. RECOMMENDED: multi_capture.
#   Manual: copy auth_token + ct0 dari DevTools Application > Cookies > .x.com)
curl -X POST http://localhost:5005/accounts \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"twitter\",\"username\":\"akun_kamu\",\"password\":\"{\\\"auth_token\\\":\\\"xxx\\\",\\\"ct0\\\":\\\"xxx\\\",\\\"twid\\\":\\\"u%3Dxxx\\\"}\"}"

# Threads (cookie-based v3.1 — share auth dengan Instagram. Cookies wajib sama
#   dengan IG: sessionid + ds_user_id. RECOMMENDED: pakai multi_capture pilih
#   platform Threads — login Threads otomatis redirect ke IG, capture lengkap
#   dari kedua domain (.threads.net + .instagram.com))
curl -X POST http://localhost:5005/accounts \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"threads\",\"username\":\"akun_kamu\",\"password\":\"{\\\"sessionid\\\":\\\"xxx\\\",\\\"ds_user_id\\\":\\\"xxx\\\"}\"}"
```

> **Tips capture cookie SEMUA platform (RECOMMENDED):** Pakai sister-tool [`multi_capture`](../multi_capture) — login manual sekali di Chromium real, klik tombol Push, otomatis ter-add di SocialPulse dengan storage_state lengkap (cookies + localStorage + atribut). Sejak v3 (7 Mei 2026) keempat platform (FB, TikTok, IG, Twitter) sudah cookie-based, dan sejak v3.1 (13 Mei 2026) Threads juga ikut — flow capture-to-scrape jadi konsisten untuk semua platform Meta + TikTok + X.
>
> **Manual fallback:** login di Chrome incognito, F12 → Application → Cookies → copy nilai cookie wajib per platform (lihat curl example di atas). Endpoint `/accounts` auto-detect format input (storage_state JSON, flat dict JSON, header Cookie string, tab-separated paste).

> **Migrasi dari versi lama:** Akun IG & Twitter yang sebelumnya di-add via user/pass (instagrapi/twikit) **tidak jalan lagi** di v3. Hapus akun lama via UI → re-add lewat multi_capture. Akun FB & TikTok yang sudah ada tetap jalan tanpa perubahan.

## 7. Buat project

UI → **+ New Project** → nama (misal "Brand X Monitoring") → keyword (`brand-x, #brandx`) → pilih platform → save.

## 8. Buka 6-monitor view

Klik **Command Center** di sidebar → pilih project → klik tombol monitor (M1-M6).

Default mapping:
- **M1 Dashboard** — overview metrics
- **M2 Analysis** — sentiment chart + analytics
- **M3 Mentions** — feed post terbaru
- **M4 SNA Graph** — social network analysis
- **M5 Locations** — peta Leaflet kota mention (interactive)
- **M6 AI Chat** — JARVIS interface dengan voice + alert banner

> M1-M5 reuse page utama dengan fullscreen mode (sidebar+topbar di-hide). Semua future view yang ditambah ke page utama otomatis bisa jadi monitor option. M6 dedicated karena unique (voice mode, news/viral panel, alert banner).

Untuk auto-arrange 6 monitor di Windows (2×3 layout): jalankan `launchers/slaytics-command-center.bat`.

Di **Monitor 6 (AI Chat)**, JARVIS otomatis sapa kamu. Coba command:
- "tampilkan brand-x"
- "ada negatif apa hari ini?"
- "dari daerah mana mention paling banyak?"
- "briefing pagi"

Klik 🎙️ untuk hands-free voice mode — panggil "Jarvis" diikuti perintah.

---

# Yang Auto-Realtime di Sistem Ini

| Aspek | Behavior |
|---|---|
| Monitor M1-M6 buka | Auto-load data project langsung dari API |
| Scheduler tiap N menit | Auto-scrape semua project, monitor auto-refresh |
| Alert detector | Tiap selesai cycle, fire ke Telegram/Discord/banner |
| JARVIS chat | On-demand scrape + broadcast realtime ke 6 monitor |
| Daily briefing | Otomatis jam 7 pagi WIB via Telegram |
| Settings change | Hot-reload — scheduler restart, no container restart needed |

---

# Troubleshooting

**Build pc-scraper gagal**
```bash
docker compose logs pc-scraper
docker system prune -a    # kalau lxml error
docker compose up -d --build
```

**`[scheduler] no_active_accounts`** — belum setup akun (Step 6).

**Telegram tidak bunyi** — cek di UI Settings, klik tombol 🧪 di sebelah Chat ID. Kalau gagal, error message akan keluar.

**Container tiktok-pc crash random** — naikkan RAM Docker Desktop ke 8GB. Chromium butuh shared memory besar.

**Alert kebanyakan** — Settings → Alert Thresholds → naikkan Negative spike ke 40, Volume multiplier ke 5, Cooldown ke 240. Save. Hot-reload jalan.

**Alert kekurangan** — Settings → turunkan threshold. Atau pastikan ada cukup data: scheduler perlu beberapa cycle untuk bangun baseline.

**IP banned platform (IG/FB)** — restart router untuk refresh DHCP. IP rumah Indihome/FirstMedia/Biznet biasanya aman. Kalau sering kena, pasang residential proxy (BrightData, IPRoyal).

**JARVIS gak ngomong (TTS)** — browser block auto-play sampai ada user interaction. Klik di halaman sekali. Greeting jalan di refresh berikutnya.

**Voice mode miss wake word** — bilang "jarvis" + perintah dalam satu napas tanpa jeda lama. Akurasi Web Speech API browser memang terbatas.

**Mau update kode**
```bash
docker compose down
# edit file
docker compose up -d --build
```

**Reset semua data (WARNING — hapus database!)**
```bash
docker compose down -v
docker compose up -d --build
```

---

# Struktur Project

```
socialpulse/
├── docker-compose.yml          # Orkestrasi 5 service
├── .env                        # 5 variabel saja (jangan commit ke git)
├── .env.example                # Template
├── README.md                   # File ini
│
├── backend/                    # Node.js API + AI agent + scheduler
│   ├── server.js
│   ├── agent.js                # JARVIS dengan 13 tool
│   ├── scheduler.js            # 24/7 cron + hot-reload
│   ├── alerts.js               # 4 detector
│   ├── notifier.js             # Telegram + Discord
│   ├── config.js               # Unified DB-backed config
│   └── Dockerfile
│
├── frontend/                   # Web UI + 6 monitor
│   ├── index.html              # Dashboard utama + Settings UI + monitor M1-M5 (fullscreen mode)
│   ├── command-center/
│   │   ├── ai-chat.html        # M6 (JARVIS UI standalone)
│   │   ├── cc-common.css
│   │   └── cc-common.js
│   └── Dockerfile
│
├── pc-scraper/                 # IG/FB/YT/Twitter/News (Python)
│   ├── app.py
│   ├── scrapers/
│   └── Dockerfile
│
├── tiktok-pc/                  # TikTok (Playwright)
│   ├── app.py
│   └── Dockerfile
│
└── launchers/                  # Windows .bat untuk auto-arrange 6 monitor
    ├── slaytics-command-center.bat
    └── slaytics-shutdown.bat
```

---

# Endpoint Utama

- **Frontend:** http://localhost
- **API:** http://localhost:3001
- **PC Scraper:** http://localhost:5005
- **TikTok-PC:** http://localhost:5006

**API yang sering dipakai:**
- `GET /api/settings/all` — get semua setting (sensitive masked)
- `PUT /api/settings/all` — bulk update setting
- `POST /api/settings/test/{telegram,discord,openrouter}` — test credential
- `POST /api/scheduler/run-now` — trigger 1 cycle manual
- `GET /api/scheduler/status` — status scheduler
- `POST /api/agent/command` — kirim perintah ke JARVIS
- `GET /api/monitor/data?project_id=X` — data lengkap untuk monitor

---

Selamat menggunakan, Bos. Semua di UI, tidak perlu utak-atik file lagi.
