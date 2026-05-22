"""
TikTok Account Manager
=======================
TikTok tidak bisa login programatik (selalu kena captcha/SMS). User login
manual di browser, lalu paste cookie ke endpoint /accounts.

Cookies penting yang dipakai (auto-extract dari input apa pun):
  ms_token         — paling penting, dipakai saat hit endpoint API TikTok
  sessionid        — auth utama, login user
  sessionid_ss     — auth secondary, dipakai endpoint *_ss
  sid_guard        — wrapper sessionid + expire info
  tt_chain_token   — anti-bot challenge token
  ttwid            — device fingerprint, kuat untuk anti-detect
  tt_csrf_token    — CSRF token untuk POST endpoint
  uid_tt           — user ID hash
  passport_csrf_token, cmpl_token, tea_web_id — anti-bot fingerprint
  store-country-code, store-idc, tt-target-idc — region info

Lima format input yang diterima di field `password` (auto-detect):

  Option A — Header Cookie string (paling gampang, copy dari DevTools Network):
    "msToken=abc...; sessionid=def...; sessionid_ss=def...; sid_guard=...; ..."
  
  Option B — JSON flat dict eksplisit:
    {"ms_token": "...", "sessionid": "...", "sessionid_ss": "...", ...}
  
  Option C — Cookie editor export (Chrome cookie editor extension format):
    Tab-separated table, satu cookie per baris (auto-detect)
  
  Option D — String ms_token saja (legacy, masih support):
    "abc123..." (hanya ms_token, scraper jalan tapi rate-limit lebih ketat)
  
  Option E — Playwright storage_state JSON (RECOMMENDED — paling lengkap):
    {"cookies":[{"name":"msToken","value":"...","domain":".tiktok.com",...},...],
     "origins":[...]}
    Format dari multi_capture / fb_capture. msToken multi-domain otomatis
    di-resolve (parent domain ".tiktok.com" diprioritaskan).

Tips:
  Cara paling lengkap & reliable: pakai multi_capture (sister-tool standalone),
  login manual di Chromium real, push storage_state via tombol di GUI.
  Cookies yang ke-capture jauh lebih lengkap (20-30 vs 5-15) → captcha challenge
  dari TikTok jadi jauh lebih jarang.
"""

import json
import logging
import re
from typing import Optional

from .base import BaseAccountManager

logger = logging.getLogger(__name__)


# Cookies yang scraper kenal dengan baik (untuk logging & priority sorting),
# tapi BUKAN whitelist filter — semua cookie ditemukan akan disimpan kecuali
# blacklist (telemetry/Akamai noise). Semakin lengkap cookies → semakin sedikit
# captcha challenge dari TikTok anti-bot.
COOKIE_KEYS = [
    'ms_token',           # Output canonical name (alias dari msToken)
    'sessionid',
    'sessionid_ss',
    'sid_guard',
    'sid_tt',
    'tt_chain_token',
    'ttwid',
    'tt_csrf_token',
    'uid_tt',
    'uid_tt_ss',
    'sid_ucp_v1',
    'ssid_ucp_v1',
    'odin_tt',
    'passport_csrf_token',
    'passport_csrf_token_default',
    'cmpl_token',
    'tt-passport-csrf-token',
    'tea_web_id',
    'last_login_method',
    'store-country-code',
    'store-idc',
    'tt-target-idc',
    'multi_sids',
]

# msToken di browser = ms_token di code kita (TikTok pakai camelCase di cookie,
# tapi snake_case lebih konsisten di Python). Sama dengan kebanyakan field
# lain yang diakses lewat ., bukan -.
COOKIE_ALIASES = {
    'mstoken': 'ms_token',
    'msToken': 'ms_token',
    'MS_TOKEN': 'ms_token',
}

# Cookies yang TIDAK BERGUNA untuk TikTokApi (Akamai bot mgmt + analytics),
# di-skip supaya tidak nyampah session file.
COOKIE_BLACKLIST = frozenset({
    '_abck', 'bm_sz', 'bm_mi', 'bm_so', 'bm_lso', 'ak_bmsc',
    '_ga', '_gid', '_gcl_au', '_fbp',
})


class TikTokAccountManager(BaseAccountManager):
    
    PLATFORM = "tiktok"
    
    def _do_login(self, username: str, password: str, verification_code: Optional[str] = None) -> dict:
        cookies = self._parse_cookie_input(password)
        
        if not cookies.get('ms_token'):
            raise Exception(
                "TikTok perlu ms_token. Cara dapat:\n"
                "1. Login ke tiktok.com di browser\n"
                "2. F12 > Application > Cookies > tiktok.com\n"
                "3. Copy nilai 'msToken' (yang domain '.tiktok.com', BUKAN 'www.tiktok.com')\n"
                "4. Re-add account dengan salah satu format di docstring."
            )
        
        # Validasi format ms_token (cek panjang saja, bukan hit network)
        try:
            self._validate_ms_token(cookies['ms_token'])
        except Exception as e:
            logger.warning(f"[tiktok] ms_token validation soft-failed: {e} (continuing anyway)")
        
        # Save SEMUA cookie yang kita parse, bukan hanya ms_token + sessionid
        self._session_path(username).write_text(json.dumps(cookies, indent=2))
        
        # Log info bermanfaat: jumlah total + breakdown recognized vs extra
        recognized = [k for k in COOKIE_KEYS if cookies.get(k)]
        extra = [k for k in cookies.keys() if k not in COOKIE_KEYS]
        logger.info(
            f"[tiktok] {username}: stored {len(cookies)} cookies total "
            f"({len(recognized)} recognized, {len(extra)} extra). "
            f"Recognized: {', '.join(recognized[:5])}"
            f"{'...' if len(recognized) > 5 else ''}"
        )
        
        return {
            'session_type': 'cookie',
            'has_sessionid': bool(cookies.get('sessionid')),
            'cookies_count': len(cookies),
        }
    
    @classmethod
    def _parse_cookie_input(cls, raw: str) -> dict:
        """
        Smart parser yang detect format input dan return dict cookies.
        Return dict yang sudah dinormalize (semua key snake_case, tanpa msToken).
        
        Format yang didukung (auto-detect):
          1. Playwright storage_state JSON ({"cookies":[...], "origins":[...]})
             ← format dari multi_capture/fb_capture
          2. JSON flat dict ({"msToken":"...", "sessionid":"...", ...})
          3. Tab-separated (DevTools Application > Cookies paste)
          4. Cookie header string (key=val; key=val)
          5. Plain ms_token (single line tanpa separator)
        """
        raw = (raw or '').strip()
        if not raw:
            return {}
        
        # Format 1 & 2: JSON
        if raw.startswith('{') or raw.startswith('['):
            try:
                parsed = json.loads(raw)
            except Exception as e:
                logger.warning(f"[tiktok] Failed to parse as JSON: {e}, trying other formats")
                parsed = None
            
            if isinstance(parsed, dict):
                # Format 1: storage_state ({"cookies":[...], ...})
                if isinstance(parsed.get('cookies'), list):
                    return cls._extract_from_storage_state(parsed)
                # Format 2: flat dict
                return cls._normalize_cookies(parsed)
        
        # Format 3: Tab-separated (DevTools Application > Cookies paste).
        # CHECK FIRST — multiple lines + tabs = strong signal, dan cookie value
        # bisa mengandung '=' yang akan bingungkan parser cookie-string.
        if '\t' in raw and '\n' in raw:
            parsed = cls._parse_tab_separated(raw)
            if parsed:
                return cls._normalize_cookies(parsed)
        
        # Format 4: Cookie header string (paling umum dari DevTools Network tab)
        if '=' in raw and (';' in raw or '\n' in raw):
            parsed = cls._parse_cookie_string(raw)
            if parsed:
                return cls._normalize_cookies(parsed)
        
        # Format 5: Plain ms_token string (legacy)
        if not any(c in raw for c in '={};\t\n'):
            return {'ms_token': raw}
        
        # Last resort: try cookie string parser anyway
        parsed = cls._parse_cookie_string(raw)
        if parsed:
            return cls._normalize_cookies(parsed)
        
        return {}
    
    @staticmethod
    def _parse_cookie_string(raw: str) -> dict:
        """
        Parse "key1=val1; key2=val2; key3=val3" format.
        Juga handle multi-line (newlines treated like ;).
        """
        out = {}
        # Normalize separators
        normalized = raw.replace('\n', ';').replace('\r', ';')
        for part in normalized.split(';'):
            part = part.strip()
            if not part or '=' not in part:
                continue
            key, _, value = part.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                # Kalau key ada duplikat (mis. msToken muncul 2x karena 2 domain),
                # PERTAMA yang menang. Sengaja: yang pertama biasanya domain
                # ".tiktok.com" yang lebih general, sesuai urutan natural Cookie header.
                if key not in out:
                    out[key] = value
        return out
    
    @staticmethod
    def _parse_tab_separated(raw: str) -> dict:
        """
        Parse format DevTools Application > Cookies (tab-separated):
          name<TAB>value<TAB>domain<TAB>path<TAB>expires<TAB>size<TAB>flags...
        
        atau cookie editor export. Kita ambil kolom name (0) dan value (1).
        Khusus untuk msToken yang muncul dua kali, kita prioritaskan domain
        '.tiktok.com' (dengan titik = parent domain, dipakai untuk semua subdomain)
        di atas 'www.tiktok.com' (host-only).
        """
        out = {}
        ms_tokens_seen = []  # [(value, domain), ...]
        
        for line in raw.splitlines():
            line = line.rstrip()
            if not line or line.startswith('#'):
                continue
            # Split by tab; minimal 2 kolom
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            
            name = parts[0].strip()
            value = parts[1].strip()
            domain = parts[2].strip() if len(parts) > 2 else ''
            
            if not name or not value:
                continue
            
            # Skip kalau value-nya cuma flag (✓, etc) — berarti name dan value tidak align
            if value in ('✓', 'TRUE', 'FALSE', 'Session', 'None'):
                continue
            
            # Special handling untuk msToken yang sering muncul 2x
            if name.lower() == 'mstoken':
                ms_tokens_seen.append((value, domain))
                continue
            
            # Kalau key sudah ada (dari row sebelumnya), skip — first wins
            if name not in out:
                out[name] = value
        
        # Pilih msToken yang domain-nya '.tiktok.com' (yang paling general)
        if ms_tokens_seen:
            # Prioritas: domain mulai dengan '.' (parent domain) > yang lain
            preferred = next(
                (v for v, d in ms_tokens_seen if d.startswith('.tiktok.com')),
                ms_tokens_seen[0][0]  # fallback: pertama yang ditemukan
            )
            out['msToken'] = preferred
        
        return out
    
    @staticmethod
    def _normalize_cookies(raw: dict) -> dict:
        """
        Normalize keys: handle alias (msToken -> ms_token), drop blacklisted
        (Akamai/analytics noise). SIMPAN SEMUA cookie lain — semakin lengkap
        cookies → semakin sedikit captcha dari TikTok anti-bot.
        """
        out = {}
        for key, value in raw.items():
            if value in (None, ''):
                continue
            if key in COOKIE_BLACKLIST:
                continue
            # Apply alias (case-insensitive lookup via lowercase)
            normalized_key = COOKIE_ALIASES.get(key, COOKIE_ALIASES.get(key.lower(), key))
            # First-wins kalau duplikat setelah alias (mis. msToken + ms_token)
            if normalized_key not in out:
                out[normalized_key] = str(value)
        return out
    
    @staticmethod
    def _extract_from_storage_state(state: dict) -> dict:
        """
        Convert Playwright storage_state {cookies: [{name, value, domain, ...}]}
        → flat dict {name: value}. Untuk msToken yang muncul di multi domain,
        prioritaskan parent domain '.tiktok.com' (yang lebih general dipakai
        TikTokApi).
        """
        out: dict = {}
        ms_token_candidates: list = []  # [(value, score), ...]
        
        for c in (state.get('cookies') or []):
            if not isinstance(c, dict):
                continue
            name = (c.get('name') or '').strip()
            value = c.get('value')
            if not name or value in (None, ''):
                continue
            if name in COOKIE_BLACKLIST:
                continue
            
            canonical = COOKIE_ALIASES.get(name, COOKIE_ALIASES.get(name.lower(), name))
            domain = c.get('domain', '') or ''
            
            if canonical == 'ms_token':
                # Skor: parent domain (".tiktok.com") menang dari host-only
                score = 2 if domain.startswith('.tiktok.com') else 1
                ms_token_candidates.append((str(value), score))
                continue
            
            if canonical not in out:
                out[canonical] = str(value)
        
        if ms_token_candidates:
            ms_token_candidates.sort(key=lambda x: -x[1])
            out['ms_token'] = ms_token_candidates[0][0]
        
        return out
    
    @staticmethod
    def _validate_ms_token(ms_token: str):
        """Quick sanity check; bukan login penuh, hanya verifikasi format."""
        if len(ms_token) < 50:
            raise Exception(f"ms_token terlalu pendek ({len(ms_token)} char), kemungkinan invalid")
        # ms_token biasanya base64-ish dengan + / = -; rough sanity
        if not re.match(r'^[A-Za-z0-9+/=_\-]+$', ms_token):
            raise Exception("ms_token mengandung karakter tidak biasa, mungkin salah copy")
    
    def get_cookies(self, username: str) -> dict:
        path = self._session_path(username)
        if not path.exists():
            raise Exception(f"No TikTok session for {username}")
        return json.loads(path.read_text())
