"""
TikTok Remote Proxy
====================
Drop-in replacement untuk TikTokScraper yang forward request ke
PC service (slaytics-tiktok-pc). Aktif kalau env TIKTOK_REMOTE_URL ada.

Fungsinya thin proxy:
  scrape_keyword(keyword, amount)  → POST {TIKTOK_REMOTE_URL}/scrape
  add_account(username, password)  → POST {TIKTOK_REMOTE_URL}/accounts
  list_accounts()                  → GET  {TIKTOK_REMOTE_URL}/accounts
  delete_account(username)         → DELETE {TIKTOK_REMOTE_URL}/accounts/tiktok/<u>
  set_status(username, 'active')   → POST {TIKTOK_REMOTE_URL}/accounts/tiktok/<u>/reactivate

Karena interface mirror persis BaseScraper + BaseAccountManager, app.py
nggak perlu tau ini lokal atau remote. Kalau env nggak diset, fallback
otomatis ke implementasi lokal lama.

Auth: kalau TIKTOK_REMOTE_KEY diset, dikirim sebagai
      Authorization: Bearer <key>.
"""

import logging
import os
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120  # PC scrape bisa makan waktu lumayan (Chromium boot ~5-15s)


def _build_headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = os.environ.get("TIKTOK_REMOTE_KEY", "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


class RemoteTikTokAccountManager:
    """Forward semua call ke PC service. Mimic BaseAccountManager interface."""

    PLATFORM = "tiktok"

    STATUS_ACTIVE = "active"
    STATUS_BANNED = "banned"
    STATUS_CHALLENGE = "challenge"
    STATUS_EXPIRED = "expired"
    STATUS_RATE_LIMITED = "rate_limited"

    def __init__(self, remote_url: str):
        self.remote_url = remote_url.rstrip("/")

    def add_account(
        self, username: str, password: str, verification_code: Optional[str] = None
    ) -> dict:
        resp = requests.post(
            f"{self.remote_url}/accounts",
            headers=_build_headers(),
            json={"platform": "tiktok", "username": username, "password": password},
            timeout=30,
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or resp.text
            except Exception:
                err = resp.text
            raise Exception(f"remote add_account failed: {err}")
        return resp.json()

    def list_accounts(self) -> List[dict]:
        try:
            resp = requests.get(
                f"{self.remote_url}/accounts",
                headers=_build_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("accounts", {}).get("tiktok", [])
        except Exception as e:
            logger.warning(f"[tiktok-remote] list_accounts failed: {e}")
            return []

    def list_active(self) -> List[str]:
        return [a["username"] for a in self.list_accounts() if a.get("status") == "active"]

    def delete_account(self, username: str) -> bool:
        try:
            resp = requests.delete(
                f"{self.remote_url}/accounts/tiktok/{username}",
                headers=_build_headers(),
                timeout=10,
            )
            return resp.json().get("deleted", False)
        except Exception as e:
            logger.warning(f"[tiktok-remote] delete_account failed: {e}")
            return False

    def set_status(self, username: str, status: str) -> bool:
        if status != "active":
            logger.warning(
                f"[tiktok-remote] set_status('{status}') tidak didukung remote, "
                f"hanya reactivate yang di-forward"
            )
            return False
        try:
            resp = requests.post(
                f"{self.remote_url}/accounts/tiktok/{username}/reactivate",
                headers=_build_headers(),
                timeout=10,
            )
            return resp.json().get("reactivated", False)
        except Exception as e:
            logger.warning(f"[tiktok-remote] reactivate failed: {e}")
            return False

    # Stub methods (dipanggil oleh kode lain tapi tidak perlu di remote case)
    def pick_next_active(self):
        active = self.list_active()
        return active[0] if active else None

    def mark_used(self, username):
        pass  # PC service handle sendiri

    def mark_error(self, username, error, status=None):
        pass  # PC service handle sendiri


class RemoteTikTokScraper:
    """Proxy ke /scrape PC service. Mimic BaseScraper interface."""

    PLATFORM = "tiktok"

    def __init__(self, account_manager: RemoteTikTokAccountManager):
        self.account_manager = account_manager
        self.remote_url = account_manager.remote_url
        self.last_used_account: Optional[str] = None

    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        resp = requests.post(
            f"{self.remote_url}/scrape",
            headers=_build_headers(),
            json={
                "platform": "tiktok",
                "keyword": keyword,
                "max_results": amount,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 500:
            try:
                err = resp.json().get("message") or resp.text
            except Exception:
                err = resp.text
            raise Exception(f"remote scrape failed (HTTP {resp.status_code}): {err}")

        data = resp.json()
        self.last_used_account = data.get("account_used")
        return data.get("posts", [])
