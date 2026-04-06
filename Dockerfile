FROM python:3.13-slim

WORKDIR /app

# Install dependencies (cached layer unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application code
COPY . .

# Create persistent data directory for cache
RUN mkdir -p /data

EXPOSE 5000

ENV CACHE_PATH=/data/pr_cache.json
ENV LOG_TO_STDOUT=true

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health')" || exit 1

# Workers auto-scale: 4 with Redis, 1 without.  Override via WEB_CONCURRENCY.
# gunicorn.conf.py handles all tunables (workers, threads, reload, timeout).
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
