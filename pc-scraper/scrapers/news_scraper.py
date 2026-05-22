"""
Indonesian News Scraper
========================
Hybrid approach:
  1. Google News RSS — `https://news.google.com/rss/search?q=<keyword>&hl=id&gl=ID&ceid=ID:id`
     1 query dapat hasil dari semua sumber besar (Detik, Kompas, CNN Indonesia, dll).
  2. Per-site fallback — kalau Google News gagal/sedikit, scrape langsung dari
     search page tiap situs.

Tidak butuh akun login (semua sumber publik). Tetap rate-limited supaya sopan.

Sites yang di-cover di fallback (representative top news ID 2026):
  - detik.com
  - kompas.com
  - cnnindonesia.com
  - tempo.co
  - tribunnews.com
  - liputan6.com
  - kumparan.com
  - antaranews.com
  - okezone.com
  - republika.co.id
"""

import logging
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from .base import to_jsonable

logger = logging.getLogger(__name__)

UA = (
    'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
)


class NewsScraper:
    """
    Tidak inherit BaseScraper karena berita tidak butuh AccountManager.
    Tapi tetap expose scrape_keyword(keyword, amount) supaya app.py konsisten.
    """
    
    PLATFORM = "news"
    
    # Tiap site punya:
    #   - search: URL search results, {q} = keyword
    #   - item: CSS selector untuk satu hasil
    #   - url_pattern: regex; href harus match ini supaya dianggap artikel
    #     valid (bukan link nav/sidebar). Optional.
    SITES = {
        'detik.com':         {'search': 'https://www.detik.com/search/searchall?query={q}', 'item': 'article', 'url_pattern': r'/d-\d{7,}'},
        'kompas.com':        {'search': 'https://search.kompas.com/search/?q={q}',          'item': 'div.article__list'},
        'cnnindonesia.com':  {'search': 'https://www.cnnindonesia.com/search/?query={q}',   'item': 'article'},
        'tempo.co':          {'search': 'https://www.tempo.co/search?q={q}',                'item': 'article'},
        'tribunnews.com':    {'search': 'https://www.tribunnews.com/search?q={q}',          'item': 'li.ptb15'},
        'liputan6.com':      {'search': 'https://www.liputan6.com/search?q={q}',            'item': 'article'},
        'kumparan.com':      {'search': 'https://kumparan.com/search?q={q}',                'item': 'article'},
        'antaranews.com':    {'search': 'https://www.antaranews.com/search?q={q}',          'item': 'article'},
    }
    
    def __init__(self, account_manager=None):
        # account_manager unused tapi diterima supaya signature konsisten
        self.last_used_account = 'google-news'
    
    def scrape_keyword(self, keyword: str, amount: int = 30, sites: Optional[List[str]] = None) -> List[Dict]:
        """
        Cari berita by keyword. Strategy hybrid:
          1. Google News RSS — biasanya cukup
          2. Per-site fallback — kalau hasil < 50% dari amount yang diminta
        """
        articles = []
        seen_urls = set()
        
        # Step 1: Google News
        try:
            gn_articles = self._google_news(keyword, amount)
            for a in gn_articles:
                if a['url'] not in seen_urls:
                    seen_urls.add(a['url'])
                    articles.append(a)
            logger.info(f"[news] Google News: {len(gn_articles)} articles for '{keyword}'")
        except Exception as e:
            logger.warning(f"[news] Google News failed: {e}")
        
        # Step 2: Fallback per-site kalau hasilnya masih kurang
        if len(articles) < amount * 0.5:
            target_per_site = max(3, (amount - len(articles)) // 4)
            sites_to_try = sites or list(self.SITES.keys())[:5]  # 5 site teratas
            
            for site in sites_to_try:
                if len(articles) >= amount:
                    break
                if site not in self.SITES:
                    continue
                try:
                    site_articles = self._scrape_site(site, keyword, target_per_site)
                    for a in site_articles:
                        if a['url'] not in seen_urls:
                            seen_urls.add(a['url'])
                            articles.append(a)
                    logger.info(f"[news] {site}: {len(site_articles)} articles")
                    time.sleep(1)  # be nice
                except Exception as e:
                    logger.warning(f"[news] {site} failed: {e}")
        
        return articles[:amount]
    
    # ---- Google News RSS ----
    
    def _google_news(self, keyword: str, amount: int) -> List[Dict]:
        url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=id&gl=ID&ceid=ID:id"
        resp = requests.get(url, headers={'User-Agent': UA}, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"Google News HTTP {resp.status_code}")
        
        soup = BeautifulSoup(resp.text, 'xml')
        items = soup.find_all('item')[:amount]
        
        out = []
        for item in items:
            try:
                title = (item.title.text if item.title else '').strip()
                link = (item.link.text if item.link else '').strip()
                desc = (item.description.text if item.description else '').strip()
                pub_date = (item.pubDate.text if item.pubDate else '')
                source_tag = item.find('source')
                source = source_tag.text.strip() if source_tag else ''
                
                # Resolve Google News redirect URL ke URL asli (best-effort)
                # Google News links: https://news.google.com/rss/articles/CBM... -> redirect
                # Kita simpan as-is, frontend bisa fetch kalau perlu.
                
                # Parse pub date
                ts = int(time.time())
                try:
                    if pub_date:
                        dt = parsedate_to_datetime(pub_date)
                        ts = int(dt.timestamp())
                except Exception:
                    pass
                
                # Strip HTML dari description
                desc_text = BeautifulSoup(desc, 'html.parser').get_text(' ', strip=True) if desc else ''
                
                out.append(to_jsonable({
                    'platform': 'news',
                    'id': link,
                    'shortCode': None,
                    'ownerUsername': source,
                    'username': source,
                    'profilePicUrl': None,
                    'profile_pic_url': None,
                    'caption': title,
                    'text': desc_text or title,
                    'title': title,
                    'description': desc_text,
                    'source': source,
                    'likesCount': 0, 'like_count': 0,
                    'commentsCount': 0, 'comment_count': 0,
                    'videoViewCount': 0, 'video_view_count': 0,
                    'timestamp': ts,
                    'taken_at': ts,
                    'pub_date': pub_date,
                    'url': link,
                }))
            except Exception as e:
                logger.debug(f"[news] skip RSS item: {e}")
        return out
    
    # ---- Per-site fallback ----
    
    def _scrape_site(self, site: str, keyword: str, amount: int) -> List[Dict]:
        cfg = self.SITES.get(site)
        if not cfg:
            return []
        
        url = cfg['search'].format(q=quote(keyword))
        resp = requests.get(url, headers={'User-Agent': UA, 'Accept-Language': 'id,en;q=0.9'}, timeout=20)
        if resp.status_code != 200:
            raise Exception(f"{site} HTTP {resp.status_code}")
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = soup.select(cfg['item'])[:amount * 3]  # over-select, akan kita filter
        
        # Detik (dan banyak portal lain) punya tag <article> juga untuk layout
        # cards (sidebar vertikal: detikInet/Wolipop/detikJatim). Itu lolos
        # selector tapi judulnya cuma nama kanal — bukan artikel. Kita reject.
        CHANNEL_NAMES = {
            'detikinet', 'detikjatim', 'detiknews', 'detikoto', 'detikfinance',
            'detikhot', 'detiksport', 'detikfood', 'detikhealth', 'detiktravel',
            'wolipop', 'sepakbola', 'inet', '20detik', 'detikedu', 'detikjabar',
            'detikjateng', 'detikbali', 'detiksumut', 'detiksulsel',
        }
        url_pat = re.compile(cfg['url_pattern']) if cfg.get('url_pattern') else None
        
        out = []
        for it in items:
            if len(out) >= amount:
                break
            try:
                # Cari link utama
                link_el = it.find('a', href=True)
                if not link_el:
                    continue
                href = link_el['href']
                if not href.startswith('http'):
                    href = f"https://www.{site}{href}"
                
                # Reject link nav/kanal: kalau site punya url_pattern, harus match
                if url_pat and not url_pat.search(href):
                    continue
                
                # Title: prefer headline tag, fallback ke anchor text
                h = it.find(['h1', 'h2', 'h3', 'h4'])
                title = h.get_text(' ', strip=True) if h else link_el.get_text(' ', strip=True)
                
                # Reject "judul" yang cuma nama kanal
                if not title or title.lower().strip() in CHANNEL_NAMES:
                    continue
                # Reject judul super pendek (biasanya nav/badge, bukan headline)
                if len(title) < 15:
                    continue
                
                # Snippet
                p = it.find('p')
                snippet = p.get_text(' ', strip=True) if p else ''
                
                # Timestamp — fallback per-site TIDAK parse tanggal artikel.
                # Jangan pakai time.time() (bikin semua item kelihatan "barusan").
                # Set None supaya frontend bisa display "tanggal tidak diketahui"
                # daripada misleading.
                ts = None
                
                out.append(to_jsonable({
                    'platform': 'news',
                    'id': href,
                    'shortCode': None,
                    'ownerUsername': site,
                    'username': site,
                    'profilePicUrl': None,
                    'profile_pic_url': None,
                    'caption': title,
                    'text': snippet or title,
                    'title': title,
                    'description': snippet,
                    'source': site,
                    'likesCount': 0, 'like_count': 0,
                    'commentsCount': 0, 'comment_count': 0,
                    'videoViewCount': 0, 'video_view_count': 0,
                    'timestamp': ts,
                    'taken_at': ts,
                    'url': href,
                }))
            except Exception as e:
                logger.debug(f"[news] skip {site} item: {e}")
        return out