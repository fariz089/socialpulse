"""
Facebook Scraper (Playwright + GraphQL response interception)
=============================================================

VERSION: v2.4 (6 May 2026)

ARSITEKTUR:
  Pendekatan: SKIP DOM scraping sepenuhnya. Pakai GraphQL response yang
  FB sendiri fetch saat halaman search loading. Response itu berisi feed
  lengkap (timestamp Unix, permalink canonical, author, engagement counts)
  dengan kualitas yang setara dengan API resmi.

  Workflow:
    1. Generate keyword variants (mis. "juragan99" → "juragan99",
       "juragan 99", "#juragan99")
    2. Untuk setiap variant × setiap endpoint (/search/posts/, /search/top/):
       a. Launch context + inject cookies
       b. Pasang response listener BEFORE goto()
       c. Navigate, wait, scroll up to MAX_SCROLLS
       d. Extract posts dari GraphQL responses, dedup local
    3. Merge semua pass, dedup global by post_id
    4. Sort by creation_time desc, return top `amount`

  v2.4 changes (6 May 2026, follow-up to v2.3):
    - Switch session storage dari flat {name: value} dict ke Playwright
      `storage_state` shape `{"cookies": [...], "origins": [...]}` —
      sama dengan output context.storage_state() Playwright dan format
      `fb_session.json` yang fb_capture pakai.
    - Cookies sekarang carry attribute lengkap: expires, httpOnly,
      sameSite, domain, path. Sebelumnya hardcoded default di
      _inject_cookies (httpOnly hanya untuk xs/fr, expires=session,
      semua secure=True).
    - LocalStorage dari `state.origins` di-inject ke context via
      add_init_script. Sebelumnya selalu kosong di fresh context kita —
      FB JS bisa baca beberapa flag (CacheStorageVersion, dll) untuk
      fingerprint/state. Tidak guaranteed mengubah ranking algorithm
      hasil search, tapi bikin session match lebih dekat dengan real
      user yang login.
    - facebook_accounts.py auto-detect 3 format input: storage_state JSON,
      flat dict legacy, semicolon string. Session file lama (flat dict)
      auto-migrated on read tanpa perlu re-add account.

  v2.3 changes (6 May 2026, follow-up to v2.2):
    - MAX_SCROLLS bumped 8 → 20. Observasi: untuk keyword brand kecil
      FB stop emit post baru di scroll ke-12-15, tapi MAX_SCROLLS=8
      kadang terlalu cepat berhenti dan miss page 2-3.
    - SCROLL_STUCK_THRESHOLD 2 → 3. FB kadang throttle 1 scroll lalu
      kasih batch baru di scroll berikut; 2 terlalu agresif bail-out.
    - Multi-pass sweep: keyword variants × endpoints. Untuk keyword
      "juragan99" sekarang scrape 3 variants × 2 endpoints = 6 passes,
      total ~3-5 menit. Hasil 2-3x lipat unique posts dibanding single-
      pass karena FB ranking algorithm beda per query/endpoint.
    - /search/top/ ditambahkan kembali sebagai SECONDARY (di v2.2 dibuang
      karena mix Pages/People/Reels). Filter post-only di extraction
      handle non-post results, jadi noise dari /top/ aman.

  v2.2 changes (kept):
    - Ganti endpoint dari /search/top/ ke /search/posts/. Alasan:
      /search/top/ adalah halaman "Top Results" yang HEAVILY PERSONALIZED
      per akun + di-mix dengan Pages/People/Reels. Akun Nanda Surya
      (production) konsisten cuma dapat 6 post reel/bus, sementara akun
      Isabella (capture tool) dapat 11 post termasuk Harian Disway —
      input keyword sama, beda akun, hasil beda 100%. /search/posts/ tab
      "Postingan" itu chronological + relevance-ranked, kurang sensitif
      ke personalisasi, dan return post-only (tidak campur Pages/People).

  v2.1 fixes (kept):
    - JANGAN block image type — FB kasih response feed jauh lebih sedikit
      ke browser yang tidak load image (response_count: 11 → 30+ setelah fix)
    - JANGAN exit _wait_for_initial_data di entry pertama — response yg
      datang duluan sering cuma metadata page, bukan feed. Tunggu sampai
      `responses_with_edges > 0`, bukan `len(collected) > 0`.
    - Scroll 2x viewport (bukan 1.5x) + fixed wait tanpa networkidle —
      networkidle di headless triggered prematur sebelum feed sampai.

  Yang DIBUANG dari versi DOM-based:
    - JS DOM extractor (~400 baris)
    - Recursive walker (kasih banyak false positive)
    - Matcher heuristic DOM↔GraphQL
    - Base64 token decoding

  Trade-off:
    - + Timestamp 100% akurat (Unix dari FB internal)
    - + URL canonical (no `?slaytics_kind=author_profile`)
    - + Engagement count exact (1965 likes, bukan "2K")
    - + Range historis luas (post 6+ bulan lalu juga keluar)
    - – Per-scrape latency naik ~5-10s vs versi awal v2 (image loaded +
      initial wait 6s) — total ~30-40s vs ~20s. Worth it untuk reliability.
    - – Bergantung pada response structure FB; kalau Meta ubah field name,
      extraction akan break. Mitigasi: logging detail di tiap step.

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


# UA Chrome desktop terbaru — harus valid supaya FB tidak kasih banner deprecation
DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

# Resource type yg di-block. PENTING: jangan block 'fetch' atau 'xhr' karena
# itu yang carry GraphQL response. Juga JANGAN block 'image' meskipun kelihatan
# heavy — observasi production: dengan image diblock, FB emit response feed
# jauh lebih sedikit (kemungkinan deteksi headless via "browser tidak load
# image"). Cuma block media (video) + font untuk speed.
BLOCKED_RESOURCE_TYPES = {'media', 'font'}
# NOTE: versi awal v2 block {'media', 'font', 'imageset'}; production test
# 6 May 2026 menunjukkan dengan image diload, response_count naik ~3x
# (11 → 30+). Trade-off ~3-5s lebih lambat per scrape, tapi data jauh lebih
# lengkap.

# Domain analytics/ads — di-block karena tidak relevan untuk scraping
BLOCKED_DOMAIN_FRAGMENTS = (
    'fbsbx.com/paid_ads_pixel',
    'connect.facebook.net',
    'doubleclick.net',
    'googletagmanager.com',
    'google-analytics.com',
)

# URL fragment yang menandakan endpoint kandidat berisi feed search.
# /api/graphql/ adalah endpoint utama; semua data post berasal dari sini.
GRAPHQL_URL_FRAGMENT = '/api/graphql/'


# ============================================================================
# Raw response dump (debug only)
# ============================================================================
# Aktif saat env SOCIALPULSE_DUMP_RAW=1. Setiap response GraphQL FB yang
# masuk filter akan disimpan ke /tmp/socialpulse_dumps/fb/<ts>_<seq>.json,
# plus actor object yang gagal extract profile pic akan di-log dgn key
# shape-nya. Tujuannya satu: kalau path profile pic FB di response sudah
# berubah lagi, kamu bisa lihat sendiri key apa yang sebenarnya dikirim.

_DUMP_RAW = os.environ.get('SOCIALPULSE_DUMP_RAW', '').strip() in ('1', 'true', 'yes')
_DUMP_DIR_FB = '/tmp/socialpulse_dumps/fb'
_DUMP_SEQ = 0
_DUMP_LOCK = threading.Lock()

if _DUMP_RAW:
    try:
        os.makedirs(_DUMP_DIR_FB, exist_ok=True)
        logger.warning(f"[facebook] RAW DUMP enabled, writing to {_DUMP_DIR_FB}/")
    except Exception as e:
        logger.error(f"[facebook] Failed to create dump dir: {e}")


def _dump_raw_response(url: str, body: str):
    if not _DUMP_RAW:
        return
    global _DUMP_SEQ
    with _DUMP_LOCK:
        _DUMP_SEQ += 1
        seq = _DUMP_SEQ
    try:
        ts = int(time.time())
        path = f"{_DUMP_DIR_FB}/{ts}_{seq:04d}.json"
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"// URL: {url}\n")
            f.write(body)
        logger.debug(f"[dump] wrote {path} ({len(body)} bytes)")
    except Exception as e:
        logger.debug(f"[dump] write failed: {e}")


def _dump_actor_sample(actor: Any, post_id: str):
    """Log key shape of an actor object that has no profile picture."""
    if not _DUMP_RAW:
        return
    try:
        if isinstance(actor, dict):
            keys = sorted(actor.keys())
            sample = {k: actor[k] for k in keys[:25]}
            logger.warning(
                f"[facebook] post {post_id} has no profile pic. "
                f"Actor keys: {keys}. Sample: {json.dumps(sample, default=str)[:600]}"
            )
        else:
            logger.warning(
                f"[facebook] post {post_id} actor is type {type(actor).__name__}: {actor!r:.200}"
            )
    except Exception as e:
        logger.debug(f"[dump] actor sample failed: {e}")


# ============================================================================
# Worker thread untuk Playwright (sync API not thread-safe)
# ============================================================================

class _PlaywrightWorker:
    """
    Single dedicated thread yang own semua Playwright operations.
    
    Kenapa perlu? Playwright sync API NOT thread-safe — invoke dari thread
    yang berbeda dari thread yang launch browser akan error
    "cannot switch to a different thread". Tapi Flask multi-threaded.
    Solusi: worker thread own Playwright, main threads queue jobs ke dia.
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
            self._thread = threading.Thread(target=self._run, daemon=True, name='fb-pw-worker')
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
                logger.warning("[facebook] Browser disconnected, re-launching")
                _close_browser()
            
            logger.info("[facebook] Launching headless Chromium")
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
                logger.info("[facebook] Worker thread shutdown")
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
# GraphQL response parsing helpers (module-level — pure functions, testable)
# ============================================================================

def _parse_multi_json(text: str) -> List[dict]:
    """
    Parse FB GraphQL response body — bisa multi-JSON (deferred/streamed).
    
    FB pakai @defer/@stream GraphQL: satu response berisi multiple JSON objects
    berurutan, dipisah whitespace/newline. Object pertama = main payload,
    sisanya = streaming patches dengan `label` + `path`.
    
    json.loads() biasa hanya parse object pertama → sisanya hilang.
    Solusi: pakai JSONDecoder.raw_decode() loop.
    """
    decoder = json.JSONDecoder()
    objs: List[dict] = []
    idx = 0
    n = len(text)
    while idx < n:
        # Skip whitespace
        while idx < n and text[idx] in ' \t\r\n':
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            # Bukan JSON valid mulai dari sini — stop (kemungkinan trailing junk)
            break
        if isinstance(obj, dict):
            objs.append(obj)
        idx = end
    return objs


def _safe_get(d: Any, *keys, default=None):
    """Nested dict access tanpa exception."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


def _extract_engagement(story: dict) -> tuple:
    """
    Extract reaction/comment/share count dari story node.
    Path FB 2026:
      story.comet_sections.feedback.story.story_ufi_container.story
        .feedback_context.feedback_target_with_context
        .comet_ufi_summary_and_actions_renderer.feedback.{reaction_count,...}
    """
    fb = _safe_get(
        story,
        'comet_sections', 'feedback', 'story',
        'story_ufi_container', 'story',
        'feedback_context', 'feedback_target_with_context',
        'comet_ufi_summary_and_actions_renderer', 'feedback',
        default={},
    ) or {}
    
    reactions = _safe_get(fb, 'reaction_count', 'count', default=0) or 0
    shares = _safe_get(fb, 'share_count', 'count', default=0) or 0
    comments = _safe_get(
        fb, 'comments_count_summary_renderer', 'feedback',
        'comment_rendering_instance', 'comments', 'total_count',
        default=0,
    ) or 0
    
    # Coerce to int (kadang FB return string)
    try:
        reactions = int(reactions)
    except (TypeError, ValueError):
        reactions = 0
    try:
        shares = int(shares)
    except (TypeError, ValueError):
        shares = 0
    try:
        comments = int(comments)
    except (TypeError, ValueError):
        comments = 0
    
    return reactions, comments, shares


def _extract_message(story: dict) -> str:
    """
    Extract caption / message text. FB inkonsisten lokasinya — coba beberapa
    path, return string pertama yang non-empty.
    """
    # Path A: comet_sections.content.story.comet_sections.message.story.message.text
    msg = _safe_get(
        story, 'comet_sections', 'content', 'story',
        'comet_sections', 'message', 'story', 'message', 'text',
    )
    if msg:
        return msg
    # Path B: top-level message.text (rare)
    msg = _safe_get(story, 'message', 'text')
    if msg:
        return msg
    # Path C: attached_story (untuk shared post / repost)
    msg = _safe_get(
        story, 'attached_story', 'comet_sections', 'content', 'story',
        'comet_sections', 'message', 'story', 'message', 'text',
    )
    return msg or ''


def _extract_creation_time(story: dict) -> Optional[int]:
    """
    Extract Unix timestamp (creation_time). Coba 2 lokasi karena FB kadang
    pakai berbeda untuk post regular vs grup vs reel.
    """
    # Path A: comet_sections.timestamp.story.creation_time (paling umum)
    ct = _safe_get(story, 'comet_sections', 'timestamp', 'story', 'creation_time')
    if ct:
        try:
            return int(ct)
        except (TypeError, ValueError):
            pass
    # Path B: scan metadata array di context_layout
    metas = _safe_get(
        story, 'comet_sections', 'context_layout', 'story',
        'comet_sections', 'metadata',
        default=[],
    ) or []
    for m in metas:
        ct = _safe_get(m, 'story', 'creation_time')
        if ct:
            try:
                return int(ct)
            except (TypeError, ValueError):
                continue
    return None


def _extract_one_post(story: dict) -> Optional[Dict]:
    """
    Convert story node FB → dict standar SocialPulse.
    Return None kalau bukan post valid (e.g. ad placeholder, system message).
    """
    post_id = story.get('post_id')
    if not post_id:
        return None
    
    # Author
    actors = story.get('actors') or []
    actor = actors[0] if actors else {}
    author_name = actor.get('name')
    author_id = actor.get('id')

    # Profile pic — di FB Comet response sekarang (verified dari raw dump
    # /search/posts/ + /search/top/ endpoint), top-level `actors[0]` udah
    # di-strip dari profile_picture. URL foto disimpan di sub-tree comet_sections:
    #
    #   PRIMARY (paling konsisten di setiap edge):
    #     story.comet_sections.content.story.actors[0].profile_picture.uri
    #
    #   ALT (rendered via "actor_photo" comet section):
    #     story.comet_sections.context_layout.story.comet_sections.actor_photo
    #          .story.actors[0].profile_picture.uri
    #
    #   FALLBACK lama (untuk endpoint/render strategy varian):
    #     actor.profile_picture.uri  / actor.profile_picture_depth_0_fb_image.uri
    #
    # Catatan: actor di nested-tree itu BEDA object dari top-level actor —
    # field-nya lebih lengkap (punya profile_picture). Kita ambil dari sana.
    # _safe_get cuma handle dict (bukan list), jadi list-index manual.
    _content_actors = _safe_get(
        story, 'comet_sections', 'content', 'story', 'actors',
        default=[]
    ) or []
    content_story_actor = _content_actors[0] if (
        isinstance(_content_actors, list) and _content_actors
    ) else {}

    _photo_actors = _safe_get(
        story, 'comet_sections', 'context_layout', 'story',
        'comet_sections', 'actor_photo', 'story', 'actors',
        default=[]
    ) or []
    actor_photo_actor = _photo_actors[0] if (
        isinstance(_photo_actors, list) and _photo_actors
    ) else {}

    profile_pic = (
        _safe_get(content_story_actor, 'profile_picture', 'uri')
        or _safe_get(actor_photo_actor, 'profile_picture', 'uri')
        or _safe_get(actor, 'profile_picture', 'uri')                      # legacy fallback
        or _safe_get(actor, 'profile_picture_depth_0_fb_image', 'uri')     # legacy fallback
        or _safe_get(actor, 'profilePicture', 'uri')                       # legacy fallback
    )

    # Debug hook: dump actor shape kalau profile pic tetap gak ada.
    # Pakai content_story_actor kalau ada (lebih informatif), fallback ke top-level.
    if not profile_pic:
        sample_actor = content_story_actor or actor_photo_actor or actor
        _dump_actor_sample(sample_actor, str(post_id))
    
    # Engagement
    reactions, comments, shares = _extract_engagement(story)
    
    # Message
    text = _extract_message(story)
    
    # Timestamp
    ct = _extract_creation_time(story)
    
    # URL — prefer permalink_url (canonical), fallback wwwURL
    url = (
        story.get('permalink_url')
        or story.get('wwwURL')
        or _safe_get(story, 'comet_sections', 'content', 'story', 'wwwURL')
    )
    
    return {
        'platform': 'facebook',
        'id': str(post_id),
        'shortCode': str(post_id),
        'ownerUsername': author_name,
        'username': author_name,
        'profilePicUrl': str(profile_pic) if profile_pic else None,
        'profile_pic_url': str(profile_pic) if profile_pic else None,
        'caption': text,
        'text': text,
        'likesCount': reactions,
        'like_count': reactions,
        'commentsCount': comments,
        'comment_count': comments,
        'videoViewCount': 0,
        'video_view_count': 0,
        'shareCount': shares,
        'timestamp': ct,
        'taken_at': ct,
        'url': url,
        # Internal — distrip di akhir, gunanya untuk dedup & debug
        '_author_id': author_id,
    }


def _extract_posts_from_response_body(body: str) -> List[Dict]:
    """
    Parse satu response body, extract semua post dari serpResponse.results.edges.
    Return [] kalau response bukan feed search atau parse fail.
    """
    objs = _parse_multi_json(body)
    if not objs:
        return []
    
    posts: List[Dict] = []
    for obj in objs:
        # Object pertama biasanya yang carry edges. Streaming patches juga
        # bisa berisi serpResponse (rare) — handle kedua case.
        edges = _safe_get(obj, 'data', 'serpResponse', 'results', 'edges', default=[]) or []
        for edge in edges:
            story = _safe_get(
                edge, 'rendering_strategy', 'view_model', 'click_model', 'story',
            )
            if not story:
                continue
            post = _extract_one_post(story)
            if post:
                posts.append(post)
    return posts


# ============================================================================
# FacebookScraper main class
# ============================================================================

class FacebookScraper(BaseScraper):
    
    PLATFORM = "facebook"
    BASE = "https://www.facebook.com"
    
    NAV_TIMEOUT = 45_000  # ms
    SELECTOR_TIMEOUT = 20_000  # ms
    
    # Scroll behavior — berapa kali scroll untuk trigger pagination GraphQL
    # v2.3: bumped 8 → 20. Observasi: FB stop emit post baru biasanya di
    # scroll ke-12-15 untuk keyword brand kecil; 20 kasih buffer tanpa
    # bikin per-scrape kelamaan (early-stop tetap kick in di scroll stuck).
    MAX_SCROLLS = 20
    SCROLL_WAIT_MS = 2_500  # tunggu after scroll supaya FB fire request
    # v2.3: stuck threshold dinaikin 2 → 3. Kadang scroll ke-N gak nambah
    # tapi scroll ke-N+1 nambah karena FB throttle. 3 kasih satu kesempatan
    # extra sebelum bail out.
    SCROLL_STUCK_THRESHOLD = 3
    
    # v2.3: Multi-endpoint sweep. /search/posts/ adalah primary (chronological,
    # less personalized). /search/top/ kadang punya post yang gak muncul di
    # /posts/ karena algoritma ranking beda. Dedup by post_id setelah merge.
    SEARCH_ENDPOINTS = ('/search/posts/', '/search/top/')
    
    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        """
        v2.3: Multi-pass sweep untuk maksimalkan unique posts.
        
        Pass matrix:
          - Keyword variants: original + minor variations (lihat _generate_variants)
          - Endpoints: /search/posts/ + /search/top/
        
        Total passes = len(variants) × len(endpoints). Untuk keyword "juragan99"
        biasanya 3-4 variants × 2 endpoints = 6-8 passes. Per pass ~30-40s,
        jadi total scrape bisa 3-5 menit. Trade-off: dapat 2-3x lebih banyak
        unique posts dibanding single-pass.
        
        Dedup by post_id setelah semua pass selesai. Akun FB yang sama dipakai
        across passes (jangan rotate akun karena rate limit FB lebih agresif
        kalau banyak akun hit endpoint sama dalam waktu singkat).
        """
        username = self.account_manager.pick_next_active()
        if not username:
            raise Exception("No active Facebook accounts available")
        
        self.last_used_account = username
        cookies = self.account_manager.get_cookies(username)
        
        variants = self._generate_keyword_variants(keyword)
        # Per-pass amount: kasih sedikit buffer di atas target supaya kalau
        # satu pass gagal, pass lain bisa cover. Cap di 80 supaya per-pass
        # gak kelamaan (FB jarang kasih >80 per single search session).
        per_pass_amount = min(80, max(amount, amount // max(1, len(variants))) + 10)
        
        # Aggregate dedup across all passes
        merged: Dict[str, Dict] = {}
        pass_stats = []
        
        try:
            for variant in variants:
                for endpoint in self.SEARCH_ENDPOINTS:
                    # Stop early kalau sudah cukup dari target. Threshold 1.3x
                    # ngasih buffer untuk filter/dedup downstream tanpa over-fetch
                    # (sebelumnya 2x — bikin scrape sering lebih dari 5 menit
                    # untuk request max_results: 100, dan mostly returns
                    # diminishing returns post-150 unique).
                    early_stop_threshold = max(int(amount * 1.3), amount + 20)
                    if len(merged) >= early_stop_threshold:
                        logger.info(
                            f"[facebook] Got {len(merged)} unique posts "
                            f"(>= early-stop threshold {early_stop_threshold} for target {amount}); "
                            f"skip remaining passes"
                        )
                        break
                    
                    try:
                        posts = self._search_with_browser(
                            variant, cookies, per_pass_amount, endpoint=endpoint,
                        )
                    except Exception as e:
                        # Per-pass failure jangan kill seluruh scrape
                        logger.warning(
                            f"[facebook] Pass failed (variant={variant!r}, endpoint={endpoint}): {e}"
                        )
                        # Tapi kalau cookie expired/checkpoint, propagate ke account manager
                        if self._infer_status(e) in ('EXPIRED', 'CHALLENGED'):
                            raise
                        pass_stats.append({
                            'variant': variant, 'endpoint': endpoint,
                            'count': 0, 'error': str(e)[:100],
                            'post_ids': [],
                        })
                        continue
                    
                    new_count = 0
                    pass_post_ids = []
                    for p in posts:
                        # NOTE: _extract_one_post return field 'id' (string),
                        # bukan 'post_id'. Sempat keliru di v2.3.0 — semua
                        # post ke-skip karena pid=None. Fixed v2.3.1.
                        pid = p.get('id')
                        if not pid:
                            continue
                        pass_post_ids.append(pid)
                        if pid not in merged:
                            merged[pid] = p
                            new_count += 1
                    
                    pass_stats.append({
                        'variant': variant, 'endpoint': endpoint,
                        'returned': len(posts), 'new': new_count,
                        'post_ids': pass_post_ids,
                    })
                    logger.info(
                        f"[facebook] Pass done: variant={variant!r} endpoint={endpoint} "
                        f"returned={len(posts)} new={new_count} total_unique={len(merged)}"
                    )
                else:
                    # inner loop completed without break → continue outer
                    continue
                break  # outer break (early stop)
            
            self.account_manager.mark_used(username)
            
            logger.info(
                f"[facebook] Multi-pass scrape done: keyword={keyword!r} "
                f"passes={len(pass_stats)} unique_posts={len(merged)} "
                f"(target was {amount})"
            )
            for s in pass_stats:
                logger.debug(f"[facebook]   pass: {s}")
            
            # Debug dump (kalau FB_SCRAPER_DEBUG=1)
            if self._debug_enabled():
                self._debug_write_run(
                    keyword=keyword,
                    username=username,
                    variants=variants,
                    per_pass_results=pass_stats,
                    merged=merged,
                    target_amount=amount,
                )
            
            # Sort by timestamp desc kalau ada, supaya hasil paling baru di atas
            # _extract_one_post output: 'taken_at' (preferred) atau 'timestamp'
            sorted_posts = sorted(
                merged.values(),
                key=lambda p: p.get('taken_at') or p.get('timestamp') or 0,
                reverse=True,
            )
            return sorted_posts[:amount]
        
        except Exception as e:
            self.account_manager.mark_error(username, e, status=self._infer_status(e))
            raise
    
    @staticmethod
    def _generate_keyword_variants(keyword: str) -> List[str]:
        """
        Generate variasi keyword sederhana untuk maksimalkan coverage.
        FB search treat "juragan99", "juragan 99", "@juragan99" sebagai query
        yang berbeda — return ranking yang bisa cukup beda.
        
        Strategy:
          - Always include original (paling penting)
          - Kalau ada angka nempel huruf: tambah versi dengan space ("juragan99" → "juragan 99")
          - Kalau ada space: tambah versi tanpa space ("harian disway" → "hariandisway")
          - Kalau bukan hashtag/mention: tambah hashtag prefix kalau singleword/no-space-after-strip
          - Dedupe & preserve order
        
        Tidak generate variant yang terlalu jauh dari original supaya tetap
        relevan ke brand yang dimonitor. Cap di 4 variants supaya total scrape
        time tetap reasonable (4 variants × 2 endpoints = 8 passes ≈ 4-5 menit).
        """
        import re
        kw = keyword.strip()
        if not kw:
            return [keyword]
        
        variants = [kw]
        
        # Variant 1: insert space antara huruf-angka boundary
        # "juragan99" → "juragan 99", "produk2024" → "produk 2024"
        spaced = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', kw)
        spaced = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', spaced)
        if spaced != kw:
            variants.append(spaced)
        
        # Variant 2: collapse spaces
        # "harian disway" → "hariandisway"
        collapsed = re.sub(r'\s+', '', kw)
        if collapsed != kw and collapsed not in variants:
            variants.append(collapsed)
        
        # Variant 3: hashtag prefix (kalau belum ada # atau @)
        # Useful untuk brand yang dipromosikan via hashtag
        if not kw.startswith(('#', '@')) and ' ' not in kw:
            hashtag = '#' + kw.lstrip('#')
            if hashtag not in variants:
                variants.append(hashtag)
        
        # Dedup preserving order, cap di 4
        seen = set()
        out = []
        for v in variants:
            if v not in seen:
                seen.add(v)
                out.append(v)
            if len(out) >= 4:
                break
        return out
    
    def _search_with_browser(
        self, keyword: str, cookies: dict, amount: int,
        endpoint: str = '/search/posts/',
    ) -> List[Dict]:
        """Submit job ke worker thread (Playwright sync not thread-safe)."""
        def _do_scrape(browser):
            return self._scrape_inside_worker(browser, keyword, cookies, amount, endpoint)
        # Timeout 180s — initial wait 6s + scroll loop bisa sampai ~70s (20 scrolls
        # × 2.5s) + buffer. Naik dari 150s di v2.2 karena MAX_SCROLLS naik.
        return _worker.submit(_do_scrape, timeout=180.0)
    
    def _scrape_inside_worker(
        self, browser, keyword: str, cookies: dict, amount: int,
        endpoint: str = '/search/posts/',
    ) -> List[Dict]:
        """
        Eksekusi inside worker thread. Receives browser instance dari worker.
        
        Strategi:
          1. Fresh context + inject cookies
          2. Pasang response listener BEFORE navigation
          3. Navigate ke /search/posts/  (v2.2: dulu /search/top/)
          4. Wait + scroll untuk trigger more GraphQL requests
          5. Dedup posts yang sudah ke-collect dari listener
        """
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={'width': 1366, 'height': 900},
            locale='id-ID',
            timezone_id='Asia/Jakarta',
            bypass_csp=True,
        )

        # Stealth init script (8 Mei 2026): patch navigator.webdriver, plugins,
        # WebGL, window.chrome stub. FB historically lebih lunak ke automation
        # tapi tetap pakai stealth supaya konsisten dengan IG/Twitter dan tahan
        # banting kalau Meta perketat suatu saat.
        try:
            context.add_init_script(STEALTH_INIT_JS)
        except Exception as e:
            logger.warning(f"[facebook] Stealth inject failed: {e}")
        
        # Collected posts: dict[post_id, post_dict] — dedupe by ID
        collected: Dict[str, Dict] = {}
        # Stats untuk diagnostic
        stats = {'responses_seen': 0, 'responses_with_edges': 0, 'parse_errors': 0}
        
        try:
            self._inject_cookies(context, cookies)
            context.route('**/*', self._handle_route)
            
            page = context.new_page()
            page.set_default_navigation_timeout(self.NAV_TIMEOUT)
            page.set_default_timeout(self.SELECTOR_TIMEOUT)
            
            # === Pasang response listener SEBELUM goto() ===
            # Penting: kalau dipasang setelah goto, request GraphQL pertama
            # (yang biasanya paling kaya — feed initial) akan ke-miss.
            self._attach_response_listener(page, collected, stats)
            
            # === Navigate ===
            # v2.3: endpoint configurable. Default /search/posts/ (chronological,
            # less personalized) tapi caller juga lewatin /search/top/ untuk
            # nambah coverage. Lihat scrape_keyword multi-pass orchestration.
            search_url = f"{self.BASE}{endpoint}?q={quote(keyword)}"
            logger.info(f"[facebook] Navigating to {search_url}")
            try:
                page.goto(search_url, wait_until='domcontentloaded')
            except PWTimeout:
                logger.warning("[facebook] Navigation timeout; lanjut anyway")
            
            # Detect redirect ke login/checkpoint
            current_url = page.url.lower()
            if any(p in current_url for p in ['/login', '/checkpoint', '/recover', '/two_step']):
                raise Exception(f"Cookie expired or challenged (redirected to {current_url})")
            
            # === Tunggu initial GraphQL responses settle ===
            # Tidak tergantung DOM marker — yang penting GraphQL response sudah
            # sampai. Polling: cek apakah feed sudah ada (with_edges > 0),
            # bukan sekedar `collected` tidak kosong (lihat _wait_for_initial_data).
            self._wait_for_initial_data(page, collected, stats, timeout_ms=20_000)
            
            # === Scroll untuk trigger pagination ===
            self._scroll_until_target(page, collected, target=amount)
            
            # Final settle
            page.wait_for_timeout(2_000)
            
            posts = list(collected.values())
            logger.info(
                f"[facebook] GraphQL extraction: "
                f"responses_seen={stats['responses_seen']} "
                f"with_edges={stats['responses_with_edges']} "
                f"parse_errors={stats['parse_errors']} "
                f"unique_posts={len(posts)}"
            )
            
            if not posts:
                logger.warning("[facebook] 0 posts extracted from GraphQL")
                self._save_debug_artifacts(page)
                # Cek apakah memang 0 hasil (no results) atau ada masalah
                if self._is_empty_search(page):
                    logger.info(f"[facebook] Search returned no results for '{keyword}'")
                    return []
            
            # Strip internal fields, jsonify
            for p in posts:
                p.pop('_author_id', None)
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
        Pasang listener untuk response /api/graphql/. Untuk tiap response,
        parse body, extract post, append ke `collected` (dict by post_id).
        
        `collected` di-mutate dari thread Playwright (sync), tapi listener
        sendiri eksekusi di context yang sama (worker thread) — aman.
        """
        def on_response(response):
            url = response.url
            if GRAPHQL_URL_FRAGMENT not in url:
                return
            
            stats['responses_seen'] += 1
            
            try:
                body = response.text()
            except Exception as e:
                # Beberapa response gak bisa di-read (e.g. aborted)
                logger.debug(f"[facebook] Can't read response body: {e}")
                return

            _dump_raw_response(url, body)

            try:
                posts = _extract_posts_from_response_body(body)
            except Exception as e:
                stats['parse_errors'] += 1
                logger.debug(f"[facebook] Parse error: {e}")
                return
            
            if not posts:
                return
            
            stats['responses_with_edges'] += 1
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
        Tunggu response feed search datang. PENTING: jangan exit di entry
        pertama — di production observasi, response yg datang duluan sering
        cuma metadata page (e.g. "Juragan99 Towing" page info), bukan feed
        post. Kalau exit terlalu cepat dan langsung scroll, scroll bisa
        cancel pending request feed real.
        
        Strategi:
          1. Tunggu fixed 6s — kasih waktu FB fire semua initial requests
             (mirror behavior capture tool yang berhasil dapat 32 responses)
          2. Setelah 6s, cek apakah `responses_with_edges > 0` (artinya feed
             sudah datang). Kalau belum, polling sampai max timeout.
          3. Kasih extra 2s setelah feed pertama datang untuk streaming chunks.
        """
        # Step 1: fixed initial wait — non-negotiable, FB butuh waktu
        page.wait_for_timeout(6_000)
        
        if stats.get('responses_with_edges', 0) > 0:
            logger.debug(
                f"[facebook] Initial feed arrived in 6s "
                f"(responses_seen={stats['responses_seen']}, posts={len(collected)})"
            )
            page.wait_for_timeout(2_000)
            return
        
        # Step 2: polling kalau belum dapat feed
        elapsed = 6_000
        interval = 500
        while elapsed < timeout_ms:
            if stats.get('responses_with_edges', 0) > 0:
                logger.debug(
                    f"[facebook] Feed arrived after {elapsed}ms "
                    f"(responses_seen={stats['responses_seen']}, posts={len(collected)})"
                )
                page.wait_for_timeout(2_000)
                return
            page.wait_for_timeout(interval)
            elapsed += interval
        
        logger.warning(
            f"[facebook] No feed responses after {timeout_ms}ms "
            f"(responses_seen={stats.get('responses_seen', 0)}) — lanjut scroll anyway"
        )
    
    def _scroll_until_target(
        self, page, collected: Dict[str, Dict], target: int,
    ):
        """
        Scroll incremental untuk trigger lazy-load. Stop kalau:
          - collected >= target
          - 2 scroll berturut-turut gak nambah post baru
          - scroll counter capai MAX_SCROLLS
        
        Behavior dicocokkan dengan fb_capture.py yang terbukti dapat 32 responses:
          - scrollBy 2x viewport (bukan 1.5x — observasi: 1.5x kadang gak
            cukup trigger pagination request di FB 2026)
          - fixed wait 2.5s (jangan pakai networkidle — di headless sering
            triggered prematur sebelum response feed sampai)
        """
        prev_count = -1
        no_new = 0
        for i in range(self.MAX_SCROLLS):
            if len(collected) >= target:
                logger.debug(f"[facebook] Reached target {target}, stop scroll")
                return
            
            try:
                page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            except Exception:
                pass
            
            # Fixed wait — jangan pakai networkidle, di headless terlalu cepat
            page.wait_for_timeout(self.SCROLL_WAIT_MS)
            
            current = len(collected)
            if current == prev_count:
                no_new += 1
                if no_new >= self.SCROLL_STUCK_THRESHOLD:
                    logger.debug(
                        f"[facebook] Scroll stuck at {current} after {i+1} iterations "
                        f"(threshold={self.SCROLL_STUCK_THRESHOLD})"
                    )
                    return
            else:
                no_new = 0
                prev_count = current
    
    # ------------------------------------------------------------------------
    # Cookie injection, route handling, helpers
    # ------------------------------------------------------------------------
    
    def _inject_cookies(self, context, state: dict):
        """
        Apply Playwright storage_state ke context.

        v2.4 (6 May 2026): switch dari flat {name: value} dict ke storage_state
        shape `{"cookies": [...], "origins": [...]}` — sama dengan output
        `context.storage_state()` Playwright dan format `fb_session.json` dari
        fb_capture tool.

        Yang berubah dari versi sebelumnya:
          - Cookies di-pass through dengan attribute lengkap (expires,
            httpOnly, sameSite, domain, path) bukan hardcode default.
          - LocalStorage dari `state.origins` di-inject via add_init_script
            sehingga FB melihat session yang lebih "lengkap" (sebelumnya
            cuma cookies, localStorage selalu kosong di context fresh kita).
            FB taruh beberapa flag di localStorage (mis. CacheStorageVersion,
            screen_time_period_logging) yang bisa di-read sama JS FB untuk
            fingerprint/personalisasi. Tidak guaranteed mengubah hasil
            ranking, tapi bikin session lebih dekat ke real user.

        Kompatibilitas: account_manager.get_cookies() sekarang return
        storage_state shape, baik untuk session file baru (canonical) maupun
        legacy (auto-converted). Jadi tidak ada code path yang masih kirim
        flat dict ke sini.
        """
        if not isinstance(state, dict):
            raise Exception(
                f"Cookie state harus dict storage_state, "
                f"got {type(state).__name__}"
            )

        cookies = state.get('cookies') or []
        if not cookies:
            raise Exception("Cookie state kosong (cookies list empty).")

        # --- Cookies ---
        # Playwright `context.add_cookies()` menerima list dengan field yang
        # match persis dengan storage_state["cookies"]. Pass through tapi
        # sanitize defensif (drop entry rusak, normalize tipe).
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
                'domain': c.get('domain') or '.facebook.com',
                'path': c.get('path') or '/',
                'httpOnly': bool(c.get('httpOnly', False)),
                'secure': bool(c.get('secure', True)),
                'sameSite': c.get('sameSite') or 'None',
            }
            # `expires` optional. -1 atau missing = session cookie.
            if 'expires' in c and c['expires'] is not None:
                try:
                    cookie['expires'] = float(c['expires'])
                except (TypeError, ValueError):
                    pass
            pw_cookies.append(cookie)

        if pw_cookies:
            context.add_cookies(pw_cookies)

        # --- localStorage per-origin ---
        # Inject via add_init_script supaya berjalan di setiap dokumen baru
        # di context (bukan cuma first page). Script per-origin: cek
        # location.origin sebelum tulis, supaya entries facebook.com tidak
        # bocor ke iframe origin lain.
        origins = state.get('origins') or []
        for origin_entry in origins:
            if not isinstance(origin_entry, dict):
                continue
            origin = origin_entry.get('origin')
            items = origin_entry.get('localStorage') or []
            if not origin or not items:
                continue

            # Filter entries yang valid (must have name + value as strings).
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
                # IIFE: cek origin match dulu, lalu setItem satu per satu.
                # try/catch per item supaya satu key bermasalah tidak gagalkan
                # seluruh batch (mis. quota exceeded, JSON-parse error).
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
                    f"[facebook] localStorage inject skipped for {origin}: {e}"
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
    
    def _is_empty_search(self, page) -> bool:
        """Cek apakah halaman menunjukkan 'no results' marker."""
        try:
            content = page.content()[:50000].lower()
            markers = [
                'no results found',
                'tidak ada hasil',
                "couldn't find anything",
                'no posts to show',
            ]
            return any(m in content for m in markers)
        except Exception:
            return False
    
    def _save_debug_artifacts(self, page):
        """Save HTML + screenshot untuk inspect kalau extraction gagal."""
        try:
            import os
            data_dir = os.environ.get('DATA_DIR', '/data')
            html_path = f"{data_dir}/fb_debug_last.html"
            png_path = f"{data_dir}/fb_debug_screenshot.png"
            with open(html_path, 'w', encoding='utf-8') as fh:
                fh.write(f"<!-- url: {page.url} -->\n")
                fh.write(page.content())
            page.screenshot(path=png_path, full_page=False)
            logger.info(f"[facebook] Debug artifacts saved: {html_path}, {png_path}")
        except Exception as e:
            logger.debug(f"[facebook] Couldn't save debug artifacts: {e}")
    
    def _infer_status(self, error: Exception):
        s = str(error).lower()
        if 'login' in s or 'expired' in s or 'cookie' in s:
            return self.account_manager.STATUS_EXPIRED
        if 'checkpoint' in s or 'verify' in s or 'challenge' in s:
            return self.account_manager.STATUS_CHALLENGE
        if '429' in s or 'rate' in s or 'too many' in s:
            return self.account_manager.STATUS_RATE_LIMITED
        return None
    
    # ------------------------------------------------------------------------
    # Debug dump (toggle via FB_SCRAPER_DEBUG=1)
    # ------------------------------------------------------------------------
    
    @staticmethod
    def _debug_enabled() -> bool:
        """Check FB_SCRAPER_DEBUG env var. Truthy values: '1', 'true', 'yes'."""
        return os.environ.get('FB_SCRAPER_DEBUG', '').strip().lower() in ('1', 'true', 'yes')
    
    @staticmethod
    def _debug_dump_dir() -> str:
        """Where to write debug dumps. Default /data, override via FB_SCRAPER_DEBUG_DIR."""
        d = os.environ.get('FB_SCRAPER_DEBUG_DIR', '/data')
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = '.'  # fallback to cwd kalau /data gak writable
        return d
    
    def _debug_write_run(
        self,
        keyword: str,
        username: str,
        variants: List[str],
        per_pass_results: List[Dict],
        merged: Dict[str, Dict],
        target_amount: int,
    ):
        """
        Dump full diagnostic of a multi-pass scrape run.
        
        File: <dir>/fb_scrape_debug_<keyword>_<timestamp>.json
        
        Berisi:
          - meta: keyword asli, username, target, variants generated
          - per_pass: array of {variant, endpoint, returned, new, post_ids,
            authors} — supaya tahu pass mana yang dapat post apa
          - merged: list dari semua unique post {post_id, author_name,
            author_username, creation_time, permalink_url, message_preview}
          - by_post: dict post_id → list of (variant, endpoint) yang nge-emit
            post itu — supaya tahu post X cuma muncul dari mana
        """
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_kw = ''.join(c if c.isalnum() else '_' for c in keyword)[:40]
            path = os.path.join(
                self._debug_dump_dir(),
                f'fb_scrape_debug_{safe_kw}_{ts}.json',
            )
            
            # Build by_post mapping: post_id → list of passes that emitted it
            by_post: Dict[str, List[str]] = {}
            for pr in per_pass_results:
                pass_label = f"{pr.get('variant')!r} @ {pr.get('endpoint')}"
                for pid in pr.get('post_ids', []):
                    by_post.setdefault(pid, []).append(pass_label)
            
            # Compact merged view (skip raw fields, keep what's useful for debug)
            # NOTE: field names cocok dengan output _extract_one_post:
            #   - 'id' (post_id), 'username' (author), 'taken_at' (unix ts),
            #   - 'url' (permalink), 'like_count', 'comment_count', 'shareCount',
            #   - 'text' (message)
            merged_compact = []
            for pid, p in merged.items():
                merged_compact.append({
                    'post_id': pid,
                    'author_name': p.get('username') or p.get('ownerUsername'),
                    'creation_time': p.get('taken_at') or p.get('timestamp'),
                    'creation_iso': self._unix_to_iso(p.get('taken_at') or p.get('timestamp')),
                    'permalink_url': p.get('url'),
                    'reactions': p.get('like_count') or p.get('likesCount'),
                    'comments': p.get('comment_count') or p.get('commentsCount'),
                    'shares': p.get('shareCount'),
                    'message_preview': (p.get('text') or p.get('caption') or '')[:200],
                    'emitted_by_passes': by_post.get(pid, []),
                })
            
            # Sort by creation_time desc untuk readability
            merged_compact.sort(key=lambda x: x.get('creation_time') or 0, reverse=True)
            
            payload = {
                'meta': {
                    'keyword': keyword,
                    'username': username,
                    'target_amount': target_amount,
                    'timestamp': ts,
                    'variants_generated': variants,
                    'endpoints_used': list(self.SEARCH_ENDPOINTS),
                    'total_passes': len(per_pass_results),
                    'total_unique_posts': len(merged),
                },
                'per_pass': per_pass_results,
                'posts': merged_compact,
            }
            
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            
            logger.info(
                f"[facebook][debug] Run dumped to {path} "
                f"({len(merged)} unique posts across {len(per_pass_results)} passes)"
            )
        except Exception as e:
            logger.warning(f"[facebook][debug] Failed to write debug dump: {e}")
    
    @staticmethod
    def _unix_to_iso(ts) -> Optional[str]:
        """Best-effort Unix timestamp → ISO string. Returns None on failure."""
        try:
            if not ts:
                return None
            return datetime.fromtimestamp(int(ts)).isoformat()
        except Exception:
            return None


# Cleanup hook — kalau process di-shutdown, close worker bersih
import atexit
atexit.register(_worker.shutdown)