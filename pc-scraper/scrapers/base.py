"""
Base interfaces untuk semua platform scraper.
================================================

BaseAccountManager  : pool akun dengan status tracking, round-robin rotation,
                      session/cookie persistence. Tiap platform inherit dan
                      override _do_login() + _save_session() + _load_client().

BaseScraper         : interface scrape_keyword(keyword, amount) -> List[Dict].
                      Setiap platform punya field output yang sedikit beda,
                      tapi semua return list-of-dict dengan field umum:
                      id, url, username, text/caption, likes, comments, views,
                      timestamp, profile_pic, platform.
"""

import json
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def to_jsonable(value: Any) -> Any:
    """
    Convert nilai apa pun (pydantic HttpUrl, datetime, Enum, BaseModel, dll)
    jadi tipe yang aman buat Flask jsonify. Mirror dari yang ada di scraper
    Instagram lama, tapi sekarang dipakai bersama.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return str(value)


class BaseAccountManager(ABC):
    """
    Pool akun per platform. Disimpan di accounts.json dengan struktur:
    {
      "<platform>": {
        "<username>": { username, password, status, last_used, last_error, use_count, ... }
      }
    }
    
    Session/cookie file disimpan di sessions/<platform>/<username>.json.
    Tiap platform format-nya beda (instagrapi pakai .json settings, TikTok pakai
    ms_token + cookies, Facebook pakai c_user/xs/datr, YouTube pakai Netscape
    cookies.txt). Subclass yang handle.
    """
    
    PLATFORM = "base"  # override di subclass
    
    STATUS_ACTIVE = 'active'
    STATUS_BANNED = 'banned'
    STATUS_CHALLENGE = 'challenge'
    STATUS_RATE_LIMITED = 'rate_limited'
    STATUS_BAD_PASSWORD = 'bad_password'
    STATUS_EXPIRED = 'expired'
    
    def __init__(self, accounts_file: Path, sessions_dir: Path):
        self.accounts_file = accounts_file
        self.sessions_dir = sessions_dir / self.PLATFORM
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._all_accounts: Dict[str, Dict[str, dict]] = {}  # platform -> username -> info
        self._load()
    
    def _load(self):
        if self.accounts_file.exists():
            try:
                data = json.loads(self.accounts_file.read_text())
                # Backward-compat: kalau file lama (cuma dict username->info), assume Instagram
                if data and isinstance(next(iter(data.values()), None), dict) and 'status' in next(iter(data.values())):
                    # legacy flat format
                    self._all_accounts = {'instagram': data}
                    logger.info(f"Migrated legacy flat accounts.json to nested format ({len(data)} IG accounts)")
                else:
                    self._all_accounts = data
            except Exception as e:
                logger.error(f"Failed to load accounts: {e}")
                self._all_accounts = {}
        else:
            self._all_accounts = {}
        
        if self.PLATFORM not in self._all_accounts:
            self._all_accounts[self.PLATFORM] = {}
    
    def _save(self):
        try:
            self.accounts_file.write_text(json.dumps(self._all_accounts, indent=2))
        except Exception as e:
            logger.error(f"Failed to save accounts: {e}")
    
    @property
    def _accounts(self) -> Dict[str, dict]:
        """Akun untuk platform ini saja."""
        return self._all_accounts.setdefault(self.PLATFORM, {})
    
    def _session_path(self, username: str) -> Path:
        return self.sessions_dir / f"{username}.json"
    
    # ---- Override di subclass ----
    
    @abstractmethod
    def _do_login(self, username: str, password: str, verification_code: Optional[str] = None) -> dict:
        """
        Login ke platform & save session ke self._session_path(username).
        Return dict tambahan yang mau disimpan di info akun (mis. user_id, dll).
        Raise Exception kalau gagal.
        """
        ...
    
    # ---- Public API (sama untuk semua platform) ----
    
    def add_account(self, username: str, password: str, verification_code: Optional[str] = None) -> dict:
        with self._lock:
            try:
                extra = self._do_login(username, password, verification_code) or {}
                self._accounts[username] = {
                    'username': username,
                    'password': password,
                    'status': self.STATUS_ACTIVE,
                    'added_at': self._accounts.get(username, {}).get('added_at', datetime.utcnow().isoformat()),
                    'last_used': None,
                    'last_error': None,
                    'use_count': 0,
                    'platform': self.PLATFORM,
                    **extra,
                }
                self._save()
                logger.info(f"[{self.PLATFORM}] Logged in {username}")
                return {'username': username, 'platform': self.PLATFORM, 'status': self.STATUS_ACTIVE}
            except Exception as e:
                # Tetap save kalau gagal challenge (supaya bisa di-resolve manual)
                err_str = str(e).lower()
                if 'challenge' in err_str or 'checkpoint' in err_str:
                    self._accounts[username] = {
                        'username': username, 'password': password,
                        'status': self.STATUS_CHALLENGE,
                        'added_at': datetime.utcnow().isoformat(),
                        'last_error': str(e)[:200],
                        'last_used': None, 'use_count': 0,
                        'platform': self.PLATFORM,
                    }
                    self._save()
                raise
    
    def pick_next_active(self) -> Optional[str]:
        with self._lock:
            active = [(u, info) for u, info in self._accounts.items()
                      if info.get('status') == self.STATUS_ACTIVE]
            if not active:
                return None
            active.sort(key=lambda kv: kv[1].get('last_used') or '0')
            return active[0][0]
    
    def mark_used(self, username: str):
        with self._lock:
            if username in self._accounts:
                self._accounts[username]['last_used'] = datetime.utcnow().isoformat()
                self._accounts[username]['use_count'] = self._accounts[username].get('use_count', 0) + 1
                self._save()
    
    def mark_error(self, username: str, error: Exception, status: Optional[str] = None):
        """
        Generic error marker. Subclass boleh override untuk auto-detect status
        dari exception type platform-spesifik.
        """
        with self._lock:
            if username not in self._accounts:
                return
            self._accounts[username]['last_error'] = str(error)[:200]
            self._accounts[username]['error_at'] = datetime.utcnow().isoformat()
            if status:
                self._accounts[username]['status'] = status
                logger.warning(f"[{self.PLATFORM}] {username} marked as {status}: {str(error)[:100]}")
            self._save()
    
    def set_status(self, username: str, status: str) -> bool:
        with self._lock:
            if username not in self._accounts:
                return False
            self._accounts[username]['status'] = status
            self._save()
            return True
    
    def delete_account(self, username: str) -> bool:
        with self._lock:
            if username not in self._accounts:
                return False
            del self._accounts[username]
            session_path = self._session_path(username)
            if session_path.exists():
                session_path.unlink()
            self._save()
            return True
    
    def list_accounts(self) -> List[dict]:
        with self._lock:
            # Filter sensitive fields. password (semua platform) dan totp_secret (twitter)
            # nggak boleh muncul di response GET /accounts.
            SENSITIVE = {'password', 'totp_secret'}
            return [
                {k: v for k, v in info.items() if k not in SENSITIVE}
                for info in self._accounts.values()
            ]
    
    def list_active(self) -> List[str]:
        with self._lock:
            return [u for u, info in self._accounts.items()
                    if info.get('status') == self.STATUS_ACTIVE]


class BaseScraper(ABC):
    """
    Interface umum: scrape_keyword(keyword, amount) -> List[Dict].
    Setiap dict harus punya minimal: id, url, platform, timestamp.
    """
    
    PLATFORM = "base"
    
    def __init__(self, account_manager: BaseAccountManager):
        self.account_manager = account_manager
        self.last_used_account: Optional[str] = None
    
    @abstractmethod
    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        ...
