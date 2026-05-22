"""
Slaytics TikTok Service — PC/VPS edition
==========================================
Service Flask kecil yang dijalankan di PC/VPS, expose endpoint yang
shape-nya identik dengan android-scraper supaya bisa di-proxy transparan.

Endpoints:
  POST /scrape    — body {platform:"tiktok", keyword, max_results}
  POST /accounts  — body {platform:"tiktok", username, password}  (cookie store)
  GET  /accounts  — list akun
  GET  /health    — status
  DELETE /accounts/tiktok/<username>
  POST /accounts/tiktok/<username>/reactivate

Dia pakai TikTokApi (David Teather) yang internally launch Chromium via
Playwright untuk handle signature TikTok web. Itu sebabnya dia harus jalan
di PC/VPS, bukan di Termux.

Auth (optional):
  Set env SCRAPER_API_KEY=... → semua request wajib kirim
  header `Authorization: Bearer <key>`. Recommended kalau service
  expose ke internet.
"""

import asyncio
import json
import logging
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, jsonify, request

from tiktok_remote import TikTokRemoteScraper, TikTokAccountStore

# ──────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# Path-path bisa di-override via env var supaya container bisa mount
# data/ ke volume Docker. Default: tetap di folder app (untuk run lokal).
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
ACCOUNTS_FILE = Path(os.environ.get("ACCOUNTS_FILE", str(DATA_DIR / "accounts.json")))
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", str(DATA_DIR / "sessions")))
LOG_FILE = Path(os.environ.get("LOG_FILE", str(DATA_DIR / "scraper.log")))

DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

account_store = TikTokAccountStore(ACCOUNTS_FILE, SESSIONS_DIR)
scraper = TikTokRemoteScraper(account_store)

# Rate limit
_last_call = 0.0
_lock = threading.Lock()
DELAY_MIN, DELAY_MAX = 5, 10

app = Flask(__name__)


# ──────────────────────────────────────────────────
# Auth middleware
# ──────────────────────────────────────────────────
@app.before_request
def _check_auth():
    if not API_KEY:
        return None
    if request.path == "/health":
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing_bearer_token"}), 401
    if auth.split(" ", 1)[1].strip() != API_KEY:
        return jsonify({"error": "invalid_token"}), 401
    return None


# ──────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "service": "Slaytics TikTok PC Service",
            "version": "1.0",
            "platforms": ["tiktok"],
            "auth_required": bool(API_KEY),
            "endpoints": [
                "GET  /health",
                "POST /scrape   {platform:'tiktok', keyword, max_results}",
                "POST /accounts {platform:'tiktok', username, password}",
                "GET  /accounts",
                "DELETE /accounts/tiktok/<username>",
                "POST /accounts/tiktok/<username>/reactivate",
            ],
        }
    )


@app.route("/health", methods=["GET"])
def health():
    accounts = account_store.list_accounts()
    active = [a for a in accounts if a["status"] == "active"]
    return jsonify(
        {
            "status": "ok",
            "service": "slaytics-tiktok-pc",
            "version": "1.0",
            "platforms": {
                "tiktok": {
                    "total": len(accounts),
                    "active": len(active),
                    "banned": len([a for a in accounts if a["status"] == "banned"]),
                    "challenged": len([a for a in accounts if a["status"] == "challenge"]),
                    "expired": len([a for a in accounts if a["status"] == "expired"]),
                    "next_account": active[0]["username"] if active else None,
                }
            },
            "uptime_check": datetime.utcnow().isoformat() + "Z",
        }
    )


@app.route("/scrape", methods=["POST"])
def scrape_endpoint():
    global _last_call
    payload = request.get_json(silent=True) or {}
    platform = (payload.get("platform") or "tiktok").lower()
    keyword = (payload.get("keyword") or "").strip().lstrip("#")
    max_results = int(payload.get("max_results") or 30)

    if platform != "tiktok":
        return (
            jsonify({"error": f"this service only handles platform=tiktok, got {platform}"}),
            400,
        )

    if not keyword:
        return jsonify({"error": "keyword required"}), 400

    if not account_store.list_active():
        return (
            jsonify(
                {
                    "error": "no_active_accounts",
                    "platform": "tiktok",
                    "message": "Tidak ada akun TikTok aktif. POST /accounts dulu.",
                    "posts": [],
                }
            ),
            503,
        )

    # Rate limit
    with _lock:
        now = time.time()
        elapsed = now - _last_call
        if elapsed < DELAY_MIN:
            sleep_for = DELAY_MIN - elapsed + random.uniform(0, DELAY_MAX - DELAY_MIN)
            logger.info(f"[tiktok] Rate limit: sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
        _last_call = time.time()

    try:
        posts = asyncio.run(scraper.scrape_keyword(keyword, amount=max_results))
        return jsonify(
            {
                "platform": "tiktok",
                "keyword": keyword,
                "mode": "posts",
                "posts": posts,
                "count": len(posts),
                "source": "tiktok-pc-service",
                "account_used": scraper.last_used_account,
            }
        )
    except Exception as e:
        logger.error(f"[tiktok] scrape failed for '{keyword}': {e}", exc_info=True)
        return (
            jsonify(
                {
                    "error": "scrape_failed",
                    "platform": "tiktok",
                    "message": str(e)[:300],
                    "posts": [],
                }
            ),
            502,
        )


@app.route("/accounts", methods=["POST"])
def add_account():
    payload = request.get_json(silent=True) or {}
    platform = (payload.get("platform") or "tiktok").lower()
    username = (payload.get("username") or "").strip()
    password = payload.get("password", "")

    if platform != "tiktok":
        return jsonify({"error": "platform must be tiktok"}), 400
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    try:
        result = account_store.add_account(username, password)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[tiktok] add account {username} failed: {e}")
        return jsonify({"error": str(e), "platform": "tiktok", "username": username}), 400


@app.route("/accounts", methods=["GET"])
def list_accounts():
    return jsonify({"accounts": {"tiktok": account_store.list_accounts()}})


@app.route("/accounts/tiktok/<username>", methods=["DELETE"])
def delete_account(username):
    ok = account_store.delete_account(username)
    return jsonify({"deleted": ok, "platform": "tiktok", "username": username})


@app.route("/accounts/tiktok/<username>/reactivate", methods=["POST"])
def reactivate_account(username):
    ok = account_store.set_status(username, "active")
    return jsonify({"reactivated": ok, "platform": "tiktok", "username": username})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5006))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"Starting Slaytics TikTok PC Service on {host}:{port}")
    logger.info(f"  auth_required: {bool(API_KEY)}")
    logger.info(f"  accounts loaded: {len(account_store.list_accounts())}")
    app.run(host=host, port=port, threaded=True)
