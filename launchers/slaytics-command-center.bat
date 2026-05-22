@echo off
REM =================================================================
REM Slaytics Command Center Launcher — Windows
REM
REM Buka 6 jendela Chrome di posisi monitor 2x3 secara otomatis.
REM Edit BASE, KEYWORD, atau path Chrome di bawah sebelum jalan.
REM =================================================================

REM ─── EDIT VARIABEL DI BAWAH ───────────────────────────────────────

REM Path Chrome (default install location)
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"

REM URL backend Slaytics. Ganti localhost ke IP server kalau remote.
set BASE=http://localhost

REM Default keyword yang ditampilkan saat monitor open. Boleh kosong.
set KEYWORD=

REM Project ID untuk filter (kosongkan kalau pakai semua project)
set PROJECT_ID=

REM JWT token dari login (optional — kalau auth diaktifkan)
REM Cara cara: login ke Slaytics di browser, F12 → Console → ketik:
REM    localStorage.getItem('jwt')
REM Lalu copy nilainya (tanpa quote) ke sini.
set TOKEN=

REM Resolusi tiap monitor (asumsi semua sama 1920x1080)
set W=1920
set H=1080

REM ─── KOORDINAT GLOBAL WINDOWS UNTUK LAYOUT 2x3 ────────────────────
REM Kolom 0=kiri (x=0), kolom 1=kanan (x=W)
REM Baris 0=atas (y=0), baris 1=tengah (y=H), baris 2=bawah (y=2H)
REM
REM Kalau layout monitor di Windows berbeda, atur ulang via:
REM   Settings → System → Display → drag monitor → catat kordinat
REM
REM Layout default:
REM   M1 (kiri-atas)    M2 (kanan-atas)
REM   M3 (kiri-tengah)  M4 (kanan-tengah)
REM   M5 (kiri-bawah)   M6 (kanan-bawah)
REM ─────────────────────────────────────────────────────────────────

set COL_LEFT=0
set /a COL_RIGHT=%W%
set ROW_TOP=0
set /a ROW_MID=%H%
set /a ROW_BOT=%H% * 2

REM ─── BUILD QUERY STRING ──────────────────────────────────────────
set QS=
if not "%KEYWORD%"=="" set QS=%QS%^&keyword=%KEYWORD%
if not "%PROJECT_ID%"=="" set QS=%QS%^&project_id=%PROJECT_ID%
if not "%TOKEN%"=="" set QS=%QS%^&token=%TOKEN%

REM ─── LAUNCH 6 WINDOWS ────────────────────────────────────────────

echo.
echo === Slaytics Command Center ===
echo Base: %BASE%
if not "%KEYWORD%"=="" echo Keyword: %KEYWORD%
echo.
echo Opening 6 monitors...

start "" %CHROME% --new-window --window-position=%COL_LEFT%,%ROW_TOP% --window-size=%W%,%H% --app="%BASE%/command-center/dashboard.html?monitor_id=1%QS%"
timeout /t 1 >nul
start "" %CHROME% --new-window --window-position=%COL_RIGHT%,%ROW_TOP% --window-size=%W%,%H% --app="%BASE%/command-center/sentiment.html?monitor_id=2%QS%"
timeout /t 1 >nul
start "" %CHROME% --new-window --window-position=%COL_LEFT%,%ROW_MID% --window-size=%W%,%H% --app="%BASE%/command-center/mentions.html?monitor_id=3%QS%"
timeout /t 1 >nul
start "" %CHROME% --new-window --window-position=%COL_RIGHT%,%ROW_MID% --window-size=%W%,%H% --app="%BASE%/command-center/sna.html?monitor_id=4%QS%"
timeout /t 1 >nul
start "" %CHROME% --new-window --window-position=%COL_LEFT%,%ROW_BOT% --window-size=%W%,%H% --app="%BASE%/command-center/influencers.html?monitor_id=5%QS%"
timeout /t 1 >nul
start "" %CHROME% --new-window --window-position=%COL_RIGHT%,%ROW_BOT% --window-size=%W%,%H% --app="%BASE%/command-center/ai-chat.html?monitor_id=6%QS%"

echo.
echo Slaytics Command Center launched on 6 monitors.
echo Untuk shutdown semua: jalankan slaytics-shutdown.bat
echo.
timeout /t 3 >nul
