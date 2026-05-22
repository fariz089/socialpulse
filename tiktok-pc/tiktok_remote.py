"""
TikTokApi wrapper untuk PC service
====================================
Fungsi utama:
  - TikTokAccountStore: simpan + rotate cookie TikTok (mirror format
    android-scraper, sehingga cookie yang tadinya di-paste lewat HP
    masih bisa dipaste sama persis ke service ini).
  - TikTokRemoteScraper: scrape_keyword() yang launch Playwright/Chromium
    via TikTokApi, generate signature otomatis, hit endpoint hashtag/feed.

Strategi scraping:
  1. Coba treat keyword sebagai hashtag → TikTokApi hashtag().videos()
     (paling reliable di TikTokApi 7.x).
  2. Kalau hashtag tidak ada / kosong → fallback ke trending feed,
     filter by keyword di caption (best-effort).
  3. Search API TikTokApi sering broken; tidak kita andalkan.

Cookie format yang diterima (auto-detect):
  - Playwright storage_state JSON ({"cookies":[...], "origins":[...]})
    ← format dari multi_capture & fb_capture; PALING LENGKAP
  - JSON flat dict ({"msToken":"...", "sessionid":"..."})
  - Header Cookie string ("msToken=...; sessionid=...; ...")
  - DevTools tabular paste (Application → Cookies)
  - ms_token saja (legacy single string)

Strategi parser: simpan SEMUA cookie yang ditemukan kecuali blacklist
(Akamai bot mgmt + analytics noise). Lebih banyak cookie = lebih sedikit
captcha dari TikTok anti-bot. Yang BENAR-BENAR strict cuma satu:
ms_token wajib ada (dipakai TikTokApi untuk generate request signature).
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────
# Cookie parser (port dari android-scraper)
# ──────────────────────────────────────────────────
# COOKIE_KEYS sekarang ALLOWLIST yang DIPRIORITASKAN, bukan filter strict.
# Cookies di luar list tetap disimpan (mereka bantu anti-bot fingerprint
# TikTok — misal passport_csrf_token, tea_web_id, cmpl_token).
#
# Yang BENAR-BENAR strict cuma satu: ms_token wajib ada (dipakai TikTokApi
# untuk generate request signature).
COOKIE_KEYS = [
    "ms_token",
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "tt_chain_token",
    "ttwid",
    "tt_csrf_token",
    "uid_tt",
    "uid_tt_ss",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "odin_tt",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "cmpl_token",
    "tt-passport-csrf-token",
    "tea_web_id",
    "last_login_method",
    "store-country-code",
    "store-idc",
    "tt-target-idc",
    "multi_sids",
    "msToken",  # raw form, akan di-canonicalize ke ms_token via aliases
]

COOKIE_ALIASES = {
    "mstoken": "ms_token",
    "ms_token": "ms_token",
}

# Cookies yang TIDAK BERGUNA (telemetry / Akamai bot mgmt / browser internal),
# di-blacklist supaya tidak nyampah di file session. Kita simpan SEMUA cookies
# tiktok yang relevan, tapi skip yang jelas-jelas noise.
COOKIE_BLACKLIST = frozenset({
    "_abck", "bm_sz", "bm_mi", "bm_so", "bm_lso", "ak_bmsc",  # Akamai bot mgmt
    "_ga", "_gid", "_gcl_au", "_fbp",  # Google/FB analytics
})


def _parse_cookies(raw: str) -> Dict[str, str]:
    """
    Parse cookie input → dict {cookie_name: value}.

    Mendukung 5 format input (auto-detect):
      A. Header Cookie string  "key=val; key=val; ..."
      B. JSON flat dict        {"msToken":"...", "sessionid":"..."}
      C. Tab-separated table   (DevTools Application > Cookies paste)
      D. ms_token saja         (legacy single-line)
      E. Playwright storage_state {"cookies":[...], "origins":[...]}
         ← format dari multi_capture, fb_capture, dan tools sejenis

    Strategi: simpan SEMUA cookie yang ditemukan kecuali blacklist
    (Akamai/analytics noise). TikTok pakai 20-30+ cookies untuk
    fingerprint, jadi makin banyak yang ke-preserve makin sedikit
    captcha challenge.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("cookie kosong")

    # Format B/E: JSON. Note: cek startswith {/[, BUKAN endswith — JSON
    # hasil json.dumps(indent=2) bisa multi-line dengan trailing newline.
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON tidak valid: {e}")

        if not isinstance(data, dict):
            raise ValueError(f"JSON harus dict, dapat {type(data).__name__}")

        # Format E: Playwright storage_state — extract cookies array
        if isinstance(data.get("cookies"), list):
            return _extract_from_storage_state(data)

        # Format B: flat dict
        return _normalize_cookie_dict(data)

    out: Dict[str, str] = {}

    # Format C: tab-separated table (DevTools paste).
    # CHECK SEBELUM cookie-string: value bisa mengandung '=' yang bikin
    # parser cookie-string salah split.
    if "\t" in raw and "\n" in raw:
        parsed = _parse_tab_separated(raw)
        if parsed:
            return _normalize_cookie_dict(parsed)

    # Format A: header string "key=val; key=val; ..."
    # Newline tidak masalah — kita normalize ke ; dulu.
    if "=" in raw and (";" in raw or "\n" in raw):
        normalized = raw.replace("\n", ";").replace("\r", ";")
        for part in normalized.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not k or not v:
                continue
            if k in COOKIE_BLACKLIST:
                continue
            # First-wins: kalau key duplikat (mis. msToken muncul 2x dari
            # 2 domain di Cookie header), pakai yang pertama.
            if k not in out:
                out[k] = v
        if out:
            return _normalize_cookie_dict(out)

    # Format D: ms_token saja (legacy single string)
    if re.match(r"^[A-Za-z0-9+/=_\-]{20,}$", raw):
        return {"ms_token": raw}

    raise ValueError(
        "Tidak bisa parse cookie. Format diterima: JSON dict, "
        "Playwright storage_state, header Cookie string, "
        "tab-separated table, atau ms_token tunggal."
    )


def _normalize_cookie_dict(data: dict) -> Dict[str, str]:
    """
    Normalize keys via COOKIE_ALIASES (msToken → ms_token), drop blacklisted.
    SIMPAN SEMUA yang lewat — bukan filter ke whitelist COOKIE_KEYS.
    """
    out: Dict[str, str] = {}
    for k, v in data.items():
        if v in (None, ""):
            continue
        if k in COOKIE_BLACKLIST:
            continue
        canonical = COOKIE_ALIASES.get(k.lower(), k)
        # First-wins kalau duplikat setelah aliasing (mis. msToken + ms_token)
        if canonical not in out:
            out[canonical] = str(v)
    return out


def _extract_from_storage_state(state: dict) -> Dict[str, str]:
    """
    Convert Playwright storage_state {cookies: [{name, value, domain, ...}]}
    → flat dict. Untuk msToken yang muncul di multi domain, prioritaskan
    parent domain ".tiktok.com" (lebih general, dipakai TikTokApi).
    """
    out: Dict[str, str] = {}
    ms_token_candidates: list = []  # [(value, score), ...]

    for c in (state.get("cookies") or []):
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        value = c.get("value")
        if not name or value in (None, ""):
            continue
        if name in COOKIE_BLACKLIST:
            continue

        canonical = COOKIE_ALIASES.get(name.lower(), name)
        domain = c.get("domain", "") or ""

        if canonical == "ms_token":
            # Skor: parent domain (".tiktok.com") menang dari host-only
            score = 2 if domain.startswith(".tiktok.com") else 1
            ms_token_candidates.append((str(value), score))
            continue

        if canonical not in out:
            out[canonical] = str(value)

    if ms_token_candidates:
        ms_token_candidates.sort(key=lambda x: -x[1])
        out["ms_token"] = ms_token_candidates[0][0]

    return out


def _parse_tab_separated(raw: str) -> dict:
    """
    Parse format DevTools Application > Cookies (tab-separated):
      name<TAB>value<TAB>domain<TAB>path<TAB>expires<TAB>size<TAB>flags...
    """
    out: dict = {}
    ms_tokens: list = []  # [(value, domain), ...]

    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue

        name = parts[0].strip()
        value = parts[1].strip()
        domain = parts[2].strip() if len(parts) > 2 else ""

        if not name or not value:
            continue
        # Skip kalau value cuma flag (misal kolom kosong di paste)
        if value in ("✓", "TRUE", "FALSE", "Session", "None", "Lax", "Strict", "Secure"):
            continue

        # Special: msToken muncul 2x → pilih parent domain
        if name.lower() == "mstoken":
            ms_tokens.append((value, domain))
            continue

        if name not in out:
            out[name] = value

    if ms_tokens:
        preferred = next(
            (v for v, d in ms_tokens if d.startswith(".tiktok.com")),
            ms_tokens[0][0],
        )
        out["msToken"] = preferred

    return out


# ──────────────────────────────────────────────────
# Account Store (file-backed, thread-safe)
# ──────────────────────────────────────────────────
class TikTokAccountStore:
    STATUS_ACTIVE = "active"
    STATUS_BANNED = "banned"
    STATUS_CHALLENGE = "challenge"
    STATUS_EXPIRED = "expired"
    STATUS_RATE_LIMITED = "rate_limited"

    def __init__(self, accounts_file: Path, sessions_dir: Path):
        self.accounts_file = accounts_file
        self.sessions_dir = sessions_dir / "tiktok"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._accounts: Dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.accounts_file.exists():
            try:
                data = json.loads(self.accounts_file.read_text())
                self._accounts = data.get("tiktok", {}) if isinstance(data, dict) else {}
            except Exception as e:
                logger.error(f"Failed to load accounts: {e}")
                self._accounts = {}

    def _save(self):
        # Wrap dalam {tiktok: ...} biar shape-nya konsisten dengan android-scraper
        try:
            self.accounts_file.write_text(
                json.dumps({"tiktok": self._accounts}, indent=2)
            )
        except Exception as e:
            logger.error(f"Failed to save accounts: {e}")

    def _session_path(self, username: str) -> Path:
        return self.sessions_dir / f"{username}.json"

    def add_account(self, username: str, password: str) -> dict:
        with self._lock:
            cookies = _parse_cookies(password)
            if not cookies.get("ms_token"):
                raise ValueError("ms_token wajib ada — cek lagi cookie yang Anda paste")

            self._session_path(username).write_text(json.dumps(cookies, indent=2))

            self._accounts[username] = {
                "username": username,
                "password": password,  # disimpan supaya bisa re-parse kalau format COOKIE_KEYS bertambah
                "status": self.STATUS_ACTIVE,
                "added_at": self._accounts.get(username, {}).get(
                    "added_at", datetime.utcnow().isoformat()
                ),
                "last_used": None,
                "last_error": None,
                "use_count": 0,
                "platform": "tiktok",
                "cookies_count": len(cookies),
                "cookies_present": list(cookies.keys()),
            }
            self._save()
            # Cookies dari multi_capture/fb_capture biasanya 20-30 (storage_state full).
            # Manual paste header Cookie biasanya 5-15. Lebih banyak lebih baik.
            logger.info(
                f"[tiktok] {username}: stored {len(cookies)} cookies "
                f"(present: {', '.join(list(cookies)[:5])}"
                f"{'...' if len(cookies) > 5 else ''})"
            )
            return {
                "username": username,
                "platform": "tiktok",
                "status": self.STATUS_ACTIVE,
                "cookies_count": len(cookies),
            }

    def get_cookies(self, username: str) -> dict:
        path = self._session_path(username)
        if not path.exists():
            raise Exception(f"No TikTok session for {username}")
        return json.loads(path.read_text())

    def pick_next_active(self) -> Optional[str]:
        with self._lock:
            active = [
                (u, info)
                for u, info in self._accounts.items()
                if info.get("status") == self.STATUS_ACTIVE
            ]
            if not active:
                return None
            active.sort(key=lambda kv: kv[1].get("last_used") or "0")
            return active[0][0]

    def list_accounts(self) -> List[dict]:
        with self._lock:
            SENSITIVE = {"password"}
            return [
                {k: v for k, v in info.items() if k not in SENSITIVE}
                for info in self._accounts.values()
            ]

    def list_active(self) -> List[str]:
        with self._lock:
            return [
                u
                for u, info in self._accounts.items()
                if info.get("status") == self.STATUS_ACTIVE
            ]

    def mark_used(self, username: str):
        with self._lock:
            if username in self._accounts:
                self._accounts[username]["last_used"] = datetime.utcnow().isoformat()
                self._accounts[username]["use_count"] = (
                    self._accounts[username].get("use_count", 0) + 1
                )
                self._save()

    def mark_error(self, username: str, error: Exception, status: Optional[str] = None):
        with self._lock:
            if username not in self._accounts:
                return
            self._accounts[username]["last_error"] = str(error)[:200]
            self._accounts[username]["error_at"] = datetime.utcnow().isoformat()
            if status:
                self._accounts[username]["status"] = status
                logger.warning(
                    f"[tiktok] {username} marked as {status}: {str(error)[:100]}"
                )
            self._save()

    def set_status(self, username: str, status: str) -> bool:
        with self._lock:
            if username not in self._accounts:
                return False
            self._accounts[username]["status"] = status
            self._save()
            return True

    def delete_account(self, username: str) -> bool:
        with self._lock:
            if username not in self._accounts:
                return False
            del self._accounts[username]
            sp = self._session_path(username)
            if sp.exists():
                sp.unlink()
            self._save()
            return True


# ──────────────────────────────────────────────────
# Scraper inti
# ──────────────────────────────────────────────────
def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return str(value)


def _normalize_video(item: dict) -> dict:
    """Normalize TikTokApi video.as_dict ke shape posts[] yang dipakai frontend Slaytics."""
    author = item.get("author") or {}
    stats = item.get("stats") or item.get("statsV2") or {}
    video = item.get("video") or {}

    vid = item.get("id") or item.get("aweme_id")
    author_name = (
        author.get("uniqueId") or author.get("unique_id") or author.get("nickname")
    )

    create_time = item.get("createTime") or item.get("create_time") or 0
    try:
        create_time = int(create_time)
    except Exception:
        create_time = int(time.time())

    avatar = author.get("avatarThumb") or author.get("avatar_thumb") or ""
    if isinstance(avatar, dict):
        avatar = (avatar.get("url_list") or [None])[0]

    def safe_int(val):
        try:
            return int(val or 0)
        except Exception:
            return 0

    result = {
        "platform": "tiktok",
        "id": str(vid) if vid else None,
        "shortCode": str(vid) if vid else None,
        "ownerUsername": author_name,
        "username": author_name,
        "profilePicUrl": str(avatar) if avatar else None,
        "profile_pic_url": str(avatar) if avatar else None,
        "caption": item.get("desc") or "",
        "text": item.get("desc") or "",
        "likesCount": safe_int(stats.get("diggCount") or stats.get("digg_count")),
        "like_count": safe_int(stats.get("diggCount") or stats.get("digg_count")),
        "commentsCount": safe_int(
            stats.get("commentCount") or stats.get("comment_count")
        ),
        "comment_count": safe_int(
            stats.get("commentCount") or stats.get("comment_count")
        ),
        "videoViewCount": safe_int(stats.get("playCount") or stats.get("play_count")),
        "video_view_count": safe_int(
            stats.get("playCount") or stats.get("play_count")
        ),
        "shareCount": safe_int(stats.get("shareCount") or stats.get("share_count")),
        "timestamp": create_time,
        "taken_at": create_time,
        "url": (
            f"https://www.tiktok.com/@{author_name}/video/{vid}"
            if author_name and vid
            else ""
        ),
        "duration": safe_int(video.get("duration")),
    }
    return _to_jsonable(result)


class TikTokRemoteScraper:
    def __init__(self, account_store: TikTokAccountStore):
        self.account_store = account_store
        self.last_used_account: Optional[str] = None

    async def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        """
        Pakai TikTokApi (Playwright). Coba hashtag dulu (paling reliable),
        fallback ke trending kalau kosong.

        Auto-retry sekali kalau timeout (TikTok kadang flag session yang
        baru aja bikin scrape, jeda 8 detik biasanya cukup).
        """
        keyword = keyword.lstrip("#")
        username = self.account_store.pick_next_active()
        if not username:
            raise Exception("No active TikTok accounts available")

        self.last_used_account = username

        max_attempts = 2
        last_error: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                return await self._scrape_attempt(username, keyword, amount)
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_transient = (
                    "timeout" in err_str
                    or "navigation" in err_str
                    or "net::" in err_str
                )
                if attempt < max_attempts - 1 and is_transient:
                    backoff = 8
                    logger.warning(
                        f"[tiktok] attempt {attempt + 1}/{max_attempts} "
                        f"transient error ({type(e).__name__}: {str(e)[:120]}), "
                        f"retrying in {backoff}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue
                break

        # All attempts exhausted
        status = self._infer_status(last_error) if last_error else None
        self.account_store.mark_error(
            username, last_error or Exception("unknown"), status=status
        )
        raise last_error or Exception("scrape failed")

    async def _scrape_attempt(
        self, username: str, keyword: str, amount: int
    ) -> List[Dict]:
        from TikTokApi import TikTokApi  # lazy import: berat (loads Playwright)

        cookies = self.account_store.get_cookies(username)
        ms_token = cookies.get("ms_token")
        if not ms_token:
            raise Exception(f"Account {username} has no ms_token cookie")

        # TikTokApi.create_sessions(cookies=...) butuh list of DICT,
        # satu dict per session, formatnya {cookieName: value}.
        # Library akan inject cookies ini ke browser context sendiri
        # dan otomatis overwrite key "msToken" dengan ms_tokens parameter.
        cookie_dict = {
            ("msToken" if k == "ms_token" else k): v
            for k, v in cookies.items()
            if v
        }

        # Headless bisa di-disable lewat env var untuk debugging visual.
        # Set TIKTOK_HEADLESS=false → Chromium muncul di layar PC, Anda
        # bisa lihat sendiri apakah TikTok serve captcha/login wall.
        headless_env = os.environ.get("TIKTOK_HEADLESS", "true").strip().lower()
        headless = headless_env not in ("false", "0", "no")

        # Context options buat fingerprint yang lebih "natural" — bantu
        # mengurangi false-positive dari heuristic anti-bot TikTok.
        context_options = {
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "viewport": {"width": 1280, "height": 800},
        }

        posts: List[Dict] = []
        async with TikTokApi() as api:
            await api.create_sessions(
                ms_tokens=[ms_token],
                num_sessions=1,
                sleep_after=3,
                headless=headless,
                cookies=[cookie_dict],
                context_options=context_options,
            )

            # 1. Hashtag attempt (paling reliable)
            try:
                tag = api.hashtag(name=keyword)
                async for video in tag.videos(count=amount):
                    try:
                        posts.append(_normalize_video(video.as_dict))
                        if len(posts) >= amount:
                            break
                    except Exception as e:
                        logger.warning(f"[tiktok] normalize failed: {e}")
                logger.info(
                    f"[tiktok] hashtag '#{keyword}' returned {len(posts)} videos"
                )
            except Exception as e:
                logger.info(f"[tiktok] hashtag '{keyword}' attempt failed: {e}")

            # 2. Trending fallback (filter by keyword)
            if not posts:
                logger.info(
                    f"[tiktok] hashtag empty, fallback to trending filter '{keyword}'"
                )
                try:
                    kw_lower = keyword.lower()
                    async for video in api.trending.videos(count=amount * 4):
                        d = video.as_dict
                        desc = (d.get("desc") or "").lower()
                        if kw_lower in desc:
                            posts.append(_normalize_video(d))
                            if len(posts) >= amount:
                                break
                    logger.info(
                        f"[tiktok] trending+filter returned {len(posts)} videos"
                    )
                except Exception as e:
                    logger.warning(f"[tiktok] trending fallback failed: {e}")

        self.account_store.mark_used(username)
        return posts

    def _infer_status(self, error: Exception) -> Optional[str]:
        s = str(error).lower()
        if "captcha" in s or "challenge" in s or "verify" in s:
            return self.account_store.STATUS_CHALLENGE
        if "401" in s or "403" in s or "login" in s or "unauthorized" in s:
            return self.account_store.STATUS_EXPIRED
        if "429" in s or "rate" in s:
            return self.account_store.STATUS_RATE_LIMITED
        return None
