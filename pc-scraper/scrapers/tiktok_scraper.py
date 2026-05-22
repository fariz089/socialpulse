"""
TikTok Scraper — FIXED VERSION
================================
Fix dari versi debug:

  1. Ganti User-Agent ke desktop (Chrome Windows) supaya konsisten dengan
     cookies yang di-export dari browser desktop.

  2. Tambah sec-fetch headers dan Accept-Language en-US supaya TikTok
     tidak anggap request suspicious.

  3. Search redirect ke /foryou fix: tambah header Referer yang benar
     dan cookie tt_csrf_token di request.

  4. Tambah fallback ke TikTok internal API endpoint (/api/challenge/item_list/
     dan /api/search/item/full/) yang lebih reliable dari HTML parsing —
     karena HTML SSR TikTok sudah tidak embed video data untuk region tertentu.

  5. canUseQuery: False diatasi dengan fallback ke API endpoint yang tidak
     depend on queryData dari SSR.
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from .base import BaseScraper, to_jsonable

logger = logging.getLogger(__name__)

# Desktop UA — harus konsisten dengan browser yang dipakai export cookies
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

DEBUG_DUMP_DIR = Path(__file__).parent.parent / "debug_dumps"

# Timeout request (detik)
REQUEST_TIMEOUT = 30


class TikTokScraper(BaseScraper):

    PLATFORM = "tiktok"

    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        keyword = keyword.lstrip('#')
        username = self.account_manager.pick_next_active()
        if not username:
            raise Exception("No active TikTok accounts available")

        self.last_used_account = username
        cookies = self.account_manager.get_cookies(username)

        try:
            # Coba API endpoint dulu (lebih reliable dari HTML scraping)
            posts = self._scrape_via_api(keyword, cookies, amount)

            # Fallback ke HTML kalau API gagal
            if not posts:
                logger.info(f"[tiktok] API returned 0, trying HTML tag page")
                posts = self._scrape_tag_html(keyword, cookies, amount)

            if not posts:
                logger.info(f"[tiktok] HTML tag returned 0, trying HTML search page")
                posts = self._scrape_search_html(keyword, cookies, amount)

            self.account_manager.mark_used(username)
            logger.info(f"[tiktok] Scraped {len(posts)} videos for '{keyword}' via @{username}")
            return posts

        except Exception as e:
            self.account_manager.mark_error(username, e, status=self._infer_status(e))
            raise

    # ──────────────────────────────────────────────
    # API Endpoint (lebih reliable dari HTML)
    # ──────────────────────────────────────────────

    def _scrape_via_api(self, keyword: str, cookies: dict, amount: int) -> List[Dict]:
        """
        Pakai TikTok internal API endpoint.
        Untuk hashtag: /api/challenge/item_list/
        Untuk search:  /api/search/item/full/
        Keduanya return JSON langsung, tidak perlu parse HTML.
        """
        posts = self._api_hashtag(keyword, cookies, amount)
        if not posts:
            posts = self._api_search(keyword, cookies, amount)
        return posts

    def _api_hashtag(self, tag: str, cookies: dict, amount: int) -> List[Dict]:
        """
        Ambil challenge ID dulu dari halaman /tag/, lalu hit /api/challenge/item_list/.
        """
        try:
            challenge_id = self._get_challenge_id(tag, cookies)
            if not challenge_id:
                logger.info(f"[tiktok-api] Could not get challenge ID for #{tag}")
                return []

            logger.info(f"[tiktok-api] Got challenge ID {challenge_id} for #{tag}")

            session = self._make_session(cookies)
            posts = []
            cursor = 0

            while len(posts) < amount:
                count = min(30, amount - len(posts))
                url = (
                    f"https://www.tiktok.com/api/challenge/item_list/"
                    f"?challengeID={challenge_id}&count={count}&cursor={cursor}"
                    f"&aid=1988&app_language=en&region=US"
                )
                resp = session.get(url, timeout=REQUEST_TIMEOUT)
                logger.info(f"[tiktok-api] /api/challenge/item_list/ → HTTP {resp.status_code}")

                if resp.status_code != 200:
                    break

                data = resp.json()
                items = data.get('itemList') or []
                if not items:
                    break

                for item in items:
                    try:
                        posts.append(self._normalize(item))
                    except Exception as e:
                        logger.warning(f"[tiktok] normalize failed: {e}")

                has_more = data.get('hasMore', False)
                cursor = data.get('cursor', cursor + count)
                if not has_more:
                    break

            return posts

        except Exception as e:
            logger.warning(f"[tiktok-api] hashtag API failed: {e}")
            return []

    def _get_challenge_id(self, tag: str, cookies: dict) -> Optional[str]:
        """
        Fetch halaman /tag/<name> dan extract challengeID dari JSON scope.
        """
        try:
            session = self._make_session(cookies)
            url = f"https://www.tiktok.com/tag/{quote(tag)}"
            resp = session.get(url, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                return None

            # Coba dari JSON scope
            m = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                resp.text, re.DOTALL
            )
            if m:
                data = json.loads(m.group(1))
                scope = data.get('__DEFAULT_SCOPE__', {})
                challenge = scope.get('webapp.challenge-detail', {})
                cid = (challenge.get('challengeInfo') or {}).get('challenge', {}).get('id')
                if cid:
                    return str(cid)

            # Fallback: cari di HTML langsung
            patterns = [
                r'"challengeId"\s*:\s*"(\d+)"',
                r'"id"\s*:\s*"(\d+)".*?"challengeName"',
                r'challengeID=(\d+)',
            ]
            for pattern in patterns:
                m = re.search(pattern, resp.text)
                if m:
                    return m.group(1)

        except Exception as e:
            logger.warning(f"[tiktok-api] _get_challenge_id failed: {e}")

        return None

    def _api_search(self, query: str, cookies: dict, amount: int) -> List[Dict]:
        """
        Pakai /api/search/item/full/ untuk search video by keyword.
        """
        try:
            session = self._make_session(cookies)
            posts = []
            offset = 0

            while len(posts) < amount:
                count = min(30, amount - len(posts))
                url = (
                    f"https://www.tiktok.com/api/search/item/full/"
                    f"?keyword={quote(query)}&offset={offset}&count={count}"
                    f"&aid=1988&app_language=en&region=US&search_source=normal"
                )
                resp = session.get(url, timeout=REQUEST_TIMEOUT)
                logger.info(f"[tiktok-api] /api/search/item/full/ → HTTP {resp.status_code}")

                if resp.status_code != 200:
                    break

                data = resp.json()
                items = data.get('item_list') or data.get('itemList') or []
                if not items:
                    break

                for item in items:
                    try:
                        posts.append(self._normalize(item))
                    except Exception as e:
                        logger.warning(f"[tiktok] normalize failed: {e}")

                has_more = data.get('has_more', False) or data.get('hasMore', False)
                offset += len(items)
                if not has_more:
                    break

            return posts

        except Exception as e:
            logger.warning(f"[tiktok-api] search API failed: {e}")
            return []

    # ──────────────────────────────────────────────
    # HTML Scraping (fallback)
    # ──────────────────────────────────────────────

    def _scrape_tag_html(self, tag: str, cookies: dict, amount: int) -> List[Dict]:
        url = f'https://www.tiktok.com/tag/{quote(tag)}'
        return self._fetch_and_parse(url, cookies, amount, label='tag')

    def _scrape_search_html(self, query: str, cookies: dict, amount: int) -> List[Dict]:
        url = f'https://www.tiktok.com/search/video?q={quote(query)}'
        return self._fetch_and_parse(url, cookies, amount, label='search')

    def _fetch_and_parse(self, url: str, cookies: dict, amount: int, label: str = '') -> List[Dict]:
        session = self._make_session(cookies)

        logger.info(f"[tiktok-debug] [{label}] GET {url}")
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        logger.info(
            f"[tiktok-debug] [{label}] HTTP {resp.status_code}, "
            f"final URL: {resp.url}, HTML size: {len(resp.text)} bytes"
        )

        if resp.status_code != 200:
            raise Exception(f"TikTok returned HTTP {resp.status_code}")

        # Kalau redirect ke login, abort
        if '/login' in resp.url or 'login?' in resp.url:
            raise Exception(f"TikTok redirected to login: {resp.url}")

        # Kalau redirect ke /foryou, HTML scraping tidak akan dapat video — log dan return []
        if '/foryou' in resp.url and '/foryou' not in url:
            logger.warning(f"[tiktok-debug] [{label}] Redirected to /foryou — HTML scraping won't work")
            return []

        html = resp.text
        m = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if not m:
            self._dump_debug(html, label, 'no_universal_data')
            raise Exception("TikTok page structure changed — __UNIVERSAL_DATA__ tidak ditemukan")

        try:
            data = json.loads(m.group(1))
        except Exception as e:
            self._dump_debug(html, label, 'json_parse_error')
            raise Exception(f"Failed parsing TikTok JSON: {e}")

        scope = data.get('__DEFAULT_SCOPE__', {})
        logger.info(f"[tiktok-debug] [{label}] scope keys ({len(scope)}): {sorted(scope.keys())}")

        items = self._extract_items(data, label=label)
        logger.info(f"[tiktok-debug] [{label}] _extract_items returned {len(items)} candidates")

        if not items:
            self._dump_debug(html, label, 'zero_items')
            self._dump_debug_json(data, label, 'zero_items')

        posts = []
        for item in items[:amount]:
            try:
                posts.append(self._normalize(item))
            except Exception as e:
                logger.warning(f"[tiktok] normalize failed: {e}")
        return posts

    # ──────────────────────────────────────────────
    # Session builder — ini kuncinya
    # ──────────────────────────────────────────────

    @staticmethod
    def _make_session(cookies: dict) -> requests.Session:
        """
        Buat requests.Session dengan headers dan cookies yang benar.

        FIX UTAMA:
          - Desktop User-Agent (bukan Android) supaya konsisten dengan
            cookies dari browser desktop
          - sec-fetch headers supaya TikTok tidak flag sebagai bot
          - Accept-Language en-US (region ID sering dapat SSR tanpa video)
          - Referer https://www.tiktok.com/ untuk semua request
          - Cookie ms_token dikirim sebagai 'msToken' (nama asli TikTok)
        """
        session = requests.Session()

        session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',  # Jangan 'br' — requests tidak auto-decode brotli tanpa package tambahan
            'Referer': 'https://www.tiktok.com/',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'Connection': 'keep-alive',
        })

        # Build cookie jar
        # FIX: ms_token di code kita = msToken di browser TikTok
        for k, v in cookies.items():
            if not v:
                continue
            if k == 'ms_token':
                session.cookies.set('msToken', v, domain='.tiktok.com')
            else:
                session.cookies.set(k, v, domain='.tiktok.com')

        # API endpoint butuh header tambahan
        # Akan di-override per-request kalau perlu
        return session

    # ──────────────────────────────────────────────
    # Extract items dari HTML JSON scope (fallback)
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_items(data: dict, label: str = '') -> List[dict]:
        scope = data.get('__DEFAULT_SCOPE__', {})
        candidates = []

        # Hashtag page
        tag_data = scope.get('webapp.challenge-detail') or {}
        if 'itemList' in tag_data:
            n = len(tag_data['itemList'])
            logger.info(f"[tiktok-debug] [{label}] webapp.challenge-detail.itemList: {n} items")
            candidates.extend(tag_data['itemList'])

        # Search page
        search_data = scope.get('webapp.search-page') or {}
        for k in ('itemList', 'data'):
            v = search_data.get(k)
            if isinstance(v, list):
                logger.info(f"[tiktok-debug] [{label}] webapp.search-page.{k}: {len(v)} entries")
                for entry in v:
                    if isinstance(entry, dict):
                        candidates.append(entry.get('item') or entry)

        # Generic search di semua scope keys
        for key, val in scope.items():
            if 'search' in key.lower() and isinstance(val, dict):
                for sub_key, v in val.items():
                    if isinstance(v, list):
                        match_count = sum(
                            1 for e in v
                            if isinstance(e, dict) and ('id' in e or 'aweme_id' in e)
                        )
                        if match_count > 0:
                            logger.info(
                                f"[tiktok-debug] [{label}] {key}.{sub_key}: "
                                f"{match_count} video-like entries"
                            )
                        for entry in v:
                            if isinstance(entry, dict) and ('id' in entry or 'aweme_id' in entry):
                                candidates.append(entry)

        # Deduplicate
        seen = set()
        out = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            cid = c.get('id') or c.get('aweme_id')
            if cid and cid not in seen:
                seen.add(cid)
                out.append(c)
        return out

    # ──────────────────────────────────────────────
    # Normalize output
    # ──────────────────────────────────────────────

    @staticmethod
    def _normalize(item: dict) -> dict:
        author = item.get('author') or {}
        stats = item.get('stats') or item.get('statsV2') or {}
        video = item.get('video') or {}

        vid = item.get('id') or item.get('aweme_id')
        author_name = (
            author.get('uniqueId') or author.get('unique_id') or author.get('nickname')
        )

        create_time = item.get('createTime') or item.get('create_time') or 0
        try:
            create_time = int(create_time)
        except Exception:
            create_time = int(time.time())

        avatar = author.get('avatarThumb') or author.get('avatar_thumb') or ''
        if isinstance(avatar, dict):
            avatar = (avatar.get('url_list') or [None])[0]

        def safe_int(val):
            try:
                return int(val or 0)
            except Exception:
                return 0

        result = {
            'platform': 'tiktok',
            'id': str(vid) if vid else None,
            'shortCode': str(vid) if vid else None,
            'ownerUsername': author_name,
            'username': author_name,
            'profilePicUrl': str(avatar) if avatar else None,
            'profile_pic_url': str(avatar) if avatar else None,
            'caption': item.get('desc') or '',
            'text': item.get('desc') or '',
            'likesCount': safe_int(stats.get('diggCount') or stats.get('digg_count')),
            'like_count': safe_int(stats.get('diggCount') or stats.get('digg_count')),
            'commentsCount': safe_int(stats.get('commentCount') or stats.get('comment_count')),
            'comment_count': safe_int(stats.get('commentCount') or stats.get('comment_count')),
            'videoViewCount': safe_int(stats.get('playCount') or stats.get('play_count')),
            'video_view_count': safe_int(stats.get('playCount') or stats.get('play_count')),
            'shareCount': safe_int(stats.get('shareCount') or stats.get('share_count')),
            'timestamp': create_time,
            'taken_at': create_time,
            'url': (
                f'https://www.tiktok.com/@{author_name}/video/{vid}'
                if author_name and vid else ''
            ),
            'duration': safe_int(video.get('duration')),
        }
        return to_jsonable(result)

    # ──────────────────────────────────────────────
    # Debug dump helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _dump_debug(html: str, label: str, reason: str):
        try:
            DEBUG_DUMP_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = DEBUG_DUMP_DIR / f"tiktok_{label}_{reason}_{ts}.html"
            filename.write_text(html, encoding='utf-8')
            logger.info(f"[tiktok-debug] Dumped HTML to {filename}")
        except Exception as e:
            logger.warning(f"[tiktok-debug] Failed to dump HTML: {e}")

    @staticmethod
    def _dump_debug_json(data: dict, label: str, reason: str):
        try:
            DEBUG_DUMP_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = DEBUG_DUMP_DIR / f"tiktok_{label}_{reason}_{ts}.json"
            filename.write_text(json.dumps(data, indent=2)[:500000], encoding='utf-8')
            logger.info(f"[tiktok-debug] Dumped JSON to {filename} (truncated to 500KB)")
        except Exception as e:
            logger.warning(f"[tiktok-debug] Failed to dump JSON: {e}")

    # ──────────────────────────────────────────────
    # Error classification
    # ──────────────────────────────────────────────

    def _infer_status(self, error: Exception) -> Optional[str]:
        s = str(error).lower()
        if 'verify' in s or 'captcha' in s or 'challenge' in s:
            return self.account_manager.STATUS_CHALLENGE
        if '403' in s or 'forbidden' in s or 'login' in s:
            return self.account_manager.STATUS_EXPIRED
        if '429' in s or 'rate' in s:
            return self.account_manager.STATUS_RATE_LIMITED
        return None
