"""
Slaytics Multi-Platform Scrapers
=================================
Modular scraper per platform. Setiap platform punya:
- *_scraper.py    : kelas Scraper, method scrape_*(keyword, amount)
- *_accounts.py   : AccountManager khusus platform (cookie/session format beda-beda)

Semua menerapkan interface dari base.py supaya app.py bisa pakai cara yang sama.
"""
