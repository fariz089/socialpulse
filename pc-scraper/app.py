"""
Slaytics Multi-Platform Scraper Service
=========================================
Flask REST API yang jalan di HP Android (via Termux) untuk scrape:
  - Instagram  (cookie-based via Playwright, storage_state)
  - TikTok     (cookie-based, ms_token + sessionid)
  - Facebook   (cookie-based, c_user/xs)
  - Twitter/X  (cookie-based, auth_token/ct0)
  - Threads    (cookie-based, sessionid+ds_user_id — share auth dengan IG)
  - YouTube    (yt-dlp + cookies anonymous)
  - News ID    (Google News RSS + per-site fallback)

v3 (7 May 2026): Instagram & Twitter migrate dari instagrapi/twikit ke
cookie-based, mirror pattern Facebook & TikTok. Semua platform pakai
storage_state hasil capture dari multi_capture (sister tool).

v3.1 (13 May 2026): Tambah platform Threads. Threads share auth dengan
Instagram (sessionid + ds_user_id sama), tapi web app & API endpoint
sendiri di threads.net/api/graphql. Scraping via Playwright + GraphQL
response intercept, mirror pattern Twitter scraper.

Endpoints:
  POST /scrape
       body: { platform, keyword, max_results, mode?, video_url?, sites? }
       platform: instagram|tiktok|facebook|twitter|threads|youtube|news
       mode: 'videos' (default) | 'comments' (YouTube only, butuh `video_url`)
  
  GET  /health
       Status semua platform & akun aktif per platform.
  
  POST /accounts
       body: { platform, username, password, verification_code? }
       Untuk SEMUA platform cookie-based (IG/TikTok/FB/Twitter):
         password = storage_state JSON / flat dict / cookie string.
       Untuk YouTube: password = isi cookies.txt atau "anonymous".
       Untuk News: tidak perlu (publik).
  
  GET  /accounts?platform=...
       List akun (filter by platform optional).
  
  DELETE /accounts/<platform>/<username>
  
  POST /accounts/<platform>/<username>/reactivate

Backward compat:
  - Endpoint lama POST /scrape dengan platform='instagram' tetap jalan.
  - File accounts.json lama (flat dict) auto-migrate ke nested format.
  - Field `email` & `totp_secret` (legacy untuk twikit) di payload akan
    di-ignore. Akun Twitter lama yang dibuat dengan twikit harus re-add
    pakai cookies (multi_capture flow).
"""

import os
import time
import random
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict
from flask import Flask, request, jsonify

from scrapers.instagram_accounts import InstagramAccountManager
from scrapers.instagram_scraper import InstagramScraper
from scrapers.tiktok_accounts import TikTokAccountManager
from scrapers.tiktok_scraper import TikTokScraper
# Remote proxy: aktif kalau env TIKTOK_REMOTE_URL diset (point ke PC service)
from scrapers.tiktok_remote_proxy import (
    RemoteTikTokAccountManager,
    RemoteTikTokScraper,
)
from scrapers.facebook_accounts import FacebookAccountManager
from scrapers.facebook_scraper import FacebookScraper
from scrapers.youtube_accounts import YouTubeAccountManager
from scrapers.youtube_scraper import YouTubeScraper
from scrapers.twitter_accounts import TwitterAccountManager
from scrapers.twitter_scraper import TwitterScraper
from scrapers.threads_accounts import ThreadsAccountManager
from scrapers.threads_scraper import ThreadsScraper
from scrapers.news_scraper import NewsScraper

# ---- Setup ----
BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
ACCOUNTS_FILE = BASE_DIR / "accounts.json"
LOG_FILE = BASE_DIR / "scraper.log"

SESSIONS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# ---- Init managers per platform ----
# TikTok bisa pakai remote PC service kalau env TIKTOK_REMOTE_URL diset.
# Workflow add_account, list, scrape, delete, reactivate semuanya akan
# di-forward ke service PC — frontend Slaytics tidak perlu tau bedanya.
TIKTOK_REMOTE_URL = os.environ.get('TIKTOK_REMOTE_URL', '').strip()
if TIKTOK_REMOTE_URL:
    tiktok_manager = RemoteTikTokAccountManager(TIKTOK_REMOTE_URL)
    tiktok_scraper = RemoteTikTokScraper(tiktok_manager)
    logging.getLogger(__name__).info(
        f"  [tiktok] REMOTE mode → forwarding to {TIKTOK_REMOTE_URL}"
    )
else:
    tiktok_manager = TikTokAccountManager(ACCOUNTS_FILE, SESSIONS_DIR)
    tiktok_scraper = TikTokScraper(tiktok_manager)

managers = {
    'instagram': InstagramAccountManager(ACCOUNTS_FILE, SESSIONS_DIR),
    'tiktok':    tiktok_manager,
    'facebook':  FacebookAccountManager(ACCOUNTS_FILE, SESSIONS_DIR),
    'youtube':   YouTubeAccountManager(ACCOUNTS_FILE, SESSIONS_DIR),
    'twitter':   TwitterAccountManager(ACCOUNTS_FILE, SESSIONS_DIR),
    'threads':   ThreadsAccountManager(ACCOUNTS_FILE, SESSIONS_DIR),
}

# Auto-bootstrap YouTube anonymous akun.
# YouTube scraping pakai yt-dlp anonymous TIDAK butuh cookies — public API.
# Tapi BaseScraper interface mensyaratkan ada "akun aktif" supaya rate
# limiting & error tracking konsisten antar platform. Solusinya: auto-add
# satu sentinel account bernama "anonymous" yang trigger code path
# yt-dlp tanpa cookiefile (lihat youtube_scraper.py:50).
#
# User boleh add akun YouTube real dengan cookies.txt kalau perlu (mis.
# untuk by-pass rate limit / bypass age-gated content) — flow itu tetap
# jalan paralel dengan akun "anonymous".
def _ensure_youtube_anonymous():
    """Idempotent — kalau "anonymous" sudah ada, no-op."""
    yt_mgr = managers['youtube']
    try:
        # list_accounts() return list of dicts dengan field 'username'
        all_accounts = yt_mgr.list_accounts()
        names = {a.get('username') for a in all_accounts if isinstance(a, dict)}
        if 'anonymous' in names:
            logger.info("[youtube] anonymous account already exists, skip bootstrap")
            return
        logger.info("[youtube] Bootstrapping 'anonymous' account (no cookies needed)")
        yt_mgr.add_account(username='anonymous', password='anonymous')
    except Exception as e:
        logger.warning(f"[youtube] Failed to bootstrap anonymous: {e}")

_ensure_youtube_anonymous()

scrapers: Dict[str, object] = {
    'instagram': InstagramScraper(managers['instagram']),
    'tiktok':    tiktok_scraper,
    'facebook':  FacebookScraper(managers['facebook']),
    'youtube':   YouTubeScraper(managers['youtube']),
    'twitter':   TwitterScraper(managers['twitter']),
    'threads':   ThreadsScraper(managers['threads']),
    'news':      NewsScraper(),  # tidak butuh account manager
}

SUPPORTED_PLATFORMS = list(scrapers.keys())

# Rate limit per-platform — masing-masing punya throttle sendiri
_last_call: Dict[str, float] = {p: 0 for p in SUPPORTED_PLATFORMS}
_locks: Dict[str, threading.Lock] = {p: threading.Lock() for p in SUPPORTED_PLATFORMS}

# Delay defaults — bisa di-tweak per platform
PLATFORM_DELAYS = {
    'instagram': (8, 15),    # konservatif, IG paling sensitif (Playwright)
    'tiktok':    (5, 10),
    'facebook':  (6, 12),
    'youtube':   (3, 6),     # public API tolerant
    'twitter':   (8, 15),    # Playwright + cookie. X anti-bot ketat ke akun fresh.
    'threads':   (8, 15),    # Meta-stack, same anti-bot family dengan IG
    'news':      (1, 3),     # paling longgar
}


app = Flask(__name__)


# ---- Endpoints ----

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'service': 'Slaytics Multi-Platform Scraper',
        'version': '2.0',
        'platforms': SUPPORTED_PLATFORMS,
        'endpoints': [
            'GET  /health',
            'POST /scrape  {platform, keyword, max_results, mode?, video_url?, sites?}',
            'POST /accounts {platform, username, password, verification_code?}',
            'GET  /accounts?platform=...',
            'DELETE /accounts/<platform>/<username>',
            'POST /accounts/<platform>/<username>/reactivate',
        ]
    })


@app.route('/health', methods=['GET'])
def health():
    summary = {}
    for plat, mgr in managers.items():
        accounts = mgr.list_accounts()
        active = [a for a in accounts if a['status'] == 'active']
        summary[plat] = {
            'total': len(accounts),
            'active': len(active),
            'banned': len([a for a in accounts if a['status'] == 'banned']),
            'challenged': len([a for a in accounts if a['status'] == 'challenge']),
            'expired': len([a for a in accounts if a['status'] == 'expired']),
            'next_account': active[0]['username'] if active else None,
        }
    summary['news'] = {'total': 'N/A', 'active': 'N/A (public)'}
    
    return jsonify({
        'status': 'ok',
        'service': 'slaytics-multi-scraper',
        'version': '2.0',
        'platforms': summary,
        'uptime_check': datetime.utcnow().isoformat() + 'Z'
    })


@app.route('/scrape', methods=['POST'])
def scrape_endpoint():
    payload = request.get_json(silent=True) or {}
    platform = (payload.get('platform') or 'instagram').lower()
    keyword = (payload.get('keyword') or '').strip().lstrip('#')
    max_results = int(payload.get('max_results') or 30)
    mode = (payload.get('mode') or 'default').lower()  # 'comments' for YouTube
    video_url = payload.get('video_url')               # for YouTube comments
    sites = payload.get('sites')                        # for news (optional list)
    
    if platform not in scrapers:
        return jsonify({'error': f'platform "{platform}" not supported',
                        'supported': SUPPORTED_PLATFORMS}), 400
    
    # YouTube comments mode butuh video_url, bukan keyword
    if not (platform == 'youtube' and mode == 'comments') and not keyword:
        return jsonify({'error': 'keyword required'}), 400
    
    if platform == 'youtube' and mode == 'comments' and not video_url:
        return jsonify({'error': 'video_url required for comments mode'}), 400
    
    # Rate limit per platform
    min_d, max_d = PLATFORM_DELAYS.get(platform, (5, 10))
    with _locks[platform]:
        now = time.time()
        elapsed = now - _last_call[platform]
        if elapsed < min_d:
            sleep_for = min_d - elapsed + random.uniform(0, max_d - min_d)
            logger.info(f"[{platform}] Rate limit: sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
        _last_call[platform] = time.time()
    
    # Cek active accounts (kecuali news)
    if platform != 'news':
        active = managers[platform].list_active()
        if not active:
            # Safety net khusus YouTube: anonymous account bisa di-delete user
            # via DELETE /accounts/youtube/anonymous. Kalau itu kejadian dan
            # user trigger scrape, auto-recreate diam-diam supaya scrape tetap
            # jalan tanpa setup ulang. Platform lain (FB/IG/TikTok/Twitter)
            # TIDAK di-auto-recreate karena butuh cookies real dari user.
            if platform == 'youtube':
                logger.info("[youtube] No active accounts at scrape-time, auto-recreating anonymous")
                try:
                    managers['youtube'].add_account(username='anonymous', password='anonymous')
                    active = managers['youtube'].list_active()
                except Exception as e:
                    logger.warning(f"[youtube] Auto-recreate failed: {e}")

            if not active:
                return jsonify({
                    'error': 'no_active_accounts',
                    'platform': platform,
                    'message': f'Tidak ada akun {platform} aktif. Tambahkan via POST /accounts.',
                    'posts': []
                }), 503
    
    scraper = scrapers[platform]
    max_attempts = 1 if platform == 'news' else min(3, len(managers[platform].list_active()))
    last_error = None
    
    for attempt in range(max_attempts):
        try:
            if platform == 'youtube' and mode == 'comments':
                posts = scraper.scrape_comments(video_url, amount=max_results)
            elif platform == 'news':
                posts = scraper.scrape_keyword(keyword, amount=max_results, sites=sites)
            else:
                posts = scraper.scrape_keyword(keyword, amount=max_results)
            
            time.sleep(random.uniform(1, 3))
            
            return jsonify({
                'platform': platform,
                'keyword': keyword if mode != 'comments' else video_url,
                'mode': mode if platform == 'youtube' else 'posts',
                'posts': posts,
                'count': len(posts),
                'source': 'android-scraper',
                'account_used': getattr(scraper, 'last_used_account', None),
            })
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{platform}] Attempt {attempt+1}/{max_attempts} failed: {e}")
            time.sleep(random.uniform(2, 5))
    
    return jsonify({
        'error': 'all_attempts_failed',
        'platform': platform,
        'message': last_error,
        'posts': []
    }), 502


@app.route('/accounts', methods=['POST'])
def add_account():
    payload = request.get_json(silent=True) or {}
    platform = (payload.get('platform') or 'instagram').lower()
    username = (payload.get('username') or '').strip()
    password = payload.get('password', '')
    verification_code = payload.get('verification_code')
    
    if platform not in managers:
        return jsonify({'error': f'platform "{platform}" not supported',
                        'supported': list(managers.keys())}), 400
    
    if not username or not password:
        return jsonify({'error': 'username and password required'}), 400
    
    try:
        # v3 (7 May 2026): semua platform sekarang cookie-based dengan
        # interface seragam (username, password=storage_state, verification_code).
        # Twitter sebelumnya punya kwargs email/totp_secret untuk twikit;
        # sekarang sudah di-drop bersama library twikit-nya.
        result = managers[platform].add_account(username, password, verification_code)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[{platform}] Add account {username} failed: {e}")
        return jsonify({'error': str(e), 'platform': platform, 'username': username}), 400


@app.route('/accounts', methods=['GET'])
def list_accounts():
    platform_filter = (request.args.get('platform') or '').lower()
    out = {}
    for plat, mgr in managers.items():
        if not platform_filter or platform_filter == plat:
            out[plat] = mgr.list_accounts()
    return jsonify({'accounts': out})


@app.route('/accounts/<platform>/<username>', methods=['DELETE'])
def delete_account(platform, username):
    if platform not in managers:
        return jsonify({'error': 'unsupported platform'}), 400
    ok = managers[platform].delete_account(username)
    return jsonify({'deleted': ok, 'platform': platform, 'username': username})


@app.route('/accounts/<platform>/<username>/reactivate', methods=['POST'])
def reactivate_account(platform, username):
    if platform not in managers:
        return jsonify({'error': 'unsupported platform'}), 400
    ok = managers[platform].set_status(username, 'active')
    return jsonify({'reactivated': ok, 'platform': platform, 'username': username})


# ---- Backward-compat: legacy endpoints (no platform = Instagram) ----

@app.route('/accounts/<username>', methods=['DELETE'])
def delete_account_legacy(username):
    ok = managers['instagram'].delete_account(username)
    return jsonify({'deleted': ok, 'username': username})


@app.route('/accounts/<username>/reactivate', methods=['POST'])
def reactivate_account_legacy(username):
    ok = managers['instagram'].set_status(username, 'active')
    return jsonify({'reactivated': ok, 'username': username})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5005))
    host = os.environ.get('HOST', '0.0.0.0')
    logger.info(f"Starting Slaytics Multi-Scraper on {host}:{port}")
    for plat, mgr in managers.items():
        logger.info(f"  [{plat}] {len(mgr.list_accounts())} account(s) loaded")
    app.run(host=host, port=port, threaded=True)
