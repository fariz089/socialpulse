"""
Twitter / X Scraper (Playwright + GraphQL response interception)
==================================================================

VERSION: v3.0 (7 May 2026)

ARSITEKTUR:
  Sama dengan Facebook & Instagram scraper: SKIP DOM scraping, pakai
  GraphQL response yang Twitter sendiri fetch saat search loading.

  Workflow:
    1. Navigate ke x.com/search?q={keyword}&f=live  (Latest tab)
    2. Pasang response listener BEFORE goto()
    3. Wait initial GraphQL responses settle, extract tweets
    4. Scroll untuk pagination
    5. Dedup by tweet id, return top `amount`

  v3.0 changes (7 May 2026): MAJOR REWRITE
    - Hapus dependency `twikit`. Twitter scraping sekarang full Playwright.
    - Pakai cookie/storage_state dengan auth_token + ct0 minimum.
    - Endpoint utama: x.com/search?q={keyword}&f=live  (latest tweets)
    - GraphQL response intercept untuk:
      * /i/api/graphql/.../SearchTimeline
      * /i/api/graphql/.../SearchByRawQuery
      Twitter pakai random hash-prefix di URL GraphQL (mis.
      `/graphql/abcdef123/SearchTimeline`), jadi kita filter by suffix
      'SearchTimeline' / 'SearchByRawQuery' di URL path.

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
from email.utils import parsedate_to_datetime
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
    'analytics.twitter.com',
    'ads-twitter.com',
)

# URL fragments yang tandain response feed search
TWITTER_SEARCH_FRAGMENTS = (
    'SearchTimeline',
    'SearchByRawQuery',
    '/i/api/2/search/',
    '/i/api/graphql',  # broad capture supaya gak miss endpoint lain
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
            self._thread = threading.Thread(target=self._run, daemon=True, name='tw-pw-worker')
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
                logger.warning("[twitter] Browser disconnected, re-launching")
                _close_browser()

            logger.info("[twitter] Launching headless Chromium")
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
                logger.info("[twitter] Worker thread shutdown")
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


def _parse_twitter_date(date_str: str) -> Optional[int]:
    """
    Parse Twitter date format → Unix timestamp.
    Twitter pakai: "Wed Apr 30 12:34:56 +0000 2026"
    """
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp())
    except Exception:
        return None


def _extract_tweet_from_legacy(
    legacy: dict,
    user_legacy: dict,
    user_core: Optional[dict] = None,
    user_avatar: Optional[dict] = None,
) -> Optional[Dict]:
    """
    Convert tweet 'legacy' object (Twitter v1.1-style fields) ke dict
    standar SocialPulse.

    Twitter GraphQL response, skema LAMA:
      result.legacy = { id_str, full_text, created_at, favorite_count, ... }
      result.core.user_results.result.legacy = { screen_name, name,
                                                 profile_image_url_https }

    Skema BARU (X mid-2025+): user fields dipindah dari legacy ke core/avatar:
      result.core.user_results.result.core   = { screen_name, name }
      result.core.user_results.result.avatar = { image_url }
      result.core.user_results.result.legacy = {} (screen_name kosong di sini)

    Kita fallback: core dulu (paling baru), legacy kedua, biar tahan kedua skema.
    """
    if not isinstance(legacy, dict):
        return None

    tid = legacy.get('id_str') or legacy.get('id')
    if not tid:
        return None

    text = legacy.get('full_text') or legacy.get('text') or ''
    created_at = legacy.get('created_at')
    timestamp_unix = _parse_twitter_date(created_at)

    user_legacy = user_legacy if isinstance(user_legacy, dict) else {}
    user_core = user_core if isinstance(user_core, dict) else {}
    user_avatar = user_avatar if isinstance(user_avatar, dict) else {}

    # Skema baru taruh di user.core; skema lama di user.legacy. Coba keduanya.
    screen_name = (
        user_core.get('screen_name')
        or user_legacy.get('screen_name')
        or 'unknown'
    )
    user_name = user_core.get('name') or user_legacy.get('name')
    avatar = (
        user_avatar.get('image_url')
        or user_legacy.get('profile_image_url_https')
        or user_legacy.get('profile_image_url')
    )

    # Engagement fields ada di legacy
    fav_count = legacy.get('favorite_count') or 0
    rt_count = legacy.get('retweet_count') or 0
    reply_count = legacy.get('reply_count') or 0
    quote_count = legacy.get('quote_count') or 0
    # view_count: di Twitter sekarang ada di luar legacy, di 'views.count'
    # caller yang isi field ini

    try:
        fav_count = int(fav_count or 0)
    except (TypeError, ValueError):
        fav_count = 0
    try:
        rt_count = int(rt_count or 0)
    except (TypeError, ValueError):
        rt_count = 0
    try:
        reply_count = int(reply_count or 0)
    except (TypeError, ValueError):
        reply_count = 0
    try:
        quote_count = int(quote_count or 0)
    except (TypeError, ValueError):
        quote_count = 0

    return {
        'platform': 'twitter',
        'id': str(tid),
        'shortCode': str(tid),
        'ownerUsername': screen_name,
        'username': screen_name,
        'authorName': user_name,
        'profilePicUrl': str(avatar) if avatar else None,
        'profile_pic_url': str(avatar) if avatar else None,
        'caption': text,
        'text': text,
        'likesCount': fav_count,
        'like_count': fav_count,
        'commentsCount': reply_count,
        'comment_count': reply_count,
        'shareCount': rt_count,
        'quoteCount': quote_count,
        'videoViewCount': 0,
        'video_view_count': 0,
        'timestamp': timestamp_unix,
        'taken_at': timestamp_unix,
        'lang': legacy.get('lang'),
        'url': f'https://x.com/{screen_name}/status/{tid}' if screen_name and tid else '',
    }


def _walk_for_tweets(obj: Any, posts: List[Dict], seen_ids: set, depth: int = 0):
    """
    Recursive walk untuk cari tweet 'result' nodes di GraphQL response.
    Twitter response shape:
      data.search_by_raw_query.search_timeline.timeline.instructions[]
        .entries[].content.itemContent.tweet_results.result
          .__typename = "Tweet"
          .legacy = {...}
          .core.user_results.result.legacy = {...}
          .views.count = "..."

    Heuristik: kalau dict punya __typename in ("Tweet", "TweetWithVisibilityResults")
    DAN punya 'legacy' subdict, treat sebagai tweet result.
    """
    if depth > 12:
        return

    if isinstance(obj, dict):
        typename = obj.get('__typename')
        if typename in ('Tweet', 'TweetWithVisibilityResults'):
            # Unwrap visibility wrapper kalau perlu
            tweet_node = obj.get('tweet') if typename == 'TweetWithVisibilityResults' else obj
            if isinstance(tweet_node, dict):
                legacy = tweet_node.get('legacy')
                # X sudah pindahin screen_name & name dari user.legacy ke user.core
                # (dan profile_image ke user.avatar). Ambil ketiganya, biarkan
                # extractor yang fallback antar-skema.
                user_result = _safe_get(
                    tweet_node, 'core', 'user_results', 'result',
                    default={}
                ) or {}
                user_legacy = user_result.get('legacy') if isinstance(user_result, dict) else {}
                user_core = user_result.get('core') if isinstance(user_result, dict) else {}
                user_avatar = user_result.get('avatar') if isinstance(user_result, dict) else {}
                post = _extract_tweet_from_legacy(
                    legacy, user_legacy, user_core, user_avatar
                )
                if post:
                    # Try dapat view_count dari views.count
                    views = _safe_get(tweet_node, 'views', 'count', default=None)
                    if views is not None:
                        try:
                            vc = int(views)
                            post['videoViewCount'] = vc
                            post['video_view_count'] = vc
                        except (TypeError, ValueError):
                            pass
                    pid = post.get('id')
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        posts.append(post)
            # Tetap descend kadang quoted_status_result punya tweet juga

        # Recurse children
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_for_tweets(v, posts, seen_ids, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _walk_for_tweets(item, posts, seen_ids, depth + 1)


def _extract_posts_from_response_body(body: str) -> List[Dict]:
    """Parse Twitter response body, walk untuk cari Tweet nodes."""
    if not body or not body.strip():
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []

    posts: List[Dict] = []
    seen: set = set()
    _walk_for_tweets(data, posts, seen)
    return posts


# ============================================================================
# TwitterScraper main class
# ============================================================================

class TwitterScraper(BaseScraper):

    PLATFORM = "twitter"
    BASE = "https://x.com"

    NAV_TIMEOUT = 45_000
    SELECTOR_TIMEOUT = 20_000

    MAX_SCROLLS = 15
    SCROLL_WAIT_MS = 2_500
    SCROLL_STUCK_THRESHOLD = 3

    # f=live (Latest), f=top (Top), f=user (People), f=image, f=video
    DEFAULT_SEARCH_TAB = 'live'

    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        """Search tweets by keyword (operator Twitter boleh dimasukin)."""
        if not keyword.strip():
            raise Exception("Keyword kosong")

        username = self.account_manager.pick_next_active()
        if not username:
            raise Exception("No active Twitter accounts available")

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
                f"[twitter] Scraped {len(sorted_posts)} tweets for '{keyword}' via @{username}"
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

        # Stealth init script — patch navigator.webdriver, plugins, WebGL,
        # window.chrome stub, dll. Twitter cek navigator.webdriver agresif —
        # tanpa stealth, scrape kena 401/redirect ke /i/flow/login.
        try:
            context.add_init_script(STEALTH_INIT_JS)
        except Exception as e:
            logger.warning(f"[twitter] Stealth inject failed: {e}")

        collected: Dict[str, Dict] = {}
        stats = {'responses_seen': 0, 'responses_with_tweets': 0, 'parse_errors': 0}

        try:
            self._inject_cookies(context, cookies)
            context.route('**/*', self._handle_route)

            page = context.new_page()
            page.set_default_navigation_timeout(self.NAV_TIMEOUT)
            page.set_default_timeout(self.SELECTOR_TIMEOUT)

            self._attach_response_listener(page, collected, stats)

            # Twitter search URL — tab Latest (chronological)
            search_url = f"{self.BASE}/search?q={quote(keyword)}&src=typed_query&f={self.DEFAULT_SEARCH_TAB}"
            logger.info(f"[twitter] Navigating to {search_url}")
            try:
                page.goto(search_url, wait_until='domcontentloaded')
            except PWTimeout:
                logger.warning("[twitter] Navigation timeout; lanjut anyway")

            current_url = page.url.lower()
            if any(p in current_url for p in [
                '/login', '/i/flow/login', '/account/access',
            ]):
                raise Exception(
                    f"Cookie expired or challenged (redirected to {current_url})"
                )

            self._wait_for_initial_data(page, collected, stats, timeout_ms=20_000)
            self._scroll_until_target(page, collected, target=amount)
            page.wait_for_timeout(2_000)

            posts = list(collected.values())
            logger.info(
                f"[twitter] Extraction: "
                f"responses_seen={stats['responses_seen']} "
                f"with_tweets={stats['responses_with_tweets']} "
                f"parse_errors={stats['parse_errors']} "
                f"unique_tweets={len(posts)}"
            )

            if not posts:
                logger.warning("[twitter] 0 tweets extracted")
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
        def on_response(response):
            url = response.url
            if not any(frag in url for frag in TWITTER_SEARCH_FRAGMENTS):
                return

            stats['responses_seen'] += 1

            try:
                body = response.text()
            except Exception as e:
                logger.debug(f"[twitter] Can't read response body: {e}")
                return

            try:
                posts = _extract_posts_from_response_body(body)
            except Exception as e:
                stats['parse_errors'] += 1
                logger.debug(f"[twitter] Parse error: {e}")
                return

            if not posts:
                return

            stats['responses_with_tweets'] += 1
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

        if stats.get('responses_with_tweets', 0) > 0:
            logger.debug(
                f"[twitter] Initial feed arrived in 5s "
                f"(responses_seen={stats['responses_seen']}, tweets={len(collected)})"
            )
            page.wait_for_timeout(2_000)
            return

        elapsed = 5_000
        interval = 500
        while elapsed < timeout_ms:
            if stats.get('responses_with_tweets', 0) > 0:
                logger.debug(
                    f"[twitter] Feed arrived after {elapsed}ms "
                    f"(responses_seen={stats['responses_seen']}, tweets={len(collected)})"
                )
                page.wait_for_timeout(2_000)
                return
            page.wait_for_timeout(interval)
            elapsed += interval

        logger.warning(
            f"[twitter] No feed responses after {timeout_ms}ms "
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
                logger.debug(f"[twitter] Reached target {target}, stop scroll")
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
                        f"[twitter] Scroll stuck at {current} after {i+1} iterations"
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
                'domain': c.get('domain') or '.x.com',
                'path': c.get('path') or '/',
                'httpOnly': bool(c.get('httpOnly', False)),
                'secure': bool(c.get('secure', True)),
                'sameSite': c.get('sameSite') or 'None',
            }
            if 'expires' in c and c['expires'] is not None:
                try:
                    cookie['expires'] = float(c['expires'])
                except (TypeError, ValueError):
                    pass
            pw_cookies.append(cookie)

        if pw_cookies:
            context.add_cookies(pw_cookies)

        # localStorage per-origin
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
                    f"[twitter] localStorage inject skipped for {origin}: {e}"
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
            html_path = f"{data_dir}/twitter_debug_last.html"
            png_path = f"{data_dir}/twitter_debug_screenshot.png"
            with open(html_path, 'w', encoding='utf-8') as fh:
                fh.write(f"<!-- url: {page.url} -->\n")
                fh.write(page.content())
            page.screenshot(path=png_path, full_page=False)
            logger.info(f"[twitter] Debug artifacts saved: {html_path}, {png_path}")
        except Exception as e:
            logger.debug(f"[twitter] Couldn't save debug artifacts: {e}")

    def _infer_status(self, error: Exception):
        s = str(error).lower()
        if 'login' in s or 'expired' in s or 'cookie' in s:
            return self.account_manager.STATUS_EXPIRED
        if 'checkpoint' in s or 'verify' in s or 'challenge' in s:
            return self.account_manager.STATUS_CHALLENGE
        if '429' in s or 'rate' in s or 'too many' in s:
            return self.account_manager.STATUS_RATE_LIMITED
        if 'suspended' in s:
            return self.account_manager.STATUS_BANNED
        return None


# Cleanup hook
import atexit
atexit.register(_worker.shutdown)