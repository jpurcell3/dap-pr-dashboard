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

# Workers auto-scale: 4 with Redis, 1 without.  Override via WEB_CONCURRENCY.
# gunicorn.conf.py handles all tunables (workers, threads, reload, timeout).
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
