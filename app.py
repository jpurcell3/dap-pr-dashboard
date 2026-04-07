"""
Flask web server for the DAP PR Dashboard.

Serves a dashboard UI and exposes JSON APIs for PR metrics,
bottleneck detection, and repository summaries sourced from
GitHub / GitHub Enterprise.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, request

from github_collector import (
    fetch_all_data, get_fusion_repos, get_all_org_repos, load_cache, save_cache,
    cancel_refresh, RefreshCancelled,
    GITHUB_API_BASE, ORG_NAME, REPO_PREFIX,
    DEFAULT_PR_LOOKBACK_DAYS, MAX_PRS_PER_REPO,
)
from metrics import compute_all_metrics, compute_pr_metrics, detect_bottlenecks
from redis_state import (
    data_store_get, data_store_set, data_store_update,
    data_store_loaded, data_store_snapshot,
    refresh_status_get, refresh_status_set, refresh_status_bulk_set,
    refresh_status_snapshot, refresh_status_reset,
    rate_limit_get, rate_limit_snapshot,
    acquire_refresh_lock, release_refresh_lock,
    is_redis_active,
)

# ---------------------------------------------------------------------------
# Derive the GitHub *web* URL from the API URL so the frontend can build
# clickable PR links.  GHE API URLs look like either:
#   https://HOSTNAME/api/v3   or   https://api.HOSTNAME
# For public GitHub the web URL is simply https://github.com.
# Users can override by setting GITHUB_WEB_URL in the environment.
# ---------------------------------------------------------------------------
def _derive_github_web_url(api_base: str) -> str:
    explicit = os.environ.get("GITHUB_WEB_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    parsed = urlparse(api_base)
    host = parsed.hostname or ""
    scheme = parsed.scheme or "https"
    # https://api.github.com  ->  https://github.com
    # https://api.ghe.corp    ->  https://ghe.corp
    if host.startswith("api."):
        return f"{scheme}://{host[4:]}"
    # https://ghe.corp/api/v3  ->  https://ghe.corp
    return f"{scheme}://{host}"

GITHUB_WEB_URL = _derive_github_web_url(GITHUB_API_BASE)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(__file__), "server.log")
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if os.environ.get("LOG_TO_STDOUT", "").lower() not in ("1", "true", "yes"):
    _log_handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

CACHE_PATH = os.environ.get(
    "CACHE_PATH",
    os.path.join(os.path.dirname(__file__), "pr_cache.json"),
)

# Auto-refresh interval in minutes.  0 (default) = disabled.
AUTO_REFRESH_INTERVAL_MINUTES = int(
    os.environ.get("AUTO_REFRESH_INTERVAL_MINUTES", "0")
)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_cache_into_store() -> bool:
    """Load cached data into the shared state store.

    Priority order:
      1. Redis — if connected and already populated, use it (no file I/O).
      2. File  — load ``pr_cache.json`` and push into Redis + in-memory.

    Returns True if data was loaded from any source, False otherwise.
    """
    # 1. If Redis already has data (e.g. from a previous container lifetime
    #    or another worker), skip the file entirely.
    if is_redis_active() and data_store_loaded():
        logger.info("Cache already present in Redis — skipping file load.")
        return True

    # 2. Fall back to the JSON file cache.
    if not os.path.exists(CACHE_PATH):
        logger.info("No cache file found at %s", CACHE_PATH)
        return False

    try:
        cached = load_cache(CACHE_PATH)
        if not cached:
            return False

        data_store_update({
            "raw_prs": cached.get("raw_prs", {}),
            "repo_summaries": cached.get("repo_summaries", []),
            "pr_metrics": cached.get("pr_metrics", {}),
            "bottlenecks": cached.get("bottlenecks", []),
            "loaded": True,
        })
        logger.info("Cache loaded from file %s (written to Redis too).", CACHE_PATH)
        return True
    except Exception:
        logger.exception("Failed to load cache from %s", CACHE_PATH)
        return False


def _build_overview(repo_summaries: list) -> dict:
    """Derive top-level overview statistics from the list of repo summaries."""
    total_repos = len(repo_summaries)
    total_prs = sum(s.get("total_prs", 0) for s in repo_summaries)
    total_merged = sum(s.get("merged_prs", 0) for s in repo_summaries)
    total_open = sum(s.get("open_prs", 0) for s in repo_summaries)

    cycle_times = [
        s["avg_cycle_time_hours"]
        for s in repo_summaries
        if s.get("avg_cycle_time_hours") is not None
    ]
    avg_cycle_time_hours = (
        round(sum(cycle_times) / len(cycle_times), 2) if cycle_times else 0.0
    )

    # Repos ranked by number of bottleneck flags (descending).
    repos_with_most_bottlenecks = sorted(
        [
            {"repo": s.get("repo"), "bottleneck_count": s.get("bottleneck_count", 0)}
            for s in repo_summaries
            if s.get("bottleneck_count", 0) > 0
        ],
        key=lambda x: x["bottleneck_count"],
        reverse=True,
    )

    return {
        "total_repos": total_repos,
        "total_prs": total_prs,
        "total_merged": total_merged,
        "total_open": total_open,
        "avg_cycle_time_hours": avg_cycle_time_hours,
        "repos_with_most_bottlenecks": repos_with_most_bottlenecks,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# 1. Dashboard page -----------------------------------------------------------
@app.route("/")
def index():
    """Serve the main dashboard HTML page."""
    return render_template("index.html")


# 1b. Config info -------------------------------------------------------------
@app.route("/api/config")
def api_config():
    """Return the current configuration (non-sensitive)."""
    return jsonify({
        "github_api_url": GITHUB_API_BASE,
        "github_web_url": GITHUB_WEB_URL,
        "github_org": ORG_NAME,
        "repo_prefix": REPO_PREFIX,
        "default_lookback_days": DEFAULT_PR_LOOKBACK_DAYS,
        "max_prs_per_repo": MAX_PRS_PER_REPO,
        "env_file": str(os.path.join(os.path.dirname(__file__), ".env")),
    })


# 2. List repos ---------------------------------------------------------------
@app.route("/api/repos")
def api_repos():
    """Return repos with priority metadata for the repo picker.

    By default only repos that have PR data are returned (simple string
    list for backward compatibility).

    Query parameters
    ----------------
    all : bool
        ``true`` returns **every** repo in the org as objects with
        priority metadata: ``{name, priority, has_data, pr_count,
        prefix_match}``.  The list is sorted by descending priority.
    """
    try:
        show_all = request.args.get("all", "").lower() in ("true", "1", "yes")

        if not show_all:
            # Simple mode: only repos with data (string list)
            repos = get_fusion_repos()
            if data_store_loaded():
                raw_prs = data_store_get("raw_prs", {})
                repos_with_data = {
                    name for name, prs in raw_prs.items()
                    if prs
                }
                repos = [r for r in repos if r in repos_with_data]
            return jsonify(repos)

        # Full mode: all org repos with priority scoring
        all_names = get_all_org_repos()
        raw_prs = data_store_get("raw_prs", {}) if data_store_loaded() else {}

        scored: list[dict] = []
        for name in all_names:
            prs = raw_prs.get(name, [])
            pr_count = len(prs) if prs else 0
            has_data = pr_count > 0
            prefix_match = name.lower().startswith(REPO_PREFIX) if REPO_PREFIX else False

            # Priority: repos with data first, then prefix matches, then alphabetical
            priority = (2 if has_data else 0) + (1 if prefix_match else 0)

            scored.append({
                "name": name,
                "priority": priority,
                "has_data": has_data,
                "pr_count": pr_count,
                "prefix_match": prefix_match,
            })

        scored.sort(key=lambda r: (-r["priority"], r["name"]))
        return jsonify(scored)

    except Exception as exc:
        logger.exception("Error fetching repo list")
        return jsonify({"error": str(exc)}), 500


# 3. Full refresh (background) -------------------------------------------------
def _do_refresh(repos=None, since=None, until=None):
    """Run the data refresh in a background thread.

    Parameters
    ----------
    repos : list[str] | None
        Explicit list of repo names to refresh.  When ``None`` all
        fusion-* repos are discovered and refreshed.
    since : str | None
        ISO date string (e.g. ``"2024-01-01"``).  Only PRs created on or
        after this date will be fetched.
    until : str | None
        ISO date string (e.g. ``"2024-12-31"``).  PRs created *after* this
        date will be filtered out after fetching (applied before metrics).
    """
    try:
        is_partial = repos is not None
        scope_label = ", ".join(repos) if is_partial else "all repos"

        refresh_status_bulk_set({
            "running": True,
            "error": None,
            "started_at": time.time(),
            "scope": scope_label,
        })

        if is_partial:
            refresh_status_bulk_set({
                "repos_total": len(repos),
                "progress": f"Fetching PRs for {scope_label}...",
            })
            logger.info("Refresh (partial): repos=%s, since=%s, until=%s", repos, since, until)
        else:
            refresh_status_set("progress", "Discovering repos...")
            repos = get_fusion_repos()
            refresh_status_bulk_set({
                "repos_total": len(repos),
                "progress": f"Found {len(repos)} repos. Fetching PRs...",
            })
            logger.info("Refresh: found %d fusion repos", len(repos))

        def progress_cb(repo_name, current, total):
            refresh_status_bulk_set({
                "current_repo": repo_name,
                "repos_done": current - 1,
                "progress": f"Fetching {repo_name} ({current}/{total})...",
            })
            logger.info("Refresh progress: %s (%d/%d)", repo_name, current, total)

        raw_prs = fetch_all_data(repos, progress_callback=progress_cb, since=since)

        # Apply "until" filter: remove PRs created after the until date
        if until:
            until_date = until[:10]  # normalise to "YYYY-MM-DD"
            for repo_name in list(raw_prs.keys()):
                raw_prs[repo_name] = [
                    pr for pr in raw_prs[repo_name]
                    if pr.get("created_at", "")[:10] <= until_date
                ]

        total_prs = sum(len(v) for v in raw_prs.values())
        refresh_status_bulk_set({
            "prs_fetched": total_prs,
            "repos_done": len(repos),
            "progress": "Computing metrics...",
        })
        logger.info("Refresh: fetched %d total PRs, computing metrics...", total_prs)

        if is_partial:
            # --- Partial refresh: incremental metric update ----------------
            # Only recompute metrics for the repos that were actually
            # refreshed, then merge into the existing store.  This avoids
            # re-running compute_all_metrics over every repo.
            changed_repos = set(raw_prs.keys())

            # Compute metrics only for changed repos
            changed_metrics = compute_all_metrics(raw_prs)

            # Merge raw_prs
            merged_raw = dict(data_store_get("raw_prs", {}))
            merged_raw.update(raw_prs)

            # Merge pr_metrics: replace changed repos, keep the rest
            merged_pr_metrics = dict(data_store_get("pr_metrics", {}))
            merged_pr_metrics.update(changed_metrics.get("pr_metrics", {}))

            # Merge repo_summaries: drop old entries for changed repos, add new
            repo_summaries = [
                s for s in data_store_get("repo_summaries", [])
                if s.get("repo") not in changed_repos
            ]
            repo_summaries.extend(changed_metrics.get("repo_summaries", []))

            # Merge bottlenecks: drop old entries for changed repos, add new
            bottlenecks = [
                b for b in data_store_get("bottlenecks", [])
                if b.get("repo") not in changed_repos
            ]
            bottlenecks.extend(changed_metrics.get("bottlenecks", []))

            refresh_status_set("progress", "Saving cache...")
            cache_payload = {
                "raw_prs": merged_raw,
                "repo_summaries": repo_summaries,
                "pr_metrics": merged_pr_metrics,
                "bottlenecks": bottlenecks,
            }
            save_cache(cache_payload, CACHE_PATH)

            data_store_update(cache_payload)
            data_store_set("loaded", True)
        else:
            # --- Full refresh: replace everything --------------------------
            all_metrics = compute_all_metrics(raw_prs)
            repo_summaries = all_metrics.get("repo_summaries", [])
            pr_metrics = all_metrics.get("pr_metrics", {})
            bottlenecks = all_metrics.get("bottlenecks", [])

            refresh_status_set("progress", "Saving cache...")
            cache_payload = {
                "raw_prs": raw_prs,
                "repo_summaries": repo_summaries,
                "pr_metrics": pr_metrics,
                "bottlenecks": bottlenecks,
            }
            save_cache(cache_payload, CACHE_PATH)

            data_store_update(cache_payload)
            data_store_set("loaded", True)

        refresh_status_bulk_set({"progress": "Complete!", "running": False})
        logger.info("Refresh complete: %d repos, %d PRs", len(repos), total_prs)

    except RefreshCancelled:
        logger.info("Refresh cancelled by user.")
        refresh_status_bulk_set({
            "running": False,
            "progress": "Cancelled.",
            "error": None,
        })
    except Exception as exc:
        logger.exception("Refresh failed")
        refresh_status_bulk_set({
            "error": str(exc),
            "running": False,
            "progress": f"Error: {exc}",
        })
    finally:
        release_refresh_lock()


@app.route("/api/refresh")
def api_refresh():
    """Start a background data refresh from GitHub.

    Query parameters
    ----------------
    repo : str, optional
        Comma-separated list of repo names to refresh.  When omitted all
        fusion-* repos are refreshed.
    since : str, optional
        ISO date (``YYYY-MM-DD``).  Only fetch PRs created on or after this
        date.  Defaults to ``DEFAULT_PR_LOOKBACK_DAYS`` days ago (90 by
        default).  Pass ``since=all`` to fetch all PRs with no date filter.
    until : str, optional
        ISO date (``YYYY-MM-DD``).  Exclude PRs created after this date
        (applied after fetch, before metric computation).
    """
    if refresh_status_get("running") or not acquire_refresh_lock():
        return jsonify({"status": "already_running", **refresh_status_snapshot()})

    repo_param = request.args.get("repo")
    since = request.args.get("since")   # e.g. "2024-01-01"
    until = request.args.get("until")   # e.g. "2024-12-31"

    # Apply default lookback when no since is specified
    if since is None and DEFAULT_PR_LOOKBACK_DAYS > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_PR_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        logger.info("No 'since' specified — defaulting to %d-day lookback: %s", DEFAULT_PR_LOOKBACK_DAYS, since)
    elif since and since.lower() == "all":
        since = None  # explicit "all" means no date filter

    repos = None
    if repo_param:
        repos = [r.strip() for r in repo_param.split(",")]

    thread = threading.Thread(target=_do_refresh, args=(repos, since, until), daemon=True)
    thread.start()

    scope = ", ".join(repos) if repos else "all repos"
    return jsonify({
        "status": "started",
        "scope": scope,
        "since": since,
        "until": until,
        "message": "Refresh started in background. Poll /api/refresh/status for progress.",
    })


@app.route("/api/refresh/status")
def api_refresh_status():
    """Return current refresh progress including rate limit info."""
    status = refresh_status_snapshot()
    elapsed = None
    if status.get("started_at"):
        elapsed = round(time.time() - status["started_at"], 1)

    # Build human-readable reset time
    reset_at = rate_limit_get("reset_at")
    reset_str = None
    if reset_at:
        reset_str = time.strftime("%H:%M:%S UTC", time.gmtime(reset_at))

    return jsonify({
        **status,
        "elapsed_seconds": elapsed,
        "data_loaded": data_store_loaded(),
        "rate_limit": {
            "remaining": rate_limit_get("remaining"),
            "limit": rate_limit_get("limit"),
            "used": rate_limit_get("used"),
            "resets_at": reset_str,
            "is_throttled": rate_limit_get("is_throttled"),
            "throttled_until": rate_limit_get("throttled_until"),
        },
    })


@app.route("/api/refresh/cancel", methods=["GET", "POST"])
def api_refresh_cancel():
    """Cancel a running refresh."""
    if not refresh_status_get("running"):
        return jsonify({"status": "not_running", "message": "No refresh is currently running."})

    cancel_refresh()
    logger.info("Refresh cancel requested by user.")
    return jsonify({"status": "cancelling", "message": "Cancel signal sent. Refresh will stop shortly."})


# 4. Summary ------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    """Return repo summaries and overview statistics (from cache)."""
    if not data_store_loaded():
        return (
            jsonify(
                {
                    "error": "No data available. Please call /api/refresh first to "
                    "fetch data from GitHub."
                }
            ),
            400,
        )

    repo_summaries = [
        s for s in data_store_get("repo_summaries", [])
        if s.get("total_prs") and s["total_prs"] > 0
    ]
    overview = _build_overview(repo_summaries)

    return jsonify({"repo_summaries": repo_summaries, "overview": overview})


# 4b. All PRs (cross-repo) ----------------------------------------------------
@app.route("/api/prs")
def api_all_prs():
    """Return all PRs across all repos with key metrics for the overview table.

    Query parameters
    ----------------
    limit : int
        Maximum number of PRs to return (default 500).
    """
    if not data_store_loaded():
        return (
            jsonify(
                {
                    "error": "No data available. Please call /api/refresh first to "
                    "fetch data from GitHub."
                }
            ),
            400,
        )

    limit = min(int(request.args.get("limit", "500")), 2000)
    all_pr_metrics = data_store_get("pr_metrics", {})
    all_prs = []

    for repo_name, prs in all_pr_metrics.items():
        for pr in prs:
            all_prs.append({
                "repo": repo_name,
                "number": pr.get("number"),
                "title": pr.get("title"),
                "author": pr.get("author") or pr.get("user"),
                "state": pr.get("state"),
                "created_at": pr.get("created_at"),
                "merged_at": pr.get("merged_at"),
                "total_cycle_time_hours": pr.get("total_cycle_time_hours"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "review_rounds": pr.get("review_rounds"),
                "bottleneck_count": len(pr.get("bottleneck_flags", []) or pr.get("bottlenecks", []) or []),
            })

    # Sort by created_at descending
    all_prs.sort(key=lambda p: p.get("created_at") or "", reverse=True)

    return jsonify(all_prs[:limit])


# 5. Single repo detail -------------------------------------------------------
@app.route("/api/repo/<repo_name>")
def api_repo(repo_name: str):
    """Return detailed PR metrics for a specific repository."""
    if not data_store_loaded():
        return (
            jsonify(
                {
                    "error": "No data available. Please call /api/refresh first to "
                    "fetch data from GitHub."
                }
            ),
            400,
        )

    # Find the repo summary
    repo_summary = next(
        (s for s in data_store_get("repo_summaries", []) if s.get("repo") == repo_name),
        None,
    )
    if repo_summary is None:
        return jsonify({"error": f"Repository '{repo_name}' not found"}), 404

    # Grab PR-level metrics and sort by cycle time descending
    prs = list(data_store_get("pr_metrics", {}).get(repo_name, []))
    prs.sort(key=lambda p: p.get("total_cycle_time_hours", 0) or 0, reverse=True)

    return jsonify({"repo_summary": repo_summary, "prs": prs})


# 5b. Purge a single repo -----------------------------------------------------
@app.route("/api/repo/<repo_name>/purge", methods=["POST", "DELETE"])
def api_repo_purge(repo_name: str):
    """Remove all cached data for a repository and re-save the cache."""
    if not data_store_loaded():
        return jsonify({"error": "No data loaded"}), 400

    # Check the repo actually has data
    raw_prs = data_store_get("raw_prs", {})
    if repo_name not in raw_prs:
        return jsonify({"error": f"No data for '{repo_name}'"}), 404

    # Remove from every section of the data store
    raw_prs.pop(repo_name, None)
    pr_metrics = data_store_get("pr_metrics", {})
    pr_metrics.pop(repo_name, None)
    repo_summaries = [
        s for s in data_store_get("repo_summaries", [])
        if s.get("repo") != repo_name
    ]
    bottlenecks = [
        b for b in data_store_get("bottlenecks", [])
        if b.get("repo") != repo_name
    ]

    # Write back and persist updated cache
    cache_payload = {
        "raw_prs": raw_prs,
        "repo_summaries": repo_summaries,
        "pr_metrics": pr_metrics,
        "bottlenecks": bottlenecks,
    }
    data_store_update(cache_payload)
    save_cache(cache_payload, CACHE_PATH)
    logger.info("Purged repo '%s' from data store and cache.", repo_name)

    return jsonify({"status": "purged", "repo": repo_name})


# 6. Single PR detail ---------------------------------------------------------
@app.route("/api/pr/<repo_name>/<int:pr_number>")
def api_pr(repo_name: str, pr_number: int):
    """Return full details for a single PR including metrics and bottlenecks."""
    if not data_store_loaded():
        return (
            jsonify(
                {
                    "error": "No data available. Please call /api/refresh first to "
                    "fetch data from GitHub."
                }
            ),
            400,
        )

    # Search computed PR metrics first
    pr_metrics_list = data_store_get("pr_metrics", {}).get(repo_name, [])
    pr_data = next(
        (p for p in pr_metrics_list if p.get("number") == pr_number), None
    )
    if pr_data is None:
        return (
            jsonify(
                {
                    "error": f"PR #{pr_number} not found in repository '{repo_name}'"
                }
            ),
            404,
        )

    # Attach bottleneck flags for this PR
    pr_bottlenecks = [
        b
        for b in data_store_get("bottlenecks", [])
        if b.get("repo") == repo_name and b.get("pr_number") == pr_number
    ]
    pr_data_with_bottlenecks = {**pr_data, "bottlenecks": pr_bottlenecks}

    return jsonify(pr_data_with_bottlenecks)


# 7. Bottlenecks ---------------------------------------------------------------
@app.route("/api/bottlenecks")
def api_bottlenecks():
    """Return PRs with bottleneck flags, optionally filtered.

    Query params:
        repo  - filter to a specific repository name
        type  - filter to a specific bottleneck type
        severity - filter by severity level (e.g. 'high', 'medium', 'low')
    """
    if not data_store_loaded():
        return (
            jsonify(
                {
                    "error": "No data available. Please call /api/refresh first to "
                    "fetch data from GitHub."
                }
            ),
            400,
        )

    bottlenecks = list(data_store_get("bottlenecks", []))

    # Apply optional filters
    repo_filter = request.args.get("repo")
    type_filter = request.args.get("type")
    severity_filter = request.args.get("severity")

    if repo_filter:
        bottlenecks = [b for b in bottlenecks if b.get("repo") == repo_filter]
    if type_filter:
        bottlenecks = [
            b for b in bottlenecks if b.get("bottleneck_type") == type_filter
        ]
    if severity_filter:
        bottlenecks = [
            b
            for b in bottlenecks
            if b.get("severity", "").lower() == severity_filter.lower()
        ]

    # Sort: high severity first, then by cycle time descending
    severity_order = {"high": 0, "medium": 1, "low": 2}
    bottlenecks.sort(
        key=lambda b: (
            severity_order.get(b.get("severity", "low").lower(), 99),
            -(b.get("total_cycle_time_hours", 0) or 0),
        )
    )

    return jsonify(bottlenecks)


# 8. Health check --------------------------------------------------------------
@app.route("/api/health")
def api_health():
    """Return service health for monitoring and container orchestration.

    Returns 200 when the service is healthy, 503 when degraded.
    The response always includes diagnostic details regardless of status.
    """
    checks: dict = {}
    healthy = True

    # --- Redis ---
    redis_active = is_redis_active()
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        checks["redis"] = {
            "status": "ok" if redis_active else "degraded",
            "url": redis_url.split("@")[-1] if "@" in redis_url else redis_url,
        }
        if not redis_active:
            checks["redis"]["detail"] = "configured but not reachable; using in-memory fallback"
    else:
        checks["redis"] = {"status": "disabled", "detail": "REDIS_URL not set"}

    # --- Data cache ---
    loaded = data_store_loaded()
    cache_exists = os.path.exists(CACHE_PATH)
    cache_source = "redis" if (redis_active and loaded) else ("file" if loaded else "none")
    cache_info: dict = {"loaded": loaded, "source": cache_source, "file_exists": cache_exists}
    if cache_exists:
        try:
            stat = os.stat(CACHE_PATH)
            cache_info["file_size_bytes"] = stat.st_size
            cache_info["file_modified"] = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc,
            ).isoformat()
            cache_info["age_seconds"] = round(time.time() - stat.st_mtime, 1)
        except OSError:
            pass
    checks["cache"] = cache_info

    # --- Last refresh ---
    refresh = refresh_status_snapshot()
    last_started = refresh.get("started_at")
    checks["refresh"] = {
        "running": refresh.get("running", False),
        "last_started": (
            datetime.fromtimestamp(last_started, tz=timezone.utc).isoformat()
            if last_started else None
        ),
        "last_status": refresh.get("progress", ""),
    }

    # --- Workers ---
    try:
        import gunicorn.config  # noqa: F401
        from gunicorn.conf import gunicorn_conf  # type: ignore
        worker_count = None  # running under gunicorn but count not easily introspectable
    except Exception:
        worker_count = None
    web_concurrency = os.environ.get("WEB_CONCURRENCY")
    checks["workers"] = {
        "web_concurrency": int(web_concurrency) if web_concurrency else "auto",
        "pid": os.getpid(),
    }

    # --- Rate limit ---
    rl = rate_limit_snapshot()
    checks["github_rate_limit"] = {
        "remaining": rl.get("remaining"),
        "limit": rl.get("limit"),
        "is_throttled": rl.get("is_throttled", False),
    }

    # --- Auto-refresh ---
    checks["auto_refresh"] = {
        "enabled": AUTO_REFRESH_INTERVAL_MINUTES > 0,
        "interval_minutes": AUTO_REFRESH_INTERVAL_MINUTES or None,
        "last_run": (
            datetime.fromtimestamp(_auto_refresh_last_run, tz=timezone.utc).isoformat()
            if _auto_refresh_last_run else None
        ),
        "last_result": _auto_refresh_last_result,
    }

    status_code = 200 if healthy else 503
    return jsonify({"status": "healthy" if healthy else "degraded", "checks": checks}), status_code


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
# Load cache into memory when the module is first imported / the app starts.
_load_cache_into_store()


# ---------------------------------------------------------------------------
# Scheduled auto-refresh
# ---------------------------------------------------------------------------
_auto_refresh_last_run: float | None = None
_auto_refresh_last_result: str = "never"


def _auto_refresh_loop():
    """Daemon thread: periodically trigger a full refresh.

    Each worker process spawns this thread.  Because _do_refresh uses
    acquire_refresh_lock(), only one worker will actually perform the
    refresh at any given time — the others silently skip.
    """
    global _auto_refresh_last_run, _auto_refresh_last_result
    interval_secs = AUTO_REFRESH_INTERVAL_MINUTES * 60

    # Wait the full interval before the first auto-refresh so a manual
    # sync on startup isn't duplicated.
    logger.info(
        "Auto-refresh enabled: every %d minutes (pid=%d).",
        AUTO_REFRESH_INTERVAL_MINUTES,
        os.getpid(),
    )
    time.sleep(interval_secs)

    while True:
        try:
            if refresh_status_get("running"):
                logger.info("Auto-refresh skipped: refresh already running.")
                _auto_refresh_last_result = "skipped (already running)"
            elif not acquire_refresh_lock():
                logger.info("Auto-refresh skipped: another worker holds the lock.")
                _auto_refresh_last_result = "skipped (lock held)"
            else:
                logger.info("Auto-refresh starting (pid=%d).", os.getpid())
                _auto_refresh_last_result = "running"
                # Compute the 'since' date using the default lookback
                since = None
                if DEFAULT_PR_LOOKBACK_DAYS > 0:
                    since = (
                        datetime.now(timezone.utc)
                        - timedelta(days=DEFAULT_PR_LOOKBACK_DAYS)
                    ).strftime("%Y-%m-%d")
                _do_refresh(repos=None, since=since, until=None)
                _auto_refresh_last_result = "completed"
            _auto_refresh_last_run = time.time()
        except Exception:
            logger.exception("Auto-refresh loop error")
            _auto_refresh_last_result = "error"
            _auto_refresh_last_run = time.time()

        time.sleep(interval_secs)


if AUTO_REFRESH_INTERVAL_MINUTES > 0:
    _auto_refresh_thread = threading.Thread(
        target=_auto_refresh_loop, daemon=True, name="auto-refresh",
    )
    _auto_refresh_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
