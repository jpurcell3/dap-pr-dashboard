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

# Single worker + threads: keeps in-memory state consistent within one process.
# --reload watches for file changes (useful with bind mounts in dev).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "300", "--reload", "app:app"]
