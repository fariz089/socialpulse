# Slaytics TikTok PC Service - Dockerfile
# Base image dari Microsoft sudah include Chromium + Playwright + Python deps
# Tag harus match dengan playwright Python package version di requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

# Install Python deps (Chromium browser sudah pre-installed di base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Volume mount point — accounts.json + sessions/ + scraper.log akan disimpan di sini
ENV DATA_DIR=/data
RUN mkdir -p /data/sessions

EXPOSE 5006

# Healthcheck biar docker compose tahu kalau service ready
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5006/health', timeout=5)" || exit 1

CMD ["python", "app.py"]
