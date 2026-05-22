"""
Threads Account Manager (cookie-based, share auth dengan Instagram)
=====================================================================
v1 (13 May 2026): Threads adalah child platform Meta yang share auth
dengan Instagram. Cookie wajib (sessionid + ds_user_id) di domain
`.instagram.com` di-share oleh threads.net via SSO. Untuk web scraping,
threads.net juga set cookie sendiri di domain `.threads.net` setelah
session warm-up.

Strategi auth Threads:
  - REQUIRED: sessionid + ds_user_id (sama persis dengan IG)
  - Domain cookies bisa di .instagram.com, .threads.net, atau keduanya
  - threads.net otomatis pick up auth dari .instagram.com saat ada flag
    `ig_did` dan sessionid yang valid → kita inject cookies ke kedua
    domain supaya safety.

Format input yang diterima saat POST /accounts (auto-detect):

  A. STORAGE STATE (RECOMMENDED — hasil capture dari multi_capture
     dengan login ke threads.net, contoh `threads_<label>.json`):
     {
       "cookies": [
         {"name":"sessionid","value":"...","domain":".instagram.com",...},
         {"name":"ds_user_id","value":"...","domain":".instagram.com",...},
         {"name":"sessionid","value":"...","domain":".threads.net",...},
         ...
       ],
       "origins": [
         {"origin":"https://www.threads.net", "localStorage":[...]},
         {"origin":"https://www.instagram.com", "localStorage":[...]}
       ]
     }

  B. INSTAGRAM SESSION REUSE (NICE-TO-HAVE — kalau user belum capture
     Threads tapi sudah punya session IG, cookie IG sah dipakai untuk
     Threads. Auto-deteksi: kalau dapat storage_state IG dan domain
     cookies semua .instagram.com, kita pass-through — Playwright
     akan inject ke kedua domain saat scraping).

  C. FLAT DICT (LEGACY):
     {"sessionid":"...","ds_user_id":"...","csrftoken":"...","ig_did":"..."}

  D. SEMICOLON STRING (LEGACY):
     "sessionid=...; ds_user_id=...; csrftoken=..."

Cookie minimum: `sessionid` + `ds_user_id`. `csrftoken` & `ig_did`
recommended (ig_did dipakai web threads.net untuk client identifier).

Validasi sessionid format: ada `%3A` (URL-encoded `:`) dengan ds_user_id
sebagai prefix — sama persis dengan IG. Threads memang reuse format.
"""

import json
import logging
from typing import Optional

from .base import BaseAccountManager

logger = logging.getLogger(__name__)


class ThreadsAccountManager(BaseAccountManager):

    PLATFORM = "threads"

    # Sama dengan IG: sessionid + ds_user_id wajib. csrftoken dan ig_did
    # secara teknis recommended tapi tidak strict (Threads kadang jalan tanpa csrftoken
    # untuk read-only ops).
    REQUIRED_COOKIES = ('sessionid', 'ds_user_id')

    # Threads punya 2 domain: .threads.net (web utama) dan .instagram.com (SSO).
    # Saat user paste flat dict tanpa metadata, default ke .instagram.com karena
    # mayoritas cookie auth Threads memang asalnya dari domain itu.
    _DEFAULT_DOMAIN = '.instagram.com'
    _DEFAULT_PATH = '/'
    _HTTP_ONLY_HINTS = frozenset({'sessionid', 'rur', 'ig_did'})

    # ------------------------------------------------------------------------
    # Public API (BaseAccountManager interface)
    # ------------------------------------------------------------------------

    def _do_login(self, username: str, password: str, verification_code: Optional[str] = None) -> dict:
        """Parse input → normalize ke storage_state → validate → save."""
        state = self._parse_session_input(password)

        cookies_lookup = self._cookies_to_lookup(state)
        missing = [c for c in self.REQUIRED_COOKIES if not cookies_lookup.get(c)]
        if missing:
            raise Exception(
                f"Threads butuh cookies: {', '.join(self.REQUIRED_COOKIES)}. "
                f"Yang kurang: {', '.join(missing)}.\n"
                "Threads share auth dengan Instagram — login ke threads.net "
                "atau pakai session IG yang valid. "
                "Cara dapat: pakai multi_capture (pilih platform Threads), "
                "atau copy cookie dari instagram.com (sessionid+ds_user_id "
                "yang sama dipakai oleh Threads via SSO)."
            )

        try:
            self._validate_cookies(cookies_lookup)
        except Exception as e:
            raise Exception(f"Cookie validation failed: {e}")

        # Persist canonical storage_state shape
        self._session_path(username).write_text(json.dumps(state, indent=2))

        n_cookies = len(state.get('cookies') or [])
        n_ls_items = sum(
            len(o.get('localStorage') or [])
            for o in (state.get('origins') or [])
        )

        # Cek domain coverage — info log buat troubleshooting
        domains = {c.get('domain') for c in (state.get('cookies') or []) if c.get('domain')}
        has_threads_domain = any('threads.net' in d for d in domains)
        has_ig_domain = any('instagram.com' in d for d in domains)

        logger.info(
            f"[threads] Saved storage_state for {username}: "
            f"{n_cookies} cookies, {n_ls_items} localStorage items, "
            f"domains_threads={has_threads_domain}, domains_ig={has_ig_domain}"
        )

        if not has_threads_domain and not has_ig_domain:
            logger.warning(
                f"[threads] Cookies tidak ada di .threads.net maupun "
                f".instagram.com — scrape mungkin gagal."
            )

        return {
            'session_type': 'storage_state',
            'ig_user_id': cookies_lookup.get('ds_user_id'),
            'has_threads_domain': has_threads_domain,
            'has_ig_domain': has_ig_domain,
        }

    def get_cookies(self, username: str) -> dict:
        """
        Return Playwright storage_state dict:
            {"cookies": [...], "origins": [...]}

        Auto-migrate dari format lama (flat dict) kalau session file masih
        bentuk legacy.
        """
        path = self._session_path(username)
        if not path.exists():
            raise Exception(f"No Threads session for {username}")

        raw = path.read_text()
        try:
            data = json.loads(raw)
        except Exception as e:
            raise Exception(f"Session file corrupt for {username}: {e}")

        if not isinstance(data, dict):
            raise Exception(
                f"Session file format invalid for {username}: "
                f"expected dict, got {type(data).__name__}"
            )

        # Storage_state shape (canonical)
        if isinstance(data.get('cookies'), list):
            return self._normalize_storage_state(data)

        # Legacy flat dict — auto-convert
        logger.info(
            f"[threads] Auto-migrating legacy session "
            f"for {username} → storage_state shape"
        )
        return self._flat_dict_to_storage_state(data)

    # ------------------------------------------------------------------------
    # Input parsing
    # ------------------------------------------------------------------------

    @classmethod
    def _parse_session_input(cls, raw: str) -> dict:
        """Auto-detect format input dan return canonical storage_state dict."""
        text = (raw or '').strip()
        if not text:
            raise Exception("Cookie input kosong.")

        # JSON: storage_state ATAU flat dict
        if text.startswith('{') or text.startswith('['):
            try:
                data = json.loads(text)
            except Exception as e:
                raise Exception(f"JSON tidak valid: {e}")

            if not isinstance(data, dict):
                raise Exception(
                    f"JSON harus object, bukan {type(data).__name__}."
                )

            if isinstance(data.get('cookies'), list):
                return cls._normalize_storage_state(data)

            return cls._flat_dict_to_storage_state(data)

        # Semicolon string
        flat = {}
        for part in text.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                flat[k.strip()] = v.strip()
        if not flat:
            raise Exception(
                "Format cookie tidak dikenali. Diharapkan: storage_state JSON, "
                "flat dict JSON, atau semicolon string 'sessionid=...; ds_user_id=...'."
            )
        return cls._flat_dict_to_storage_state(flat)

    @classmethod
    def _normalize_storage_state(cls, state: dict) -> dict:
        """Pastikan setiap cookie punya minimum field valid."""
        cookies = []
        for c in (state.get('cookies') or []):
            if not isinstance(c, dict):
                continue
            name = (c.get('name') or '').strip()
            value = c.get('value')
            if not name or value in (None, ''):
                continue
            normalized = {
                'name': name,
                'value': str(value),
                'domain': c.get('domain') or cls._DEFAULT_DOMAIN,
                'path': c.get('path') or cls._DEFAULT_PATH,
                'httpOnly': bool(c.get('httpOnly', name in cls._HTTP_ONLY_HINTS)),
                'secure': bool(c.get('secure', True)),
                'sameSite': c.get('sameSite') or 'Lax',
            }
            if 'expires' in c and c['expires'] is not None:
                try:
                    normalized['expires'] = float(c['expires'])
                except (TypeError, ValueError):
                    pass
            cookies.append(normalized)

        origins = []
        for o in (state.get('origins') or []):
            if not isinstance(o, dict):
                continue
            origin = o.get('origin')
            ls = o.get('localStorage')
            if not origin:
                continue
            origins.append({
                'origin': origin,
                'localStorage': ls if isinstance(ls, list) else [],
            })

        return {'cookies': cookies, 'origins': origins}

    @classmethod
    def _flat_dict_to_storage_state(cls, flat: dict) -> dict:
        """
        Convert flat {name: value} → storage_state.

        SPECIAL: untuk Threads, kalau cookie wajib (sessionid, ds_user_id)
        ada di input, kita duplicate ke .threads.net DAN .instagram.com supaya
        Playwright bisa hit kedua domain. Cookie lain (csrftoken, ig_did, rur)
        cukup di .instagram.com — threads.net pick up otomatis.
        """
        cookies = []
        # Cookies yang HARUS ada di dua domain (auth utama)
        DUAL_DOMAIN_COOKIES = {'sessionid', 'ds_user_id'}

        for name, value in flat.items():
            if value in (None, ''):
                continue
            if not isinstance(value, (str, int, float, bool)):
                continue

            base_entry = {
                'name': str(name),
                'value': str(value),
                'path': cls._DEFAULT_PATH,
                'httpOnly': str(name) in cls._HTTP_ONLY_HINTS,
                'secure': True,
                'sameSite': 'Lax',
            }

            if str(name) in DUAL_DOMAIN_COOKIES:
                # Duplicate ke kedua domain
                cookies.append({**base_entry, 'domain': '.instagram.com'})
                cookies.append({**base_entry, 'domain': '.threads.net'})
            else:
                cookies.append({**base_entry, 'domain': cls._DEFAULT_DOMAIN})

        return {'cookies': cookies, 'origins': []}

    # ------------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------------

    @staticmethod
    def _cookies_to_lookup(state: dict) -> dict:
        """
        Build flat {name: value} lookup dari state.cookies untuk validation.
        Kalau cookie sama ada di multiple domain, prioritaskan .instagram.com
        (karena itu yang authoritative untuk Threads auth).
        """
        out = {}
        priority = {}  # name -> domain priority score
        for c in (state.get('cookies') or []):
            n = c.get('name')
            v = c.get('value')
            d = c.get('domain', '')
            if not n or v in (None, ''):
                continue
            # Score: .instagram.com tertinggi, lalu .threads.net, lalu lainnya
            score = 3 if 'instagram.com' in d else (2 if 'threads.net' in d else 1)
            if n not in out or score > priority.get(n, 0):
                out[n] = str(v)
                priority[n] = score
        return out

    @staticmethod
    def _validate_cookies(cookies: dict) -> None:
        """
        Format-only validation. NO live HTTP/Playwright check.

        Format check (sama persis dengan IG karena Threads share auth):
          - ds_user_id harus numerik, minimal 4 digit
          - sessionid: panjang minimal 20 char, umumnya berisi `%3A`
        """
        ds_user_id = (cookies.get('ds_user_id') or '').strip()
        sessionid = (cookies.get('sessionid') or '').strip()

        if not ds_user_id.isdigit():
            raise Exception(
                f"ds_user_id format invalid: harus numerik (IG/Threads user ID). "
                f"Yg ke-paste: '{ds_user_id[:30]}'. Pastiin copy nilai cookie "
                f"ds_user_id, bukan field lain."
            )
        if len(ds_user_id) < 4:
            raise Exception(
                f"ds_user_id terlalu pendek (panjang {len(ds_user_id)}, minimal 4 digit)."
            )
        if len(sessionid) < 20:
            raise Exception(
                f"sessionid terlalu pendek (panjang {len(sessionid)}, minimal 20 char). "
                f"sessionid Threads (sama dengan IG) biasanya 40-100+ karakter."
            )
        if '%3A' not in sessionid and ':' not in sessionid:
            logger.warning(
                f"[threads] sessionid tidak mengandung '%3A' atau ':' "
                f"(format umumnya `<userid>%3A<token>`). "
                f"Cookies tetap di-accept tapi mungkin invalid."
            )

        logger.info(
            f"[threads] Cookie format OK (ds_user_id={ds_user_id}, "
            f"sessionid len={len(sessionid)}). "
            f"Real validation akan terjadi saat scrape pertama."
        )

    # ------------------------------------------------------------------------
    # Override mark_error
    # ------------------------------------------------------------------------

    def mark_error(self, username: str, error: Exception, status: Optional[str] = None):
        """Auto-detect status dari error message."""
        if status is None:
            err = str(error).lower()
            if 'login' in err or 'expired' in err or 'cookie' in err:
                status = self.STATUS_EXPIRED
            elif 'challenge' in err or 'checkpoint' in err or 'verify' in err:
                status = self.STATUS_CHALLENGE
            elif '429' in err or 'rate' in err or 'too many' in err:
                status = self.STATUS_RATE_LIMITED
            elif 'banned' in err or 'suspended' in err or 'disabled' in err:
                status = self.STATUS_BANNED
        super().mark_error(username, error, status)
