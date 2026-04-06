"""
Gunicorn configuration for the DAP PR Dashboard.

Workers
-------
Set ``WEB_CONCURRENCY`` to control the number of worker processes.
When Redis is configured (``REDIS_URL``), multiple workers safely share
state.  Without Redis the default is **1** worker (in-memory state is
not shared across processes).
"""

import os

# ---------------------------------------------------------------------------
# Workers & threads
# ---------------------------------------------------------------------------
_redis_url = os.environ.get("REDIS_URL", "").strip()
_default_workers = 4 if _redis_url else 1

workers = int(os.environ.get("WEB_CONCURRENCY", _default_workers))
threads = int(os.environ.get("GUNICORN_THREADS", 4))

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 300))

# ---------------------------------------------------------------------------
# Dev convenience
# ---------------------------------------------------------------------------
reload = os.environ.get("GUNICORN_RELOAD", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Logging (gunicorn's own access / error logs)
# ---------------------------------------------------------------------------
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
