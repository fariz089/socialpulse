"""
Instagram Scraper (Playwright + GraphQL response interception)
================================================================

VERSION: v3.0 (7 May 2026)

ARSITEKTUR:
  Sama dengan FacebookScraper: SKIP DOM scraping, pakai GraphQL response
  yang IG sendiri fetch saat halaman tag/explore loading. Response itu
  berisi feed lengkap (timestamp Unix, like_count, caption, owner) dengan
  kualitas setara API resmi.

  Workflow:
    1. Untuk keyword tanpa #: navigate ke explore/tags/{keyword}/
    2. Pasang response listener BEFORE goto()
    3. Wait initial GraphQL responses settle, extract posts
    4. Scroll untuk trigger pagination, collect more
    5. Dedup by post id, return top `amount` sorted by timestamp desc

  v3.0 changes (7 May 2026): MAJOR REWRITE
    - Hapus dependency `instagrapi`. IG scraping sekarang full Playwright.
    - Pakai cookie/storage_state (mirror FacebookScraper architecture)
    - Endpoint utama: /explore/tags/{keyword}/  (hashtag feed page)
    - Fallback endpoint: /explore/search/keyword/?q={keyword}  (general search)
    - GraphQL response intercept untuk:
      * /api/v1/tags/web_info/ (hashtag metadata + recent media)
      * /graphql/query  (TagPageQuery / TopicalExploreRootQuery)
      * /api/v1/feed/tag/{tag}/ (paginated tag feed)

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
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .base import BaseScraper, to_jsonable
from ._stealth import STEALTH_INIT_JS, STEALTH_LAUNCH_ARGS

logger = logging.getLogger(__name__)

# Lazy import — kalau Playwright belum ke-install, scraper lain tetap jalan
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


# UA Chrome desktop terbaru — IG juga sensitif ke UA, harus modern
DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

BLOCKED_RESOURCE_TYPES = {'media', 'font'}
BLOCKED_DOMAIN_FRAGMENTS = (
    'doubleclick.net',
    'googletagmanager.com',
    'google-analytics.com',
    'connect.facebook.net',
)

# URL fragments yang menandakan response feed
IG_API_FRAGMENTS = (
    '/api/v1/tags/',
    '/api/v1/feed/tag/',
    '/api/v1/topsearch/',
    '/graphql/query',
    '/api/graphql',
)


# ============================================================================
# Raw response dump (debug only)
# ============================================================================
# Aktif saat env SOCIALPULSE_DUMP_RAW=1. Setiap response IG yang masuk
# difilter akan disimpan ke /tmp/socialpulse_dumps/ig/<timestamp>_<seq>.json
# bersama URL endpoint-nya. Plus kalau ada media yang `profilePicUrl=None`,
# satu sample `user` object dump di log warning supaya kita tahu key apa
# yang sebenarnya dikirim IG di response itu.

_DUMP_RAW = os.environ.get('SOCIALPULSE_DUMP_RAW', '').strip() in ('1', 'true', 'yes')
_DUMP_DIR_IG = '/tmp/socialpulse_dumps/ig'
_DUMP_SEQ = 0
_DUMP_LOCK = threading.Lock()

if _DUMP_RAW:
    try:
        os.makedirs(_DUMP_DIR_IG, exist_ok=True)
        logger.warning(f"[instagram] RAW DUMP enabled, writing to {_DUMP_DIR_IG}/")
    except Exception as e:
        logger.error(f"[instagram] Failed to create dump dir: {e}")


def _dump_raw_response(url: str, body: str, platform_dir: str = _DUMP_DIR_IG):
    """Save raw response body + URL hint to disk for offline inspection."""
    if not _DUMP_RAW:
        return
    global _DUMP_SEQ
    with _DUMP_LOCK:
        _DUMP_SEQ += 1
        seq = _DUMP_SEQ
    try:
        ts = int(time.time())
        # Take last URL segment for readable filename hint
        hint = url.rsplit('/', 2)[-2] if '/' in url else 'response'
        hint = ''.join(c if c.isalnum() else '_' for c in hint)[:30]
        path = f"{platform_dir}/{ts}_{seq:04d}_{hint}.json"
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"// URL: {url}\n")
            f.write(body)
        logger.debug(f"[dump] wrote {path} ({len(body)} bytes)")
    except Exception as e:
        logger.debug(f"[dump] write failed: {e}")


def _dump_user_sample(user_obj: Any, post_id: str):
    """Log key shape of a user/owner object that has no profile_pic_url."""
    if not _DUMP_RAW:
        return
    try:
        if isinstance(user_obj, dict):
            keys = sorted(user_obj.keys())
            sample = {k: user_obj[k] for k in keys[:20]}  # cap dump size
            logger.warning(
                f"[instagram] post {post_id} has no profile_pic_url. "
                f"User keys: {keys}. Sample: {json.dumps(sample, default=str)[:600]}"
            )
        else:
            logger.warning(
                f"[instagram] post {post_id} has user obj of type {type(user_obj).__name__}: {user_obj!r:.200}"
            )
    except Exception as e:
        logger.debug(f"[dump] user sample failed: {e}")


# ============================================================================
# Avatar cache (module-level, thread-safe)
# ============================================================================
# IG GraphQL endpoint sering kirim owner={id, username} tanpa profile_pic_url
# di response feed. Tapi kadang response REST sebelumnya untuk user yang sama
# sudah include avatar. Cache ini bridge gap-nya: sekali kita lihat avatar
# untuk username X, post selanjutnya dari X otomatis dapet avatar yang sama.
#
# Selain itu, dipakai juga sebagai persistent cache untuk fallback fetch via
# /api/v1/users/web_profile_info/ (lihat _backfill_missing_avatars di class).
#
# Kapasitas dibatasi supaya tidak grow unbounded di long-running process.

_AVATAR_CACHE: Dict[str, str] = {}
_AVATAR_CACHE_LOCK = threading.Lock()
_AVATAR_CACHE_MAX = 5000  # ~500KB worst case (avg URL 100 chars)


def _avatar_cache_set(username: str, url: str):
    """Thread-safe cache set with simple FIFO eviction when over limit."""
    if not username or not url:
        return
    with _AVATAR_CACHE_LOCK:
        if username in _AVATAR_CACHE:
            return  # don't overwrite (first-seen wins, usually highest quality)
        if len(_AVATAR_CACHE) >= _AVATAR_CACHE_MAX:
            # Drop ~10% oldest entries (FIFO via dict insertion order)
            drop_n = _AVATAR_CACHE_MAX // 10
            for k in list(_AVATAR_CACHE.keys())[:drop_n]:
                _AVATAR_CACHE.pop(k, None)
        _AVATAR_CACHE[username] = url


# ============================================================================
# Worker thread untuk Playwright (sync API not thread-safe)
# ============================================================================

class _PlaywrightWorker:
    """
    Single dedicated thread yang own semua Playwright operations.
    Mirror dari FacebookScraper worker. Beda instance, supaya browser
    untuk IG terpisah dari FB (avoid context contamination).
    """

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
            self._thread = threading.Thread(target=self._run, daemon=True, name='ig-pw-worker')
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
                logger.warning("[instagram] Browser disconnected, re-launching")
                _close_browser()

            logger.info("[instagram] Launching headless Chromium")
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
                logger.info("[instagram] Worker thread shutdown")
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
            try:
                self._queue.put(self._SHUTDOWN)
                if self._thread:
                    self._thread.join(timeout=5)
            except Exception:
                pass


_worker = _PlaywrightWorker()


# ============================================================================
# Response parsing helpers
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


def _extract_one_post(media: dict) -> Optional[Dict]:
    """
    Convert IG media node → dict standar SocialPulse.

    IG media node bentuknya bervariasi tergantung endpoint:
      - /api/v1/tags/web_info/  → recent.sections[].layout_content.medias[].media
      - /api/v1/feed/tag/       → items[]
      - /graphql TagPageQuery   → data.xdt_tag_v2.recent.edges[].node
      - /graphql TopicalExplore → data.xdt_topical_explore_root.edges[].node

    Common fields yang kita extract:
      pk / id           → post ID
      code              → shortcode (untuk URL)
      user.{username, profile_pic_url, pk}
      caption.text
      like_count, comment_count, play_count
      taken_at          → Unix timestamp

    Return None kalau bukan post valid.
    """
    if not isinstance(media, dict):
        return None

    pk = media.get('pk') or media.get('id')
    code = media.get('code') or media.get('shortcode') or media.get('shortCode')

    if not pk and not code:
        return None

    # User info — bisa di 'user' (REST) atau 'owner' (GraphQL)
    user = media.get('user') or media.get('owner') or {}
    if not isinstance(user, dict):
        user = {}
    username = user.get('username')
    # Profile pic — IG taruhnya di banyak lokasi tergantung endpoint/response shape:
    #   user.profile_pic_url                              (REST /tags/web_info)
    #   user.profile_pic_url_hd                           (HD variant, modern responses)
    #   user.hd_profile_pic_url_info.url                  (mobile-style payload)
    #   user.hd_profile_pic_versions[-1].url              (multi-resolution)
    #   media.coauthor_producers[0].profile_pic_url       (collab posts)
    #   media.invited_coauthor_producers[0].profile_pic_url
    #   user.profilePicUrl                                (camelCase, rare)
    avatar = (
        user.get('profile_pic_url')
        or user.get('profile_pic_url_hd')
        or user.get('profilePicUrl')
        or _safe_get(user, 'hd_profile_pic_url_info', 'url')
    )
    if not avatar:
        versions = user.get('hd_profile_pic_versions') or []
        if isinstance(versions, list) and versions:
            last = versions[-1]
            if isinstance(last, dict):
                avatar = last.get('url')
    # Coauthor fallback — kalau owner gak punya avatar, cek coauthor produsers
    if not avatar:
        for key in ('coauthor_producers', 'invited_coauthor_producers'):
            producers = media.get(key)
            if isinstance(producers, list) and producers:
                for p in producers:
                    if isinstance(p, dict):
                        cand = (
                            p.get('profile_pic_url')
                            or p.get('profile_pic_url_hd')
                            or _safe_get(p, 'hd_profile_pic_url_info', 'url')
                        )
                        if cand:
                            avatar = cand
                            break
                if avatar:
                    break

    # Username→avatar cache: kalau response sebelumnya sudah punya avatar untuk
    # username ini, populate dari cache. Banyak GraphQL TagPageQuery cuma kirim
    # owner={id, username} tanpa profile_pic, padahal sebelumnya REST endpoint
    # sudah ngasih avatar untuk user yang sama.
    if not avatar and username:
        with _AVATAR_CACHE_LOCK:
            cached = _AVATAR_CACHE.get(username)
        if cached:
            avatar = cached

    # Update cache kalau kita BARU saja extract avatar untuk username ini
    if avatar and username:
        _avatar_cache_set(username, str(avatar))

    # Debug hook: dump user shape kalau profile pic ttp gak ada
    if not avatar:
        _dump_user_sample(user, str(pk or code or '?'))

    # Caption — bisa nested 'caption.text' atau 'edge_media_to_caption.edges[0].node.text'
    caption_text = ''
    cap = media.get('caption')
    if isinstance(cap, dict):
        caption_text = cap.get('text') or ''
    elif isinstance(cap, str):
        caption_text = cap
    if not caption_text:
        # GraphQL legacy
        edges = _safe_get(media, 'edge_media_to_caption', 'edges', default=[]) or []
        if edges and isinstance(edges[0], dict):
            caption_text = _safe_get(edges[0], 'node', 'text', default='') or ''

    # Engagement
    like_count = (
        media.get('like_count')
        or _safe_get(media, 'edge_liked_by', 'count')
        or _safe_get(media, 'edge_media_preview_like', 'count')
        or 0
    )
    comment_count = (
        media.get('comment_count')
        or _safe_get(media, 'edge_media_to_comment', 'count')
        or _safe_get(media, 'edge_media_to_parent_comment', 'count')
        or 0
    )
    play_count = (
        media.get('play_count')
        or media.get('view_count')
        or _safe_get(media, 'video_view_count')
        or 0
    )

    # Timestamp
    taken_at = media.get('taken_at') or media.get('taken_at_timestamp')
    if isinstance(taken_at, (int, float)):
        timestamp_unix = int(taken_at)
    else:
        timestamp_unix = None

    # Coerce numerics
    try:
        like_count = int(like_count or 0)
    except (TypeError, ValueError):
        like_count = 0
    try:
        comment_count = int(comment_count or 0)
    except (TypeError, ValueError):
        comment_count = 0
    try:
        play_count = int(play_count or 0)
    except (TypeError, ValueError):
        play_count = 0

    url = f'https://www.instagram.com/p/{code}/' if code else ''

    return {
        'platform': 'instagram',
        'id': str(pk) if pk else (code or ''),
        'shortCode': code,
        'ownerUsername': username,
        'username': username,
        'profilePicUrl': str(avatar) if avatar else None,
        'profile_pic_url': str(avatar) if avatar else None,
        'caption': caption_text,
        'text': caption_text,
        'likesCount': like_count,
        'like_count': like_count,
        'commentsCount': comment_count,
        'comment_count': comment_count,
        'videoViewCount': play_count,
        'video_view_count': play_count,
        'timestamp': timestamp_unix,
        'taken_at': timestamp_unix,
        'url': url,
    }


def _walk_for_medias(obj: Any, posts: List[Dict], seen_ids: set, depth: int = 0):
    """
    Recursive walk through response JSON untuk cari media-shaped nodes.
    IG response shape sangat bervariasi (REST vs GraphQL vs deferred chunks),
    jadi pakai walker generic: kalau dict punya key kombinasi yang khas
    (pk/id + caption/like_count/taken_at), treat sebagai media.

    Anti-recursion: cap depth di 8 supaya tidak infinite loop.
    """
    if depth > 8:
        return
    if isinstance(obj, dict):
        # Heuristik: kalau dict punya 'pk' atau 'id' DAN salah satu dari
        # ('caption', 'like_count', 'taken_at', 'edge_liked_by'), kemungkinan
        # besar ini media node.
        has_id = 'pk' in obj or ('id' in obj and isinstance(obj.get('id'), (str, int)))
        has_media_signal = any(
            k in obj for k in (
                'caption', 'like_count', 'taken_at',
                'taken_at_timestamp', 'edge_liked_by',
                'play_count', 'comment_count',
            )
        )
        if has_id and has_media_signal:
            post = _extract_one_post(obj)
            if post:
                pid = post.get('id')
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    posts.append(post)
            # Tetap descend — kadang ada nested attached_media

        # Special-case path: GraphQL "node" wrapper
        if 'node' in obj and isinstance(obj['node'], dict):
            _walk_for_medias(obj['node'], posts, seen_ids, depth + 1)
            return

        # Recurse ke children
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_for_medias(v, posts, seen_ids, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _walk_for_medias(item, posts, seen_ids, depth + 1)


def _extract_posts_from_response_body(body: str) -> List[Dict]:
    """
    Parse response body (single JSON), walk untuk cari media nodes.
    Return [] kalau parse fail.
    """
    if not body or not body.strip():
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []

    posts: List[Dict] = []
    seen: set = set()
    _walk_for_medias(data, posts, seen)
    return posts


# ============================================================================
# InstagramScraper main class
# ============================================================================

class InstagramScraper(BaseScraper):

    PLATFORM = "instagram"
    BASE = "https://www.instagram.com"

    NAV_TIMEOUT = 45_000  # ms
    SELECTOR_TIMEOUT = 20_000  # ms

    MAX_SCROLLS = 12
    SCROLL_WAIT_MS = 2_500
    SCROLL_STUCK_THRESHOLD = 3

    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        """Scrape recent posts dari hashtag (tanpa #)."""
        keyword = keyword.lstrip('#').strip()
        if not keyword:
            raise Exception("Keyword kosong")

        username = self.account_manager.pick_next_active()
        if not username:
            raise Exception("No active Instagram accounts available")

        self.last_used_account = username
        cookies = self.account_manager.get_cookies(username)

        try:
            posts = self._search_with_browser(keyword, cookies, amount)
            self.account_manager.mark_used(username)

            # Sort by timestamp desc, kalau ada
            sorted_posts = sorted(
                posts,
                key=lambda p: p.get('taken_at') or p.get('timestamp') or 0,
                reverse=True,
            )
            logger.info(
                f"[instagram] Scraped {len(sorted_posts)} posts for #{keyword} via @{username}"
            )
            return sorted_posts[:amount]
        except Exception as e:
            self.account_manager.mark_error(username, e, status=self._infer_status(e))
            raise

    def _search_with_browser(
        self, keyword: str, cookies: dict, amount: int,
    ) -> List[Dict]:
        """Submit job ke worker thread (Playwright sync not thread-safe)."""
        def _do_scrape(browser):
            return self._scrape_inside_worker(browser, keyword, cookies, amount)
        return _worker.submit(_do_scrape, timeout=120.0)

    def _scrape_inside_worker(
        self, browser, keyword: str, cookies: dict, amount: int,
    ) -> List[Dict]:
        """
        Eksekusi inside worker thread.

        Strategi:
          1. Fresh context + inject cookies
          2. Pasang response listener BEFORE navigation
          3. Navigate ke /explore/tags/{keyword}/
          4. Wait + scroll untuk trigger more requests
          5. Dedup posts by id
        """
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={'width': 1366, 'height': 900},
            locale='id-ID',
            timezone_id='Asia/Jakarta',
            bypass_csp=True,
        )

        # Stealth init script — patch navigator.webdriver, plugins, WebGL,
        # window.chrome stub, dll. Inject SEBELUM cookies / route handler /
        # navigation supaya semua page yang dibuka context ini ke-cover.
        try:
            context.add_init_script(STEALTH_INIT_JS)
        except Exception as e:
            logger.warning(f"[instagram] Stealth inject failed: {e}")

        collected: Dict[str, Dict] = {}
        stats = {'responses_seen': 0, 'responses_with_media': 0, 'parse_errors': 0}

        try:
            self._inject_cookies(context, cookies)
            context.route('**/*', self._handle_route)

            page = context.new_page()
            page.set_default_navigation_timeout(self.NAV_TIMEOUT)
            page.set_default_timeout(self.SELECTOR_TIMEOUT)

            # Pasang listener SEBELUM goto
            self._attach_response_listener(page, collected, stats)

            # IG hashtag URL pattern
            search_url = f"{self.BASE}/explore/tags/{quote(keyword)}/"
            logger.info(f"[instagram] Navigating to {search_url}")
            try:
                page.goto(search_url, wait_until='domcontentloaded')
            except PWTimeout:
                logger.warning("[instagram] Navigation timeout; lanjut anyway")

            # Detect redirect ke login/checkpoint
            current_url = page.url.lower()
            if any(p in current_url for p in [
                '/accounts/login', '/challenge', '/checkpoint', '/two_factor',
            ]):
                raise Exception(
                    f"Cookie expired or challenged (redirected to {current_url})"
                )

            # Wait initial GraphQL responses
            self._wait_for_initial_data(page, collected, stats, timeout_ms=20_000)

            # Scroll untuk trigger pagination
            self._scroll_until_target(page, collected, target=amount)

            # Final settle
            page.wait_for_timeout(2_000)

            posts = list(collected.values())

            # Backfill avatars yang masih missing via /api/v1/users/web_profile_info/
            # IG GraphQL TagPageQuery sering hanya kirim owner={id, username} tanpa
            # profile_pic_url. Sebelum return, kita fetch endpoint user-profile
            # untuk tiap username unik yang avatar-nya masih kosong, lalu populate
            # ke posts[*]. Hasil fetch ditambahin ke _AVATAR_CACHE supaya scrape
            # berikutnya skip request yang sama.
            avatar_stats = self._backfill_missing_avatars(page, posts)

            logger.info(
                f"[instagram] Extraction: "
                f"responses_seen={stats['responses_seen']} "
                f"with_media={stats['responses_with_media']} "
                f"parse_errors={stats['parse_errors']} "
                f"unique_posts={len(posts)} "
                f"avatar_coverage={avatar_stats['with_avatar']}/{len(posts)} "
                f"(backfilled={avatar_stats['backfilled']}, "
                f"still_missing={avatar_stats['still_missing']})"
            )

            # Warn kalau coverage rendah — sinyal bahwa endpoint berubah lagi
            if posts and avatar_stats['with_avatar'] / len(posts) < 0.5:
                logger.warning(
                    f"[instagram] LOW avatar coverage: only "
                    f"{avatar_stats['with_avatar']}/{len(posts)} posts have avatar. "
                    f"IG response shape may have changed — set "
                    f"SOCIALPULSE_DUMP_RAW=1 to inspect."
                )

            if not posts:
                logger.warning("[instagram] 0 posts extracted")
                self._save_debug_artifacts(page)

            return [to_jsonable(p) for p in posts[:amount]]

        finally:
            try:
                context.close()
            except Exception:
                pass

    # ------------------------------------------------------------------------
    # Response listener
    # ------------------------------------------------------------------------

    def _attach_response_listener(
        self, page, collected: Dict[str, Dict], stats: Dict[str, int],
    ):
        """
        Pasang listener untuk response IG API endpoint (REST + GraphQL).
        Untuk tiap response, parse body, extract media, append ke collected.
        """
        def on_response(response):
            url = response.url
            # Filter: only API/GraphQL endpoints relevan
            if not any(frag in url for frag in IG_API_FRAGMENTS):
                return

            stats['responses_seen'] += 1

            try:
                body = response.text()
            except Exception as e:
                logger.debug(f"[instagram] Can't read response body: {e}")
                return

            _dump_raw_response(url, body)

            try:
                posts = _extract_posts_from_response_body(body)
            except Exception as e:
                stats['parse_errors'] += 1
                logger.debug(f"[instagram] Parse error: {e}")
                return

            if not posts:
                return

            stats['responses_with_media'] += 1
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
        """
        Tunggu response feed IG datang. IG biasanya kasih initial batch
        di response /api/v1/tags/web_info/ atau /graphql/query (TagPageQuery)
        dalam 3-6 detik setelah goto.
        """
        # Initial fixed wait
        page.wait_for_timeout(5_000)

        if stats.get('responses_with_media', 0) > 0:
            logger.debug(
                f"[instagram] Initial feed arrived in 5s "
                f"(responses_seen={stats['responses_seen']}, posts={len(collected)})"
            )
            page.wait_for_timeout(2_000)
            return

        # Polling kalau belum dapat feed
        elapsed = 5_000
        interval = 500
        while elapsed < timeout_ms:
            if stats.get('responses_with_media', 0) > 0:
                logger.debug(
                    f"[instagram] Feed arrived after {elapsed}ms "
                    f"(responses_seen={stats['responses_seen']}, posts={len(collected)})"
                )
                page.wait_for_timeout(2_000)
                return
            page.wait_for_timeout(interval)
            elapsed += interval

        logger.warning(
            f"[instagram] No feed responses after {timeout_ms}ms "
            f"(responses_seen={stats.get('responses_seen', 0)}) — lanjut scroll anyway"
        )

    def _scroll_until_target(
        self, page, collected: Dict[str, Dict], target: int,
    ):
        """
        Scroll incremental untuk trigger lazy-load. Stop kalau target tercapai
        atau scroll stuck N kali berturut-turut.
        """
        prev_count = -1
        no_new = 0
        for i in range(self.MAX_SCROLLS):
            if len(collected) >= target:
                logger.debug(f"[instagram] Reached target {target}, stop scroll")
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
                        f"[instagram] Scroll stuck at {current} after {i+1} iterations"
                    )
                    return
            else:
                no_new = 0
                prev_count = current

    # ------------------------------------------------------------------------
    # Avatar backfill (post-scrape, untuk username yang missing avatar)
    # ------------------------------------------------------------------------

    # Cap supaya tidak fetch puluhan endpoint per scrape
    BACKFILL_MAX_USERNAMES = 20
    BACKFILL_FETCH_TIMEOUT_MS = 8_000

    def _backfill_missing_avatars(self, page, posts: List[Dict]) -> Dict[str, int]:
        """
        Untuk tiap post yang profilePicUrl-nya None, coba fetch
        /api/v1/users/web_profile_info/?username=X di browser context (cookies
        authenticated otomatis). Hasil ditulis balik ke posts[*] dan disimpan
        di _AVATAR_CACHE supaya scrape berikutnya tidak refetch.

        Return stats dict: {with_avatar, backfilled, still_missing}.

        Kenapa fetch di browser context, bukan `requests`?
          - IG block plain HTTP client tanpa proper headers (CSRF, X-IG-App-ID).
          - page.evaluate(fetch(...)) inherit semua cookies + UA + origin yang
            sudah di-set sama Playwright. Same-origin → IG kasih response
            tanpa anti-bot challenge.
        """
        # 1. Coba isi dari cache dulu (zero HTTP)
        with _AVATAR_CACHE_LOCK:
            cache_snapshot = dict(_AVATAR_CACHE)

        for p in posts:
            if p.get('profilePicUrl'):
                continue
            uname = p.get('ownerUsername') or p.get('username')
            if not uname:
                continue
            cached = cache_snapshot.get(uname)
            if cached:
                p['profilePicUrl'] = cached
                p['profile_pic_url'] = cached

        # 2. Kumpulkan username unik yang MASIH belum ada avatar-nya
        missing_usernames: List[str] = []
        seen_missing: set = set()
        for p in posts:
            if p.get('profilePicUrl'):
                continue
            uname = p.get('ownerUsername') or p.get('username')
            if not uname or uname in seen_missing:
                continue
            seen_missing.add(uname)
            missing_usernames.append(uname)
            if len(missing_usernames) >= self.BACKFILL_MAX_USERNAMES:
                break

        backfilled_count = 0

        # 3. Fetch satu-per-satu (paralel via Promise.all dalam satu evaluate)
        if missing_usernames:
            try:
                fetched = self._fetch_user_profiles_in_browser(page, missing_usernames)
            except Exception as e:
                logger.debug(f"[instagram] Avatar backfill fetch failed: {e}")
                fetched = {}

            # 4. Apply hasil fetch ke posts + cache
            for uname, avatar_url in fetched.items():
                if not avatar_url:
                    continue
                _avatar_cache_set(uname, avatar_url)
                for p in posts:
                    pu = p.get('ownerUsername') or p.get('username')
                    if pu == uname and not p.get('profilePicUrl'):
                        p['profilePicUrl'] = avatar_url
                        p['profile_pic_url'] = avatar_url
                        backfilled_count += 1

        # 5. Final stats
        with_avatar = sum(1 for p in posts if p.get('profilePicUrl'))
        still_missing = len(posts) - with_avatar
        return {
            'with_avatar': with_avatar,
            'backfilled': backfilled_count,
            'still_missing': still_missing,
        }

    def _fetch_user_profiles_in_browser(
        self, page, usernames: List[str],
    ) -> Dict[str, Optional[str]]:
        """
        Fetch /api/v1/users/web_profile_info/?username=X untuk daftar usernames.
        Eksekusi di browser context via page.evaluate, paralel via Promise.all.
        Return dict {username: avatar_url_or_None}.

        Endpoint ini perlu header X-IG-App-ID (936619743392459 untuk web).
        Tanpa header itu, IG balik 401/403.
        """
        if not usernames:
            return {}

        try:
            usernames_json = json.dumps(usernames)
            timeout_ms = self.BACKFILL_FETCH_TIMEOUT_MS
            # Catatan: kita pakai AbortController per-request supaya satu user
            # yang lambat tidak block semuanya. Eksternal timeout via Playwright
            # default sudah ada via page.set_default_timeout, tapi page.evaluate
            # blocking sampai promise resolve, jadi explicit abort lebih aman.
            script = f"""
            (async () => {{
              const usernames = {usernames_json};
              const TIMEOUT_MS = {timeout_ms};
              const APP_ID = '936619743392459';
              const results = {{}};

              const fetchOne = async (uname) => {{
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
                try {{
                  const resp = await fetch(
                    `/api/v1/users/web_profile_info/?username=${{encodeURIComponent(uname)}}`,
                    {{
                      method: 'GET',
                      headers: {{
                        'X-IG-App-ID': APP_ID,
                        'Accept': '*/*',
                      }},
                      credentials: 'include',
                      signal: ctrl.signal,
                    }}
                  );
                  if (!resp.ok) {{
                    results[uname] = null;
                    return;
                  }}
                  const data = await resp.json();
                  const u = data && data.data && data.data.user;
                  if (!u) {{
                    results[uname] = null;
                    return;
                  }}
                  results[uname] = (
                    u.profile_pic_url_hd ||
                    u.profile_pic_url ||
                    null
                  );
                }} catch (e) {{
                  results[uname] = null;
                }} finally {{
                  clearTimeout(timer);
                }}
              }};

              await Promise.all(usernames.map(fetchOne));
              return results;
            }})()
            """
            result = page.evaluate(script)
            if isinstance(result, dict):
                # Cast values ke str/None
                cleaned: Dict[str, Optional[str]] = {}
                for k, v in result.items():
                    cleaned[str(k)] = str(v) if v else None
                return cleaned
            return {}
        except Exception as e:
            logger.debug(f"[instagram] page.evaluate backfill failed: {e}")
            return {}

    # ------------------------------------------------------------------------
    # Cookie injection, route handling, helpers
    # ------------------------------------------------------------------------

    def _inject_cookies(self, context, state: dict):
        """
        Apply Playwright storage_state ke context.
        Mirror dari FacebookScraper._inject_cookies.
        """
        if not isinstance(state, dict):
            raise Exception(
                f"Cookie state harus dict storage_state, got {type(state).__name__}"
            )

        cookies = state.get('cookies') or []
        if not cookies:
            raise Exception("Cookie state kosong (cookies list empty).")

        # --- Cookies ---
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

        # --- localStorage per-origin ---
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
                    f"[instagram] localStorage inject skipped for {origin}: {e}"
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
        """Save HTML + screenshot untuk inspect kalau extraction gagal."""
        try:
            data_dir = os.environ.get('DATA_DIR', '/data')
            html_path = f"{data_dir}/ig_debug_last.html"
            png_path = f"{data_dir}/ig_debug_screenshot.png"
            with open(html_path, 'w', encoding='utf-8') as fh:
                fh.write(f"<!-- url: {page.url} -->\n")
                fh.write(page.content())
            page.screenshot(path=png_path, full_page=False)
            logger.info(f"[instagram] Debug artifacts saved: {html_path}, {png_path}")
        except Exception as e:
            logger.debug(f"[instagram] Couldn't save debug artifacts: {e}")

    def _infer_status(self, error: Exception):
        s = str(error).lower()
        if 'login' in s or 'expired' in s or 'cookie' in s:
            return self.account_manager.STATUS_EXPIRED
        if 'checkpoint' in s or 'verify' in s or 'challenge' in s:
            return self.account_manager.STATUS_CHALLENGE
        if '429' in s or 'rate' in s or 'too many' in s:
            return self.account_manager.STATUS_RATE_LIMITED
        return None


# Cleanup hook
import atexit
atexit.register(_worker.shutdown)