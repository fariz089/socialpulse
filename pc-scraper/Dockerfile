# pc-scraper/Dockerfile
# Wrapper untuk multi-platform scraper supaya jalan di PC dalam Docker.
# Sekarang include Playwright + Chromium untuk Facebook (FB jadi SPA, gak bisa
# di-scrape dengan requests+BS4 plain).
FROM python:3.11-slim

# Dependencies untuk lxml DAN Playwright Chromium runtime.
# Chromium butuh banyak shared libs untuk render headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Build deps untuk wheels native (lxml)
        gcc \
        libxml2-dev \
        libxslt-dev \
        # Common
        ca-certificates \
        curl \
        wget \
        gnupg \
        # Playwright Chromium runtime deps
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libatspi2.0-0 \
        # Fonts (kalau ada Asian text di scrape)
        fonts-liberation \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps dulu supaya layer ini ke-cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser via Playwright. --with-deps redundant karena sudah di-install
# di atas, tapi --with-deps di sini akan complain kalau ada yg kurang.
RUN playwright install chromium

# Copy source apa adanya
COPY . .

# Persistent data (accounts.json + sessions/) di /data, supaya bisa di-mount volume
ENV DATA_DIR=/data
ENV ACCOUNTS_FILE=/data/accounts.json
ENV SESSIONS_DIR=/data/sessions
ENV LOG_FILE=/data/scraper.log
RUN mkdir -p /data/sessions

EXPOSE 5005

# Healthcheck supaya docker compose tahu service ready.
# Note: start-period dinaikin ke 30s karena Chromium first-launch lebih lambat.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5005/health', timeout=5)" || exit 1

CMD ["python", "app.py"]