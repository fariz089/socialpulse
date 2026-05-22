"""
Facebook Account Manager
=========================
v2 (6 May 2026): switch ke Playwright `storage_state` format

Sama seperti TikTok: Facebook tidak bisa login programatik (selalu kena
checkpoint/2FA). User harus paste cookie/session dari browser.

Format input yang diterima saat POST /accounts (auto-detect):

  A. STORAGE STATE (RECOMMENDED — sama dengan output `context.storage_state()`
     Playwright, contoh: `fb_session.json` dari fb_capture):
     {
       "cookies": [
         {"name":"c_user","value":"...","domain":".facebook.com","path":"/",
          "expires":1812629381.04,"httpOnly":false,"secure":true,"sameSite":"None"},
         {"name":"xs","value":"...", ...},
         ...
       ],
       "origins": [
         {"origin":"https://www.facebook.com",
          "localStorage":[{"name":"...","value":"..."}, ...]}
       ]
     }
     Cookies di sini bawa attribute lengkap (expires, httpOnly, sameSite),
     plus `localStorage` per-origin. Inject ke Playwright context jadi paling
     fidel — identik dengan session yang fb_capture pakai.

  B. FLAT DICT (LEGACY — tetap diterima untuk kompatibilitas):
     {"c_user":"100012345","xs":"..","datr":"..","fr":".."}
     Akan auto-convert ke storage_state internally. Attribute fallback
     (expires=session, httpOnly heuristic dari nama cookie).

  C. SEMICOLON STRING (LEGACY):
     "c_user=...; xs=...; datr=..."
     Auto-convert ke flat dict, lalu ke storage_state.

Cookie minimum yang dibutuhkan: `c_user` + `xs`. Sisanya recommended tapi
tidak strict.

Cara dapat storage_state lengkap (paling reliable):
  1. Login ke facebook.com di Playwright/Selenium browser session
  2. `state = context.storage_state(path='fb_session.json')`
  3. Paste isi `fb_session.json` ke field password POST /accounts.

Atau cara manual untuk flat dict (cara lama):
  1. Login ke facebook.com di browser desktop
  2. F12 > Application > Cookies > facebook.com
  3. Copy nilai c_user, xs, datr, fr, sb, dpr
  4. POST /accounts dengan password={"c_user":"...","xs":"..."}
"""

import json
import logging
from typing import Optional

from .base import BaseAccountManager

logger = logging.getLogger(__name__)


class FacebookAccountManager(BaseAccountManager):

    PLATFORM = "facebook"

    REQUIRED_COOKIES = ('c_user', 'xs')

    # Default attribute saat convert dari flat dict (yang gak punya metadata)
    _DEFAULT_DOMAIN = '.facebook.com'
    _DEFAULT_PATH = '/'
    # Heuristik: cookies ini biasanya httpOnly oleh FB. Saat user paste flat
    # dict tanpa metadata, kita tebak supaya inject mendekati real browser.
    _HTTP_ONLY_HINTS = frozenset({'xs', 'fr', 'datr', 'sb'})

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
                f"Facebook butuh cookies: {', '.join(self.REQUIRED_COOKIES)}. "
                f"Yang kurang: {', '.join(missing)}.\n"
                "Cara dapat: login ke facebook.com > F12 > Application > Cookies, "
                "atau paste isi fb_session.json hasil Playwright context.storage_state()."
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
        logger.info(
            f"[facebook] Saved storage_state for {username}: "
            f"{n_cookies} cookies, {n_ls_items} localStorage items"
        )
        return {
            'session_type': 'storage_state',
            'fb_user_id': cookies_lookup.get('c_user'),
        }

    def get_cookies(self, username: str) -> dict:
        """
        Return Playwright storage_state dict:
            {"cookies": [...], "origins": [...]}

        Auto-migrate dari format lama (flat dict) kalau session file masih
        bentuk legacy. Tidak nulis ulang file — biarkan migrasi di-trigger
        saat user re-add account next time, supaya read path tidak side-effect.
        """
        path = self._session_path(username)
        if not path.exists():
            raise Exception(f"No Facebook session for {username}")

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
            f"[facebook] Auto-migrating legacy flat-dict session "
            f"for {username} → storage_state shape"
        )
        return self._flat_dict_to_storage_state(data)

    # ------------------------------------------------------------------------
    # Input parsing
    # ------------------------------------------------------------------------

    @classmethod
    def _parse_session_input(cls, raw: str) -> dict:
        """
        Auto-detect format input dan return canonical storage_state dict.
        """
        text = (raw or '').strip()
        if not text:
            raise Exception("Cookie input kosong.")

        # JSON: bisa storage_state ATAU flat dict
        if text.startswith('{') or text.startswith('['):
            try:
                data = json.loads(text)
            except Exception as e:
                raise Exception(f"JSON tidak valid: {e}")

            if not isinstance(data, dict):
                raise Exception(
                    f"JSON harus object, bukan {type(data).__name__}."
                )

            # Storage_state shape: punya key 'cookies' bertipe list
            if isinstance(data.get('cookies'), list):
                return cls._normalize_storage_state(data)

            # Flat dict legacy
            return cls._flat_dict_to_storage_state(data)

        # Format C: semicolon string "c_user=...; xs=..."
        flat = {}
        for part in text.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                flat[k.strip()] = v.strip()
        if not flat:
            raise Exception(
                "Format cookie tidak dikenali. Diharapkan: storage_state JSON, "
                "flat dict JSON, atau semicolon string 'c_user=...; xs=...'."
            )
        return cls._flat_dict_to_storage_state(flat)

    @classmethod
    def _normalize_storage_state(cls, state: dict) -> dict:
        """
        Pastikan setiap cookie punya minimum field valid + sane defaults.
        Drop cookie tanpa name/value. Origins di-pass through apa adanya.
        """
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
                'sameSite': c.get('sameSite') or 'None',
            }
            # expires optional (epoch float). -1 / missing = session cookie.
            if 'expires' in c and c['expires'] is not None:
                try:
                    normalized['expires'] = float(c['expires'])
                except (TypeError, ValueError):
                    pass
            cookies.append(normalized)

        # Origins: validate shape but jangan terlalu strict — pass through
        # apa pun yang shape-nya plausible.
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
        """Convert legacy {name: value} → storage_state. Origins selalu []."""
        cookies = []
        for name, value in flat.items():
            if value in (None, ''):
                continue
            cookies.append({
                'name': str(name),
                'value': str(value),
                'domain': cls._DEFAULT_DOMAIN,
                'path': cls._DEFAULT_PATH,
                'httpOnly': str(name) in cls._HTTP_ONLY_HINTS,
                'secure': True,
                'sameSite': 'None',
            })
        return {'cookies': cookies, 'origins': []}

    # ------------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------------

    @staticmethod
    def _cookies_to_lookup(state: dict) -> dict:
        """Build flat {name: value} lookup dari state.cookies untuk validation."""
        out = {}
        for c in (state.get('cookies') or []):
            n = c.get('name')
            v = c.get('value')
            if n and v not in (None, ''):
                out[n] = str(v)
        return out

    @staticmethod
    def _validate_cookies(cookies: dict) -> None:
        """
        Format-only validation. NO live HTTP/Playwright check.

        Kenapa?
          - Plain `requests` ke FB endpoints kena 400/403 karena anti-bot
            (gak punya CSRF tokens, fingerprint mismatch).
          - Playwright validation di sini akan bentrok dengan scraper worker
            thread (Playwright sync API gak thread-safe; Flask multi-threaded).
          - Real validation tetap terjadi saat scrape pertama: Playwright deteksi
            redirect ke /login → mark akun EXPIRED otomatis.

        Format check:
          - c_user harus numerik, minimal 8 digit (FB user ID)
          - xs minimal 20 char (session token biasanya 50+)
        """
        c_user = (cookies.get('c_user') or '').strip()
        xs = (cookies.get('xs') or '').strip()

        if not c_user.isdigit():
            raise Exception(
                f"c_user format invalid: harus numerik (FB user ID). "
                f"Yg ke-paste: '{c_user[:30]}'. Pastiin copy nilai cookie c_user, "
                f"bukan field lain."
            )
        if len(c_user) < 8:
            raise Exception(
                f"c_user terlalu pendek (panjang {len(c_user)}, minimal 8 digit). "
                f"Mungkin keliru copy nilai cookie lain."
            )
        if len(xs) < 20:
            raise Exception(
                f"xs terlalu pendek (panjang {len(xs)}, minimal 20 char). "
                f"xs cookie biasanya 50-100+ karakter."
            )

        logger.info(
            f"[facebook] Cookie format OK (c_user={c_user[:6]}..., xs len={len(xs)}). "
            f"Real validation akan terjadi saat scrape pertama."
        )