"""
Threads Scraper (Playwright + GraphQL response interception)
==============================================================

VERSION: v1.0 (13 May 2026)

ARSITEKTUR:
  Mirror pattern Twitter/Instagram scraper: SKIP DOM scraping, pakai
  GraphQL response yang Threads sendiri fetch saat search loading.

  Workflow:
    1. Navigate ke www.threads.net/search?q={keyword}&serp_type=default
    2. Pasang response listener BEFORE goto()
    3. Dismiss "Continue as <username>" login choice modal kalau muncul
       (modal ini block scroll-triggered XHR pagination)
    4. Wait initial GraphQL responses settle, extract thread items
    5. Scroll untuk pagination (Threads infinite scroll)
    6. Dedup by thread id (pk), return top `amount`

LOGIN CHOICE MODAL:
  Kadang Threads tampilin modal 'Katakan lebih banyak dengan Threads /
  Lanjutkan dengan Instagram <username>' walaupun cookies dari Instagram
  session sudah ter-recognize. Tombolnya `<div role="button">` (BUKAN
  link biasa), klik akan trigger JS auth handshake yang finalize session.
  Tanpa klik ini, scroll-triggered pagination XHR gak fire — hasil log
  bakal `responses_seen=0`.

ENDPOINT TARGET:
  - https://www.threads.net/api/graphql  (POST GraphQL queries)
  - Query yang relevan untuk search:
    * BarcelonaSearchResultsTabbedQuery     (top/recent search results)
    * BarcelonaSearchRecentTabQuery          (recent-only tab)
  - Headers wajib di GraphQL request:
    * x-ig-app-id: 238260118697367
    * referer: https://www.threads.net/...
  Tapi karena kita scrape via browser, Playwright auto-include semua itu.

  Threads search URL pakai param `serp_type`:
    - serp_type=default (mixed top results)
    - serp_type=recent  (Latest tab, chronological)
  Kita default ke `default` untuk dapet trending + recent campur.

RESPONSE STRUCTURE (skema 2026-Q2):
  data.searchResults.edges[].node
    .thread_items[].post = {
       pk: "1234567890",
       id: "...",
       caption: { text: "..." } | null,
       like_count, text_post_app_info: { direct_reply_count, reshare_count },
       taken_at: <unix>,
       user: { username, full_name, profile_pic_url, ... },
       code: "Cabc...",   # untuk build URL: threads.net/@{user}/post/{code}
       ...
    }

  Bisa juga ada wrapping di:
    data.media_id_to_serialized_xpost_thread (untuk media id query)
    data.viewer.search_results_v3.results[]

  Kita walker generik yang cari node dengan key `pk` + `caption` + `user`
  supaya tahan kalau Meta ubah envelope tapi pertahankan inner shape.

  Lifecycle:
    - Browser singleton di-launch sekali, reuse antar scrape
    - Per-scrape pakai fresh BrowserContext
    - Auto-recovery kalau browser crash
    - Worker thread untuk Playwright (sync API not thread-safe)
"""

import json
import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .base import BaseScraper, to_jsonable
from ._stealth import STEALTH_INIT_JS, STEALTH_LAUNCH_ARGS

logger = logging.getLogger(__name__)

_playwright_import_error: Optional[str] = None
try:
    from playwright.sync_api import (
        sync_playwright,
        Browser,
        Response,
        TimeoutError as PWTimeout,
    )
except ImportError as e:
    _playwright_import_error = str(e)
    sync_playwright = None  # type: ignore
    Browser = Response = PWTimeout = None  # type: ignore


DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

BLOCKED_RESOURCE_TYPES = {'media', 'font'}
BLOCKED_DOMAIN_FRAGMENTS = (
    'doubleclick.net',
    'googletagmanager.com',
    'google-analytics.com',
    'facebook.com/tr',  # FB Pixel tracking
)

# URL fragments yang menandai response feed search Threads.
# Threads pakai random doc_id hash di POST body, tapi URL-nya konsisten
# `/api/graphql`. Untuk membedakan request search vs request lain
# (notifikasi, viewer, dll), kita confirm dengan body content di parser.
THREADS_GRAPHQL_PATH = '/api/graphql'
THREADS_SEARCH_OPERATION_HINTS = (
    'BarcelonaSearchResultsTabbedQuery',
    'BarcelonaSearchRecentTabQuery',
    'BarcelonaSearchResultsQuery',
    'searchResults',
    'search_results',
)


# ============================================================================
# Worker thread untuk Playwright
# ============================================================================

class _PlaywrightWorker:
    _SHUTDOWN = object()

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._lock = threading.Lock()

    def _ensure_started(self):
        with self._lock:
            if self._started:
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name='th-pw-worker')
            self._thread.start()
            self._started = True

    def submit(self, fn: Callable, timeout: float = 90.0):
        if _playwright_import_error:
            raise Exception(
                f"Playwright not installed: {_playwright_import_error}. "
                "Install: pip install playwright && playwright install chromium"
            )
        self._ensure_started()
        future: Future = Future()
        self._queue.put((fn, future))
        return future.result(timeout=timeout)

    def _run(self):
        pw = None
        browser = None

        def _close_browser():
            nonlocal browser, pw
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
                browser = None
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass
                pw = None

        def _ensure_browser():
            nonlocal browser, pw
            if browser is not None:
                try:
                    if browser.is_connected():
                        return browser
                except Exception:
                    pass
                logger.warning("[threads] Browser disconnected, re-launching")
                _close_browser()

            logger.info("[threads] Launching headless Chromium")
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=STEALTH_LAUNCH_ARGS,
                ignore_default_args=['--enable-automation'],
            )
            return browser

        while True:
            try:
                item = self._queue.get()
            except Exception:
                continue

            if item is self._SHUTDOWN:
                _close_browser()
                logger.info("[threads] Worker thread shutdown")
                return

            fn, future = item
            try:
                br = _ensure_browser()
                result = fn(br)
                future.set_result(result)
            except Exception as e:
                err_str = str(e).lower()
                if any(s in err_str for s in ('disconnected', 'closed', 'crashed', 'target')):
                    _close_browser()
                future.set_exception(e)

    def shutdown(self):
        if self._started:
            self._queue.put(self._SHUTDOWN)


_worker = _PlaywrightWorker()


# ============================================================================
# Post extraction helpers
# ============================================================================

def _safe_get(d: Any, *keys, default=None):
    """Nested dict access tanpa exception."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


def _extract_post(post: dict) -> Optional[Dict]:
    """
    Convert Threads `post` dict ke shape standar SocialPulse.

    Skema Threads post (2026):
      {
        pk: "3138977881796614961",       # primary key
        id: "...",                        # alias
        code: "Cabc...",                  # untuk URL: threads.net/@user/post/<code>
        caption: { text: "..." } | null,  # null kalau image/repost only
        like_count: 42,
        text_post_app_info: {
          direct_reply_count: 5,
          reshare_count: 2,
          repost_count: 1,
          is_post_unavailable: false,
        },
        taken_at: 1715600000,             # Unix timestamp
        user: {
          pk: "12345", username, full_name, profile_pic_url, is_verified
        },
        image_versions2: { candidates: [{url, width, height}, ...] } | null,
        video_versions: [...] | null,
      }
    """
    if not isinstance(post, dict):
        return None

    pk = post.get('pk') or post.get('id')
    if not pk:
        return None

    # Caption — bisa null kalau cuma image/video tanpa text
    caption_obj = post.get('caption')
    if isinstance(caption_obj, dict):
        text = caption_obj.get('text') or ''
    elif isinstance(caption_obj, str):
        text = caption_obj
    else:
        text = ''

    # User info
    user = post.get('user') or {}
    if not isinstance(user, dict):
        user = {}
    username = user.get('username') or 'unknown'
    full_name = user.get('full_name')
    avatar = user.get('profile_pic_url')

    # Engagement
    like_count = post.get('like_count') or 0
    try:
        like_count = int(like_count)
    except (TypeError, ValueError):
        like_count = 0

    tpa = post.get('text_post_app_info') or {}
    reply_count = (tpa.get('direct_reply_count') if isinstance(tpa, dict) else 0) or 0
    reshare_count = (tpa.get('reshare_count') if isinstance(tpa, dict) else 0) or 0
    repost_count = (tpa.get('repost_count') if isinstance(tpa, dict) else 0) or 0
    try:
        reply_count = int(reply_count)
    except (TypeError, ValueError):
        reply_count = 0
    try:
        reshare_count = int(reshare_count)
    except (TypeError, ValueError):
        reshare_count = 0
    try:
        repost_count = int(repost_count)
    except (TypeError, ValueError):
        repost_count = 0

    # Timestamp — Threads pakai taken_at Unix (sama dengan IG)
    taken_at = post.get('taken_at')
    try:
        taken_at = int(taken_at) if taken_at else None
    except (TypeError, ValueError):
        taken_at = None

    # URL: threads.net/@{username}/post/{code}
    code = post.get('code')
    url = (
        f'https://www.threads.net/@{username}/post/{code}'
        if username and code else
        f'https://www.threads.net/t/{pk}'
    )

    # Image: ambil candidate highest resolution kalau ada
    image_url = None
    img2 = post.get('image_versions2')
    if isinstance(img2, dict):
        candidates = img2.get('candidates') or []
        if candidates and isinstance(candidates[0], dict):
            image_url = candidates[0].get('url')

    return {
        'platform': 'threads',
        'id': str(pk),
        'shortCode': str(code) if code else str(pk),
        'ownerUsername': username,
        'username': username,
        'authorName': full_name,
        'profilePicUrl': str(avatar) if avatar else None,
        'profile_pic_url': str(avatar) if avatar else None,
        'caption': text,
        'text': text,
        'likesCount': like_count,
        'like_count': like_count,
        'commentsCount': reply_count,
        'comment_count': reply_count,
        'shareCount': reshare_count,
        'repostCount': repost_count,
        # Threads tidak expose view count untuk post text-only,
        # tapi video punya `video_view_count`
        'videoViewCount': 0,
        'video_view_count': 0,
        'timestamp': taken_at,
        'taken_at': taken_at,
        'imageUrl': image_url,
        'url': url,
    }


def _walk_for_posts(obj: Any, posts: List[Dict], seen_ids: set, depth: int = 0):
    """
    Recursive walk untuk cari Threads post nodes di GraphQL response.

    Heuristik:
      - Dict yang punya field 'pk' (atau 'id') DAN 'caption'/'text_post_app_info'
        DAN 'user' diperlakukan sebagai post.
      - Threads wrapper biasa: thread_items[] (array of {post: {...}}).
        Kita descend ke 'post' field-nya.
    """
    if depth > 14:
        return

    if isinstance(obj, dict):
        # Pattern 1: dict ini sendiri adalah post-shape
        if (
            'pk' in obj or 'id' in obj
        ) and (
            'caption' in obj or 'text_post_app_info' in obj
        ) and 'user' in obj:
            post = _extract_post(obj)
            if post:
                pid = post.get('id')
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    posts.append(post)
            # Tetap descend (mungkin ada quoted/reposted di dalam)

        # Pattern 2: thread_items wrapper — descend ke setiap 'post'
        thread_items = obj.get('thread_items')
        if isinstance(thread_items, list):
            for item in thread_items:
                if isinstance(item, dict) and isinstance(item.get('post'), dict):
                    _walk_for_posts(item['post'], posts, seen_ids, depth + 1)

        # Recurse children lainnya
        for k, v in obj.items():
            if k == 'thread_items':
                continue  # sudah di-handle di atas
            _walk_for_posts(v, posts, seen_ids, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            _walk_for_posts(item, posts, seen_ids, depth + 1)


def _extract_posts_from_response_body(body: str) -> List[Dict]:
    """
    Parse GraphQL response body Threads → list of posts.
    Threads kadang stream multi-JSON di body (newline-delimited untuk
    persistent connections), jadi kita coba parse whole-body dulu,
    fallback ke per-line.
    """
    if not body:
        return []

    posts: List[Dict] = []
    seen: set = set()

    # Try whole-body single JSON
    try:
        data = json.loads(body)
        _walk_for_posts(data, posts, seen)
        if posts:
            return posts
    except Exception:
        pass

    # Fallback: split per-line (kadang Meta GraphQL pakai NDJSON)
    for line in body.split('\n'):
        line = line.strip()
        if not line or not (line.startswith('{') or line.startswith('[')):
            continue
        try:
            data = json.loads(line)
            _walk_for_posts(data, posts, seen)
        except Exception:
            continue

    return posts


def _response_body_looks_like_search(body: str) -> bool:
    """
    Heuristik: response GraphQL Threads relevan untuk search?
    Tanpa filter ini, kita parse banyak GraphQL response yang gak related
    (notifikasi, viewer, dsb) — buang-buang CPU.
    """
    if not body or len(body) < 50:
        return False
    # Cek ada keyword search-related ATAU thread_items/post struktur
    sample = body[:5000].lower()
    if any(hint.lower() in sample for hint in THREADS_SEARCH_OPERATION_HINTS):
        return True
    # Sometimes the operation hint hilang dari body, tapi shape thread_items ada
    if '"thread_items"' in sample or '"text_post_app_info"' in sample:
        return True
    return False


# ============================================================================
# SSR HTML extraction (initial search page)
# ============================================================================
#
# Threads search page-1 di-render server-side via Relay preloader. JSON state
# di-embed langsung di dalam <script> tags HTML, bukan via XHR. Listener
# response GraphQL kita gak nangkap apa-apa untuk page 1 — XHR baru fire saat
# scroll trigger pagination (page 2+).
#
# Karena itu kita extract dari HTML setelah page loaded: cari setiap blok
# `"post":{...}` dengan brace-balanced JSON parsing, kasih ke walker yang
# sudah ada untuk shape-check + dedup.

def _find_balanced_value(text: str, start: int) -> int:
    """
    Diberi text[start] == '{' atau '[', return index SETELAH karakter penutup
    yang seimbang. Handle escaped quotes di dalam string literal.
    Return -1 kalau tidak balanced atau invalid start.
    """
    if start >= len(text):
        return -1
    open_c = text[start]
    if open_c == '{':
        close_c = '}'
    elif open_c == '[':
        close_c = ']'
    else:
        return -1

    depth = 0
    in_string = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _extract_posts_from_html(html: str) -> List[Dict]:
    """
    Extract Threads posts dari SSR Relay preloader state yang di-embed di
    dalam HTML. Strategi: find setiap occurrence `"post":{`, brace-balance
    untuk dapat JSON object lengkap, parse, kasih ke `_walk_for_posts`.

    Walker akan filter ke dict yang shape-nya benar (`pk` + `caption|
    text_post_app_info` + `user`) dan dedup via seen-set.
    """
    if not html or '"post":{' not in html:
        return []

    posts: List[Dict] = []
    seen: set = set()

    for m in re.finditer(r'"post":\{', html):
        start = m.end() - 1  # position of '{'
        end = _find_balanced_value(html, start)
        if end < 0:
            continue
        try:
            obj = json.loads(html[start:end])
        except Exception:
            continue
        _walk_for_posts(obj, posts, seen)

    return posts


# ============================================================================
# ThreadsScraper main class
# ============================================================================

class ThreadsScraper(BaseScraper):

    PLATFORM = "threads"
    BASE = "https://www.threads.net"

    NAV_TIMEOUT = 45_000
    SELECTOR_TIMEOUT = 20_000

    MAX_SCROLLS = 15
    SCROLL_WAIT_MS = 2_500
    SCROLL_STUCK_THRESHOLD = 3

    # serp_type: 'default' (Top + Recent campuran) atau 'recent' (Latest chrono).
    # Default ke 'default' supaya dapat hasil mix yang lebih reflective untuk
    # sentiment monitoring. User bisa override per-keyword via project config.
    DEFAULT_SEARCH_SERP = 'default'

    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        """Search threads by keyword."""
        if not keyword.strip():
            raise Exception("Keyword kosong")

        username = self.account_manager.pick_next_active()
        if not username:
            raise Exception("No active Threads accounts available")

        self.last_used_account = username
        cookies = self.account_manager.get_cookies(username)

        try:
            posts = self._search_with_browser(keyword, cookies, amount)
            self.account_manager.mark_used(username)

            sorted_posts = sorted(
                posts,
                key=lambda p: p.get('taken_at') or p.get('timestamp') or 0,
                reverse=True,
            )
            logger.info(
                f"[threads] Scraped {len(sorted_posts)} threads for '{keyword}' via @{username}"
            )
            return sorted_posts[:amount]
        except Exception as e:
            self.account_manager.mark_error(username, e, status=self._infer_status(e))
            raise

    def _search_with_browser(
        self, keyword: str, cookies: dict, amount: int,
    ) -> List[Dict]:
        """Submit job ke worker thread."""
        def _do_scrape(browser):
            return self._scrape_inside_worker(browser, keyword, cookies, amount)
        return _worker.submit(_do_scrape, timeout=120.0)

    def _scrape_inside_worker(
        self, browser, keyword: str, cookies: dict, amount: int,
    ) -> List[Dict]:
        """Eksekusi inside worker thread."""
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={'width': 1366, 'height': 900},
            locale='id-ID',
            timezone_id='Asia/Jakarta',
            bypass_csp=True,
        )

        try:
            context.add_init_script(STEALTH_INIT_JS)
        except Exception as e:
            logger.warning(f"[threads] Stealth inject failed: {e}")

        collected: Dict[str, Dict] = {}
        stats = {'responses_seen': 0, 'responses_with_posts': 0, 'parse_errors': 0}

        try:
            self._inject_cookies(context, cookies)
            context.route('**/*', self._handle_route)

            page = context.new_page()
            page.set_default_navigation_timeout(self.NAV_TIMEOUT)
            page.set_default_timeout(self.SELECTOR_TIMEOUT)

            self._attach_response_listener(page, collected, stats)

            search_url = (
                f"{self.BASE}/search?q={quote(keyword)}"
                f"&serp_type={self.DEFAULT_SEARCH_SERP}"
            )
            logger.info(f"[threads] Navigating to {search_url}")
            try:
                page.goto(search_url, wait_until='domcontentloaded')
            except PWTimeout:
                logger.warning("[threads] Navigation timeout; lanjut anyway")

            # Dismiss 'Continue as <username>' login choice modal kalau muncul.
            # Kalau gak di-klik, scroll XHR gak fire (responses_seen=0).
            modal_clicked = self._dismiss_login_modal(page)

            # Setelah klik modal, Threads sering redirect ke home feed ('/')
            # alih-alih stay di /search. Re-navigate ke search URL — sekarang
            # session sudah finalized, request kedua bakal load search normal
            # tanpa modal lagi.
            if modal_clicked and '/search' not in page.url.lower():
                logger.info(
                    f"[threads] Post-modal redirect ke {page.url} — "
                    "re-navigate ke search URL"
                )
                try:
                    page.goto(search_url, wait_until='domcontentloaded')
                except PWTimeout:
                    logger.warning("[threads] Re-navigation timeout; lanjut anyway")
                # Defensive: kalau modal nongol lagi (rare), dismiss sekali lagi
                self._dismiss_login_modal(page, timeout_ms=3_000)

            current_url = page.url.lower()

            # Fix: threads.com (wrong domain / redirect) → threads.net (domain resmi).
            # Kalau browser ter-redirect ke www.threads.com, page akan render kosong
            # (body empty, no SSR content). Deteksi dan force re-navigate ke threads.net.
            if 'threads.com' in current_url and 'threads.net' not in current_url:
                logger.warning(
                    f"[threads] Redirected ke threads.com (wrong domain): {page.url} — "
                    "force re-navigate ke threads.net"
                )
                corrected_url = search_url.replace('threads.com', 'threads.net')
                try:
                    page.goto(corrected_url, wait_until='domcontentloaded')
                except PWTimeout:
                    logger.warning("[threads] Re-navigation (domain fix) timeout; lanjut anyway")
                current_url = page.url.lower()

            if any(p in current_url for p in [
                '/login', '/accounts/login', '/challenge', 'instagram.com/accounts',
            ]):
                raise Exception(
                    f"Cookie expired or challenged (redirected to {current_url})"
                )

            # ----------------------------------------------------------------
            # SSR HTML extraction — initial search results di-render server-side
            # ke dalam <script> tag (Relay preloader), BUKAN via XHR. XHR cuma
            # fire saat scroll pagination (page 2+). Karena itu kita extract
            # dari HTML dulu untuk dapat page-1 data, lalu listener XHR handle
            # pagination scroll.
            # ----------------------------------------------------------------
            html_posts_count = 0
            try:
                # Beri waktu render React selesai (biasanya cepat setelah
                # domcontentloaded, tapi Relay preloader hydrate butuh tick).
                page.wait_for_timeout(2_500)
                html_content = page.content()

                # Guard: kalau body kosong sama sekali (body empty / domain salah
                # / cookie expired silent), save debug dan skip langsung ke
                # XHR listener — gak ada gunanya parse HTML kosong.
                if not html_content or len(html_content.strip()) < 200:
                    logger.warning(
                        f"[threads] Page body hampir kosong (len={len(html_content or '')}) "
                        f"di url={page.url} — kemungkinan wrong domain atau cookie invalid. "
                        "Skip SSR extraction, fallback ke XHR listener."
                    )
                    self._save_debug_artifacts(page)
                else:
                    html_posts = _extract_posts_from_html(html_content)
                    for p in html_posts:
                        pid = p.get('id')
                        if pid and pid not in collected:
                            collected[pid] = p
                    html_posts_count = len(html_posts)
                    if html_posts_count > 0:
                        logger.info(
                            f"[threads] HTML SSR extraction: "
                            f"{html_posts_count} posts ({len(collected)} unique total)"
                        )
            except Exception as e:
                logger.warning(f"[threads] HTML SSR extraction error (non-fatal): {e}")

            # Skip XHR wait kalau HTML sudah kasih data — XHR memang gak fire
            # untuk page 1, gak ada gunanya tunggu 20 detik. Kalau HTML kosong
            # (struktur Threads berubah / blocked), tetap coba listener.
            if html_posts_count == 0:
                self._wait_for_initial_data(page, collected, stats, timeout_ms=20_000)

            self._scroll_until_target(page, collected, target=amount)
            page.wait_for_timeout(2_000)

            posts = list(collected.values())
            logger.info(
                f"[threads] Extraction: "
                f"html_posts={html_posts_count} "
                f"responses_seen={stats['responses_seen']} "
                f"with_posts={stats['responses_with_posts']} "
                f"parse_errors={stats['parse_errors']} "
                f"unique_posts={len(posts)}"
            )

            if not posts:
                logger.warning("[threads] 0 posts extracted")
                self._save_debug_artifacts(page)

            return [to_jsonable(p) for p in posts[:amount]]

        finally:
            try:
                context.close()
            except Exception:
                pass

    # ------------------------------------------------------------------------
    # Login choice modal handling
    # ------------------------------------------------------------------------

    def _dismiss_login_modal(self, page, timeout_ms: int = 6_000) -> bool:
        """
        Klik tombol 'Lanjutkan dengan Instagram <username>' (a.k.a. 'Continue
        with Instagram') kalau Threads tampilin login choice modal.

        Modal ini muncul saat Threads recognize Instagram session cookies tapi
        belum finalize Threads session penuh. Tombolnya adalah
        `<div role="button" tabindex="0">` (bukan `<a>`), jadi gak bisa di-klik
        via href — harus simulate click event.

        Effect setelah klik:
          - JS handshake panggil endpoint auth Meta
          - Modal hilang, session finalized
          - Scroll-triggered XHR ke /api/graphql mulai fire normal

        Tanpa step ini, scraper bisa stuck dengan `responses_seen=0` walaupun
        cookies valid.

        Return True kalau modal terdeteksi & di-klik; False kalau gak ada modal
        (session sudah authenticated penuh).
        """
        # Combined locator: role=button containing 'Lanjutkan dengan Instagram'
        # atau 'Continue with Instagram'. Pakai regex supaya tahan locale variant
        # (id-ID default, tapi user/CI bisa override locale).
        try:
            button = page.locator('div[role="button"]').filter(
                has_text=re.compile(
                    r'(Lanjutkan dengan|Continue with)\s+Instagram',
                    re.IGNORECASE,
                )
            ).first

            try:
                button.wait_for(state='visible', timeout=timeout_ms)
            except PWTimeout:
                logger.debug(
                    "[threads] No login choice modal (session sudah authenticated)"
                )
                return False

            logger.info(
                "[threads] Login choice modal terdeteksi — klik 'Continue with Instagram'"
            )

            # Tiered click fallback. Threads kadang render backdrop overlay yang
            # nge-intercept pointer events — Playwright lihat tombol visible tapi
            # actionability check gagal. Strategi:
            #   1. Normal click — proper event sequence, paling sering work
            #   2. Force click — bypass actionability check, dispatch at coords
            #   3. JS-dispatch click via evaluate — panggil el.click() di DOM,
            #      bypass hit-testing total. Last resort tapi paling reliable
            #      untuk kasus pointer intercept.
            clicked = False
            for strategy_name, action in [
                ('normal',
                 lambda: button.click(timeout=3_000)),
                ('force',
                 lambda: button.click(force=True, timeout=3_000)),
                ('js-dispatch',
                 lambda: button.evaluate("(el) => el.click()")),
            ]:
                try:
                    action()
                    clicked = True
                    if strategy_name != 'normal':
                        logger.debug(
                            f"[threads] Modal click succeeded via {strategy_name} fallback"
                        )
                    break
                except Exception as e:
                    logger.debug(
                        f"[threads] Click strategy '{strategy_name}' failed: "
                        f"{type(e).__name__}: {str(e)[:120]}"
                    )

            if not clicked:
                logger.warning(
                    "[threads] All modal click strategies failed; lanjut tanpa dismiss"
                )
                return False

            # Tunggu efek klik: bisa dismiss in-place atau trigger soft reload
            page.wait_for_timeout(3_000)

            # Verify modal hilang (best-effort, jangan fatal)
            try:
                button.wait_for(state='hidden', timeout=5_000)
                logger.info("[threads] Login modal dismissed, session finalized")
            except PWTimeout:
                logger.warning(
                    "[threads] Modal masih visible after click; lanjut anyway"
                )

            return True

        except Exception as e:
            logger.warning(f"[threads] Modal dismiss attempt failed (non-fatal): {e}")
            return False

    # ------------------------------------------------------------------------
    # Response listener
    # ------------------------------------------------------------------------

    def _attach_response_listener(
        self, page, collected: Dict[str, Dict], stats: Dict[str, int],
    ):
        def on_response(response):
            url = response.url
            # Filter ke GraphQL endpoint only
            if THREADS_GRAPHQL_PATH not in url:
                return

            try:
                body = response.text()
            except Exception as e:
                logger.debug(f"[threads] Can't read response body: {e}")
                return

            # Confirm ini search response (bukan notif/viewer/dll)
            if not _response_body_looks_like_search(body):
                return

            stats['responses_seen'] += 1

            try:
                posts = _extract_posts_from_response_body(body)
            except Exception as e:
                stats['parse_errors'] += 1
                logger.debug(f"[threads] Parse error: {e}")
                return

            if not posts:
                return

            stats['responses_with_posts'] += 1
            for p in posts:
                pid = p['id']
                if pid not in collected:
                    collected[pid] = p

        page.on('response', on_response)

    # ------------------------------------------------------------------------
    # Scroll & wait helpers
    # ------------------------------------------------------------------------

    def _wait_for_initial_data(
        self, page, collected: Dict[str, Dict], stats: Dict[str, int],
        timeout_ms: int = 20_000,
    ):
        """Wait initial GraphQL response feed datang."""
        page.wait_for_timeout(5_000)

        if stats.get('responses_with_posts', 0) > 0:
            logger.debug(
                f"[threads] Initial feed arrived in 5s "
                f"(responses_seen={stats['responses_seen']}, posts={len(collected)})"
            )
            page.wait_for_timeout(2_000)
            return

        elapsed = 5_000
        interval = 500
        while elapsed < timeout_ms:
            if stats.get('responses_with_posts', 0) > 0:
                logger.debug(
                    f"[threads] Feed arrived after {elapsed}ms "
                    f"(responses_seen={stats['responses_seen']}, posts={len(collected)})"
                )
                page.wait_for_timeout(2_000)
                return
            page.wait_for_timeout(interval)
            elapsed += interval

        logger.warning(
            f"[threads] No feed responses after {timeout_ms}ms "
            f"(responses_seen={stats.get('responses_seen', 0)}) — lanjut scroll anyway"
        )

    def _scroll_until_target(
        self, page, collected: Dict[str, Dict], target: int,
    ):
        """Scroll incremental untuk trigger pagination."""
        prev_count = -1
        no_new = 0
        for i in range(self.MAX_SCROLLS):
            if len(collected) >= target:
                logger.debug(f"[threads] Reached target {target}, stop scroll")
                return

            try:
                page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            except Exception:
                pass

            page.wait_for_timeout(self.SCROLL_WAIT_MS)

            current = len(collected)
            if current == prev_count:
                no_new += 1
                if no_new >= self.SCROLL_STUCK_THRESHOLD:
                    logger.debug(
                        f"[threads] Scroll stuck at {current} after {i+1} iterations"
                    )
                    return
            else:
                no_new = 0
                prev_count = current

    # ------------------------------------------------------------------------
    # Cookie injection, route handling, helpers
    # ------------------------------------------------------------------------

    def _inject_cookies(self, context, state: dict):
        """Apply Playwright storage_state ke context."""
        if not isinstance(state, dict):
            raise Exception(
                f"Cookie state harus dict storage_state, got {type(state).__name__}"
            )

        cookies = state.get('cookies') or []
        if not cookies:
            raise Exception("Cookie state kosong (cookies list empty).")

        pw_cookies = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = c.get('name')
            value = c.get('value')
            if not name or value in (None, ''):
                continue
            cookie = {
                'name': name,
                'value': str(value),
                'domain': c.get('domain') or '.instagram.com',
                'path': c.get('path') or '/',
                'httpOnly': bool(c.get('httpOnly', False)),
                'secure': bool(c.get('secure', True)),
                'sameSite': c.get('sameSite') or 'Lax',
            }
            if 'expires' in c and c['expires'] is not None:
                try:
                    cookie['expires'] = float(c['expires'])
                except (TypeError, ValueError):
                    pass
            pw_cookies.append(cookie)

        if pw_cookies:
            context.add_cookies(pw_cookies)

        # localStorage per-origin (kalau ada — Threads simpan beberapa flag client-side)
        origins = state.get('origins') or []
        for origin_entry in origins:
            if not isinstance(origin_entry, dict):
                continue
            origin = origin_entry.get('origin')
            items = origin_entry.get('localStorage') or []
            if not origin or not items:
                continue

            valid_items = [
                {'name': str(it.get('name')), 'value': str(it.get('value'))}
                for it in items
                if isinstance(it, dict) and it.get('name') and it.get('value') is not None
            ]
            if not valid_items:
                continue

            try:
                origin_json = json.dumps(origin)
                items_json = json.dumps(valid_items)
                init_script = (
                    "(() => { try {"
                    f"  if (location.origin !== {origin_json}) return;"
                    f"  const items = {items_json};"
                    "   for (const it of items) {"
                    "     try { localStorage.setItem(it.name, it.value); } catch(e) {}"
                    "   }"
                    "} catch(e) {} })();"
                )
                context.add_init_script(init_script)
            except Exception as e:
                logger.warning(
                    f"[threads] localStorage inject skipped for {origin}: {e}"
                )

    def _handle_route(self, route):
        """Block resource yang gak relevan untuk scraping."""
        request = route.request
        rtype = request.resource_type
        url = request.url

        if rtype in BLOCKED_RESOURCE_TYPES:
            return route.abort()
        if any(d in url for d in BLOCKED_DOMAIN_FRAGMENTS):
            return route.abort()
        return route.continue_()

    def _save_debug_artifacts(self, page):
        try:
            data_dir = os.environ.get('DATA_DIR', '/data')
            html_path = f"{data_dir}/threads_debug_last.html"
            png_path = f"{data_dir}/threads_debug_screenshot.png"
            with open(html_path, 'w', encoding='utf-8') as fh:
                fh.write(f"<!-- url: {page.url} -->\n")
                fh.write(page.content())
            page.screenshot(path=png_path, full_page=False)
            logger.info(f"[threads] Debug artifacts saved: {html_path}, {png_path}")
        except Exception as e:
            logger.debug(f"[threads] Couldn't save debug artifacts: {e}")

    def _infer_status(self, error: Exception):
        s = str(error).lower()
        if 'login' in s or 'expired' in s or 'cookie' in s:
            return self.account_manager.STATUS_EXPIRED
        if 'checkpoint' in s or 'verify' in s or 'challenge' in s:
            return self.account_manager.STATUS_CHALLENGE
        if '429' in s or 'rate' in s or 'too many' in s:
            return self.account_manager.STATUS_RATE_LIMITED
        if 'suspended' in s or 'banned' in s:
            return self.account_manager.STATUS_BANNED
        return None


# Cleanup hook
import atexit
atexit.register(_worker.shutdown)