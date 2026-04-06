"""
Redis-backed shared state for the DAP PR Dashboard.

When ``REDIS_URL`` is set in the environment the helpers in this module
store and retrieve application state from Redis so that multiple
processes / containers share a single source of truth.

When Redis is **not** configured every helper falls back to plain
in-memory Python dicts — exactly the behaviour the app had before this
module was introduced.  This means ``python app.py`` keeps working
locally without any extra infrastructure.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection (lazy, optional)
# ---------------------------------------------------------------------------
_redis_client = None
_redis_available = False

REDIS_URL = os.environ.get("REDIS_URL", "").strip()


def _get_redis():
    """Return a Redis client, or *None* if Redis is not configured."""
    global _redis_client, _redis_available
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        return None
    try:
        import redis as _redis_mod
        _redis_client = _redis_mod.Redis.from_url(
            REDIS_URL, decode_responses=True, socket_connect_timeout=5,
        )
        _redis_client.ping()
        _redis_available = True
        logger.info("Connected to Redis at %s", REDIS_URL)
        return _redis_client
    except Exception as exc:
        logger.warning("Redis unavailable (%s) — falling back to in-memory state", exc)
        _redis_available = False
        return None


def is_redis_active() -> bool:
    """Return *True* if Redis is connected and usable."""
    _get_redis()
    return _redis_available


# ---------------------------------------------------------------------------
# Key constants
# ---------------------------------------------------------------------------
_KEY_DATA_STORE = "dap:data_store"        # Redis hash – each field is a JSON blob
_KEY_REFRESH_STATUS = "dap:refresh_status" # Redis hash – flat string values
_KEY_RATE_LIMIT = "dap:rate_limit"         # Redis hash – flat string values


# ---------------------------------------------------------------------------
# Data store helpers  (_data_store equivalent)
# ---------------------------------------------------------------------------
# In-memory fallback — mirrors the old global dict in app.py.
_mem_data_store: dict = {
    "raw_prs": {},
    "repo_summaries": [],
    "pr_metrics": {},
    "bottlenecks": [],
    "loaded": False,
}


def data_store_get(key: str, default=None):
    """Read a single field from the data store."""
    r = _get_redis()
    if r is not None:
        try:
            val = r.hget(_KEY_DATA_STORE, key)
            if val is None:
                return default
            return json.loads(val)
        except Exception:
            logger.debug("Redis read failed for data_store[%s]", key, exc_info=True)
    return _mem_data_store.get(key, default)


def data_store_set(key: str, value) -> None:
    """Write a single field to the data store."""
    _mem_data_store[key] = value
    r = _get_redis()
    if r is not None:
        try:
            r.hset(_KEY_DATA_STORE, key, json.dumps(value, default=str))
        except Exception:
            logger.debug("Redis write failed for data_store[%s]", key, exc_info=True)


def data_store_update(mapping: dict) -> None:
    """Bulk-update multiple fields in the data store."""
    _mem_data_store.update(mapping)
    r = _get_redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            for k, v in mapping.items():
                pipe.hset(_KEY_DATA_STORE, k, json.dumps(v, default=str))
            pipe.execute()
        except Exception:
            logger.debug("Redis bulk-write failed for data_store", exc_info=True)


def data_store_snapshot() -> dict:
    """Return the full data store as a plain dict.

    Callers that need multiple fields in one shot should use this to
    avoid per-field round-trips to Redis.
    """
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hgetall(_KEY_DATA_STORE)
            if raw:
                return {k: json.loads(v) for k, v in raw.items()}
        except Exception:
            logger.debug("Redis snapshot read failed", exc_info=True)
    return dict(_mem_data_store)


def data_store_loaded() -> bool:
    """Convenience: check the 'loaded' flag."""
    return bool(data_store_get("loaded", False))


# ---------------------------------------------------------------------------
# Refresh status helpers  (_refresh_status equivalent)
# ---------------------------------------------------------------------------
_REFRESH_DEFAULTS: dict = {
    "running": False,
    "progress": "",
    "current_repo": "",
    "repos_done": 0,
    "repos_total": 0,
    "prs_fetched": 0,
    "started_at": None,
    "error": None,
    "scope": "all",
}

_mem_refresh_status: dict = dict(_REFRESH_DEFAULTS)


def _serialize_refresh(val):
    """Serialize a value for Redis hash storage (flat strings)."""
    if val is None:
        return "__none__"
    if isinstance(val, bool):
        return "1" if val else "0"
    return str(val)


def _deserialize_refresh(key: str, raw: str):
    """Deserialize a Redis hash value back to the expected Python type."""
    if raw == "__none__":
        return None
    default = _REFRESH_DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw == "1"
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return 0
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    if key == "started_at" and raw != "__none__":
        try:
            return float(raw)
        except ValueError:
            return None
    return raw


def refresh_status_get(key: str, default=None):
    """Read a single refresh-status field."""
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hget(_KEY_REFRESH_STATUS, key)
            if raw is not None:
                return _deserialize_refresh(key, raw)
        except Exception:
            logger.debug("Redis read failed for refresh_status[%s]", key, exc_info=True)
    return _mem_refresh_status.get(key, default)


def refresh_status_set(key: str, value) -> None:
    """Write a single refresh-status field."""
    _mem_refresh_status[key] = value
    r = _get_redis()
    if r is not None:
        try:
            r.hset(_KEY_REFRESH_STATUS, key, _serialize_refresh(value))
        except Exception:
            logger.debug("Redis write failed for refresh_status[%s]", key, exc_info=True)


def refresh_status_bulk_set(mapping: dict) -> None:
    """Bulk-update multiple refresh-status fields."""
    _mem_refresh_status.update(mapping)
    r = _get_redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            for k, v in mapping.items():
                pipe.hset(_KEY_REFRESH_STATUS, k, _serialize_refresh(v))
            pipe.execute()
        except Exception:
            logger.debug("Redis bulk-write failed for refresh_status", exc_info=True)


def refresh_status_snapshot() -> dict:
    """Return the full refresh status as a plain dict."""
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hgetall(_KEY_REFRESH_STATUS)
            if raw:
                return {k: _deserialize_refresh(k, v) for k, v in raw.items()}
        except Exception:
            logger.debug("Redis snapshot read failed for refresh_status", exc_info=True)
    return dict(_mem_refresh_status)


def refresh_status_reset() -> None:
    """Reset refresh status to defaults."""
    _mem_refresh_status.update(_REFRESH_DEFAULTS)
    r = _get_redis()
    if r is not None:
        try:
            r.delete(_KEY_REFRESH_STATUS)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Rate-limit helpers  (rate_limit_info equivalent)
# ---------------------------------------------------------------------------
_mem_rate_limit: dict = {
    "remaining": None,
    "limit": None,
    "used": None,
    "reset_at": None,
    "is_throttled": False,
    "throttled_until": None,
}


def rate_limit_get(key: str, default=None):
    """Read a single rate-limit field."""
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hget(_KEY_RATE_LIMIT, key)
            if raw is not None:
                if raw == "__none__":
                    return None
                if key == "is_throttled":
                    return raw == "1"
                if key in ("remaining", "limit", "used"):
                    try:
                        return int(raw)
                    except ValueError:
                        return None
                if key == "reset_at":
                    try:
                        return float(raw)
                    except ValueError:
                        return None
                return raw
        except Exception:
            logger.debug("Redis read failed for rate_limit[%s]", key, exc_info=True)
    return _mem_rate_limit.get(key, default)


def rate_limit_set(key: str, value) -> None:
    """Write a single rate-limit field."""
    _mem_rate_limit[key] = value
    r = _get_redis()
    if r is not None:
        try:
            r.hset(_KEY_RATE_LIMIT, key, _serialize_refresh(value))
        except Exception:
            logger.debug("Redis write failed for rate_limit[%s]", key, exc_info=True)


def rate_limit_snapshot() -> dict:
    """Return full rate-limit info as a plain dict."""
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hgetall(_KEY_RATE_LIMIT)
            if raw:
                result = {}
                for k, v in raw.items():
                    result[k] = rate_limit_get(k)
                return result
        except Exception:
            logger.debug("Redis snapshot read failed for rate_limit", exc_info=True)
    return dict(_mem_rate_limit)


def rate_limit_bulk_set(mapping: dict) -> None:
    """Bulk-update multiple rate-limit fields."""
    _mem_rate_limit.update(mapping)
    r = _get_redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            for k, v in mapping.items():
                pipe.hset(_KEY_RATE_LIMIT, k, _serialize_refresh(v))
            pipe.execute()
        except Exception:
            logger.debug("Redis bulk-write failed for rate_limit", exc_info=True)


# ---------------------------------------------------------------------------
# Distributed lock  (refresh coordination across workers)
# ---------------------------------------------------------------------------
import threading as _threading

_local_lock = _threading.Lock()       # fallback when Redis is unavailable
_KEY_REFRESH_LOCK = "dap:refresh_lock"
_LOCK_TTL_SECONDS = 600               # auto-expire if holder crashes


def acquire_refresh_lock(timeout: float = 0) -> bool:
    """Try to acquire the refresh lock.

    With Redis this uses ``SET NX EX`` so exactly one worker wins across
    all processes.  Without Redis it falls back to a threading lock
    (single-process only).

    Parameters
    ----------
    timeout : float
        Seconds to wait for the lock (0 = non-blocking).

    Returns True if the lock was acquired, False otherwise.
    """
    r = _get_redis()
    if r is not None:
        try:
            acquired = r.set(
                _KEY_REFRESH_LOCK, "1", nx=True, ex=_LOCK_TTL_SECONDS,
            )
            return bool(acquired)
        except Exception:
            logger.debug("Redis lock acquire failed", exc_info=True)
    # Fallback: threading lock
    return _local_lock.acquire(blocking=(timeout > 0), timeout=timeout or -1)


def release_refresh_lock() -> None:
    """Release the refresh lock."""
    r = _get_redis()
    if r is not None:
        try:
            r.delete(_KEY_REFRESH_LOCK)
            return
        except Exception:
            logger.debug("Redis lock release failed", exc_info=True)
    # Fallback
    try:
        _local_lock.release()
    except RuntimeError:
        pass  # wasn't held
