"""
Twitter / X Account Manager (cookie-based)
============================================
v3 (7 May 2026): full migrate dari twikit (user/pass + email + TOTP login)
ke cookie-based, mirror pattern Facebook & TikTok. Library `twikit` di-DROP.

Format input yang diterima saat POST /accounts (auto-detect):

  A. STORAGE STATE (RECOMMENDED — output `context.storage_state()` Playwright,
     contoh `twitter_<label>.json` dari multi_capture):
     {
       "cookies": [
         {"name":"auth_token","value":"...","domain":".x.com","path":"/",
          "expires":..., "httpOnly":true, "secure":true, "sameSite":"None"},
         {"name":"ct0","value":"...", ...},
         {"name":"twid","value":"u%3D...", ...},
         ...
       ],
       "origins": [...]
     }

  B. FLAT DICT (LEGACY):
     {"auth_token":"...","ct0":"...","twid":"u%3D..."}

  C. SEMICOLON STRING (LEGACY):
     "auth_token=...; ct0=...; twid=..."

Cookie minimum yang dibutuhkan: `auth_token` + `ct0`. `twid` recommended
karena dipakai untuk identify user_id (format `u%3D<userid>`).

Cara dapat storage_state:
  1. Pakai multi_capture, login X manual di Chromium real, push lewat GUI.
  2. Atau manual: login ke x.com, F12 > Application > Cookies > .x.com →
     copy auth_token, ct0, twid.

Format check:
  - auth_token: hex-string panjang ~40 char
  - ct0: hex-string panjang ~32-160 char (CSRF token)
  - twid (jika ada): format `u%3D<numeric_user_id>` — `u%3D` itu URL-encoded
    "u=" karena cookie value gak bisa langsung pakai "=" yang juga separator.
"""

import json
import logging
import re
from typing import Optional

from .base import BaseAccountManager

logger = logging.getLogger(__name__)


class TwitterAccountManager(BaseAccountManager):

    PLATFORM = "twitter"

    REQUIRED_COOKIES = ('auth_token', 'ct0')

    # Default attribute saat convert dari flat dict (yang gak punya metadata)
    # Twitter pindah ke .x.com tapi sebagian user/cookie masih .twitter.com.
    # Kita default-kan ke '.x.com' karena itu domain primary sekarang.
    _DEFAULT_DOMAIN = '.x.com'
    _DEFAULT_PATH = '/'
    _HTTP_ONLY_HINTS = frozenset({'auth_token', 'kdt', 'twid'})

    # ------------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------------

    def _do_login(self, username: str, password: str, verification_code: Optional[str] = None) -> dict:
        """Parse input → normalize ke storage_state → validate → save."""
        state = self._parse_session_input(password)

        cookies_lookup = self._cookies_to_lookup(state)
        missing = [c for c in self.REQUIRED_COOKIES if not cookies_lookup.get(c)]
        if missing:
            raise Exception(
                f"Twitter butuh cookies: {', '.join(self.REQUIRED_COOKIES)}. "
                f"Yang kurang: {', '.join(missing)}.\n"
                "Cara dapat: login ke x.com > F12 > Application > Cookies, "
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

        # Extract user_id dari twid kalau ada (format "u%3D<userid>")
        twitter_user_id = self._extract_user_id_from_twid(cookies_lookup.get('twid'))

        logger.info(
            f"[twitter] Saved storage_state for {username}: "
            f"{n_cookies} cookies, {n_ls_items} localStorage items, "
            f"user_id={twitter_user_id}"
        )
        return {
            'session_type': 'storage_state',
            'twitter_user_id': twitter_user_id,
        }

    def get_cookies(self, username: str) -> dict:
        """
        Return Playwright storage_state dict.
        Auto-migrate dari format lama (flat dict) kalau session file masih legacy.
        """
        path = self._session_path(username)
        if not path.exists():
            raise Exception(f"No Twitter session for {username}")

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

        # Legacy formats — termasuk twikit cookies file (yang biasanya
        # flat dict {name: value} hasil pickle/json dump cookies)
        logger.info(
            f"[twitter] Auto-migrating legacy session "
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
                "flat dict JSON, atau semicolon string 'auth_token=...; ct0=...'."
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
                'sameSite': c.get('sameSite') or 'None',
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
        """Convert legacy {name: value} → storage_state."""
        cookies = []
        for name, value in flat.items():
            if value in (None, ''):
                continue
            if not isinstance(value, (str, int, float, bool)):
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
    def _extract_user_id_from_twid(twid: Optional[str]) -> Optional[str]:
        """
        twid format: 'u%3D<userid>' (URL-encoded "u=<userid>")
        Return string user_id atau None.
        """
        if not twid:
            return None
        # URL decode minimal: %3D → =
        decoded = twid.replace('%3D', '=')
        m = re.match(r'^u=(\d+)$', decoded)
        if m:
            return m.group(1)
        # Kalau cuma angka raw, terima
        if decoded.isdigit():
            return decoded
        return None

    @staticmethod
    def _validate_cookies(cookies: dict) -> None:
        """
        Format-only validation. NO live HTTP/Playwright check.

        Format check:
          - auth_token: harus hex-string panjang minimal 32 char
          - ct0: minimal 32 char
          - twid (optional): format `u%3D<digits>` atau `u=<digits>`
        """
        auth_token = (cookies.get('auth_token') or '').strip()
        ct0 = (cookies.get('ct0') or '').strip()

        if len(auth_token) < 32:
            raise Exception(
                f"auth_token terlalu pendek (panjang {len(auth_token)}, "
                f"minimal 32 char). auth_token Twitter biasanya 40 hex chars."
            )
        # auth_token umumnya hex, tapi jangan strict reject — Twitter kadang ubah
        if not re.match(r'^[a-f0-9]+$', auth_token, re.IGNORECASE):
            logger.warning(
                f"[twitter] auth_token tidak fully hex (umumnya 40-char hex). "
                f"Tetap di-accept tapi mungkin invalid."
            )

        if len(ct0) < 32:
            raise Exception(
                f"ct0 terlalu pendek (panjang {len(ct0)}, minimal 32 char). "
                f"ct0 (CSRF token) biasanya 32-160 hex chars."
            )

        # twid optional
        twid = (cookies.get('twid') or '').strip()
        if twid:
            decoded = twid.replace('%3D', '=')
            if not (re.match(r'^u=\d+$', decoded) or decoded.isdigit()):
                logger.warning(
                    f"[twitter] twid format tidak dikenali ('{twid[:30]}'). "
                    f"Umumnya 'u%3D<userid>'. Tetap di-accept."
                )

        logger.info(
            f"[twitter] Cookie format OK (auth_token len={len(auth_token)}, "
            f"ct0 len={len(ct0)}). "
            f"Real validation akan terjadi saat scrape pertama."
        )

    # ------------------------------------------------------------------------
    # Override mark_error
    # ------------------------------------------------------------------------

    def mark_error(self, username: str, error: Exception, status: Optional[str] = None):
        """Auto-detect status dari error message."""
        if status is None:
            err = str(error).lower()
            if 'suspended' in err or 'banned' in err:
                status = self.STATUS_BANNED
            elif 'login' in err or 'unauthorized' in err or '401' in err or 'expired' in err:
                status = self.STATUS_EXPIRED
            elif 'challenge' in err or 'verify' in err or 'suspicious' in err or 'checkpoint' in err:
                status = self.STATUS_CHALLENGE
            elif 'rate' in err or 'too many' in err or '429' in err:
                status = self.STATUS_RATE_LIMITED
        super().mark_error(username, error, status)
