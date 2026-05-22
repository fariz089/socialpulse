"""
Instagram Account Manager (cookie-based)
==========================================
v3 (7 May 2026): full migrate dari instagrapi (user/pass login) ke cookie-based,
mirror pattern Facebook & TikTok. Library `instagrapi` di-DROP — semua scraping
sekarang via Playwright + cookies dari multi_capture.

Format input yang diterima saat POST /accounts (auto-detect):

  A. STORAGE STATE (RECOMMENDED — sama dengan output `context.storage_state()`
     Playwright, contoh `instagram_<label>.json` dari multi_capture):
     {
       "cookies": [
         {"name":"sessionid","value":"...","domain":".instagram.com","path":"/",
          "expires":..., "httpOnly":true, "secure":true, "sameSite":"None"},
         {"name":"ds_user_id","value":"...", ...},
         {"name":"csrftoken","value":"...", ...},
         ...
       ],
       "origins": [
         {"origin":"https://www.instagram.com",
          "localStorage":[{"name":"...","value":"..."}, ...]}
       ]
     }
     Cookies di sini bawa attribute lengkap (expires, httpOnly, sameSite),
     plus `localStorage` per-origin.

  B. FLAT DICT (LEGACY — tetap diterima):
     {"sessionid":"...","ds_user_id":"...","csrftoken":"...","rur":"..."}
     Auto-convert ke storage_state internally.

  C. SEMICOLON STRING (LEGACY):
     "sessionid=...; ds_user_id=...; csrftoken=..."

Cookie minimum yang dibutuhkan: `sessionid` + `ds_user_id`. `csrftoken` &
`rur` recommended tapi tidak strict.

Cara dapat storage_state lengkap:
  1. Pakai multi_capture (sister tool standalone), login IG manual di
     Chromium real, push lewat tombol "Push to SocialPulse" di GUI.
  2. Atau manual: login ke instagram.com di browser → F12 > Application >
     Cookies > instagram.com → copy nilai sessionid, ds_user_id, csrftoken.

Validasi sessionid format: ada `%3A` (URL-encoded `:`) dengan ds_user_id
sebagai prefix — IG sessionid biasanya bentuknya `<userid>%3A<token>`.
"""

import json
import logging
from typing import Optional

from .base import BaseAccountManager

logger = logging.getLogger(__name__)


class InstagramAccountManager(BaseAccountManager):

    PLATFORM = "instagram"

    REQUIRED_COOKIES = ('sessionid', 'ds_user_id')

    # Default attribute saat convert dari flat dict (yang gak punya metadata)
    _DEFAULT_DOMAIN = '.instagram.com'
    _DEFAULT_PATH = '/'
    # Heuristik: cookies ini biasanya httpOnly oleh IG. Saat user paste flat
    # dict tanpa metadata, kita tebak supaya inject mendekati real browser.
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
                f"Instagram butuh cookies: {', '.join(self.REQUIRED_COOKIES)}. "
                f"Yang kurang: {', '.join(missing)}.\n"
                "Cara dapat: login ke instagram.com > F12 > Application > Cookies, "
                "atau pakai multi_capture untuk capture session lengkap."
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
            f"[instagram] Saved storage_state for {username}: "
            f"{n_cookies} cookies, {n_ls_items} localStorage items"
        )
        return {
            'session_type': 'storage_state',
            'ig_user_id': cookies_lookup.get('ds_user_id'),
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
            raise Exception(f"No Instagram session for {username}")

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

        # Legacy flat dict (or instagrapi settings format) — auto-convert
        # NOTE: instagrapi format punya nested 'authorization_data' / 'cookies'
        # dict. Kita extract apa pun yang bisa diparse sebagai cookies.
        logger.info(
            f"[instagram] Auto-migrating legacy session "
            f"for {username} → storage_state shape"
        )
        return self._legacy_to_storage_state(data)

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

            # Flat dict legacy — atau instagrapi settings (akan kita try extract)
            return cls._legacy_to_storage_state(data)

        # Format C: semicolon string "sessionid=...; ds_user_id=..."
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
                'sameSite': c.get('sameSite') or 'Lax',
            }
            # expires optional (epoch float). -1 / missing = session cookie.
            if 'expires' in c and c['expires'] is not None:
                try:
                    normalized['expires'] = float(c['expires'])
                except (TypeError, ValueError):
                    pass
            cookies.append(normalized)

        # Origins: validate shape but jangan terlalu strict — pass through
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
            # Skip kalau bukan string-able value (mis. dict nested instagrapi)
            if not isinstance(value, (str, int, float, bool)):
                continue
            cookies.append({
                'name': str(name),
                'value': str(value),
                'domain': cls._DEFAULT_DOMAIN,
                'path': cls._DEFAULT_PATH,
                'httpOnly': str(name) in cls._HTTP_ONLY_HINTS,
                'secure': True,
                'sameSite': 'Lax',
            })
        return {'cookies': cookies, 'origins': []}

    @classmethod
    def _legacy_to_storage_state(cls, data: dict) -> dict:
        """
        Convert legacy formats ke storage_state:
          - Flat dict {name: value, ...} → langsung
          - instagrapi settings (nested) → extract dari 'authorization_data'
            atau 'cookies' subdict
        """
        # instagrapi settings format: punya 'cookies' sebagai dict nested
        nested_cookies = data.get('cookies')
        if isinstance(nested_cookies, dict) and nested_cookies:
            # Bisa jadi flat-style {name: value} di nested
            return cls._flat_dict_to_storage_state(nested_cookies)

        # instagrapi authorization_data: punya 'sessionid', 'ds_user_id', 'csrftoken' direct
        auth_data = data.get('authorization_data')
        if isinstance(auth_data, dict) and auth_data:
            return cls._flat_dict_to_storage_state(auth_data)

        # Generic fallback: treat as flat dict
        return cls._flat_dict_to_storage_state(data)

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

        Format check:
          - ds_user_id harus numerik, minimal 4 digit (IG user ID)
          - sessionid: panjang minimal 20 char DAN umumnya kontain "%3A"
            (URL-encoded ":") karena format-nya `<userid>%3A<token>%3A...`
        """
        ds_user_id = (cookies.get('ds_user_id') or '').strip()
        sessionid = (cookies.get('sessionid') or '').strip()

        if not ds_user_id.isdigit():
            raise Exception(
                f"ds_user_id format invalid: harus numerik (IG user ID). "
                f"Yg ke-paste: '{ds_user_id[:30]}'. Pastiin copy nilai cookie "
                f"ds_user_id, bukan field lain."
            )
        if len(ds_user_id) < 4:
            raise Exception(
                f"ds_user_id terlalu pendek (panjang {len(ds_user_id)}, minimal 4 digit). "
                f"Mungkin keliru copy nilai cookie lain."
            )
        if len(sessionid) < 20:
            raise Exception(
                f"sessionid terlalu pendek (panjang {len(sessionid)}, minimal 20 char). "
                f"sessionid IG biasanya 40-100+ karakter."
            )
        # Soft check: warn kalau sessionid gak punya '%3A' (encoded ':') tapi
        # tidak strict reject — IG kadang ubah format.
        if '%3A' not in sessionid and ':' not in sessionid:
            logger.warning(
                f"[instagram] sessionid tidak mengandung '%3A' atau ':' "
                f"(format umumnya `<userid>%3A<token>`). "
                f"Cookies tetap di-accept tapi mungkin invalid."
            )

        logger.info(
            f"[instagram] Cookie format OK (ds_user_id={ds_user_id}, "
            f"sessionid len={len(sessionid)}). "
            f"Real validation akan terjadi saat scrape pertama."
        )

    # ------------------------------------------------------------------------
    # Override mark_error untuk infer status dari error message
    # ------------------------------------------------------------------------

    def mark_error(self, username: str, error: Exception, status: Optional[str] = None):
        """
        Auto-detect status dari error message Playwright (cookie expired,
        challenge, dll). Karena kita tidak pakai instagrapi lagi, exception
        type-nya generic — kita inspect string-nya.
        """
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
