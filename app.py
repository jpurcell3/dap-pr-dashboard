"""
Flask web server for the Fusion PR Metrics Dashboard.

Serves a dashboard UI and exposes JSON APIs for PR metrics,
bottleneck detection, and repository summaries sourced from
the fusion-e GitHub organisation.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request

from github_collector import (
    fetch_all_data, get_fusion_repos, load_cache, save_cache,
    rate_limit_info, cancel_refresh, RefreshCancelled,
    GITHUB_API_BASE, ORG_NAME, REPO_PREFIX,
    DEFAULT_PR_LOOKBACK_DAYS, MAX_PRS_PER_REPO,
)
from metrics import compute_all_metrics, compute_pr_metrics, detect_bottlenecks

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(__file__), "server.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

CACHE_PATH = r"C:\Users\jpurcell\fusion-pr-dashboard\pr_cache.json"

# In-memory store populated from cache or a /api/refresh call.
_data_store: dict = {
    "raw_prs": {},        # repo_name -> [raw PR dicts]
    "repo_summaries": [], # list of per-repo summary dicts
    "pr_metrics": {},     # repo_name -> [pr metric dicts]
    "bottlenecks": [],    # flat list of bottleneck dicts
    "loaded": False,
}

# Background refresh tracking
_refresh_status: dict = {
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
    """Try to load the JSON cache file into the in-memory store.
    Returns True on success, False otherwise."""
    if not os.path.exists(CACHE_PATH):
        logger.info("No cache file found at %s", CACHE_PATH)
        return False

    try:
        cached = load_cache(CACHE_PATH)
        if not cached:
            return False

        _data_store["raw_prs"] = cached.get("raw_prs", {})
        _data_store["repo_summaries"] = cached.get("repo_summaries", [])
        _data_store["pr_metrics"] = cached.get("pr_metrics", {})
        _data_store["bottlenecks"] = cached.get("bottlenecks", [])
        _data_store["loaded"] = True
        logger.info("Cache loaded successfully from %s", CACHE_PATH)
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
        "github_org": ORG_NAME,
        "repo_prefix": REPO_PREFIX,
        "default_lookback_days": DEFAULT_PR_LOOKBACK_DAYS,
        "max_prs_per_repo": MAX_PRS_PER_REPO,
        "env_file": str(os.path.join(os.path.dirname(__file__), ".env")),
    })


# 2. List repos ---------------------------------------------------------------
@app.route("/api/repos")
def api_repos():
    """Return a JSON list of fusion-* repository names.

    By default only repos that have PR data in the current data store are
    returned.  Pass ``?all=true`` to include every discovered repo (useful
    for the refresh repo-picker).
    """
    try:
        show_all = request.args.get("all", "").lower() in ("true", "1", "yes")
        repos = get_fusion_repos()

        if not show_all and _data_store["loaded"]:
            repos_with_data = {
                name for name, prs in _data_store.get("raw_prs", {}).items()
                if prs
            }
            repos = [r for r in repos if r in repos_with_data]

        return jsonify(repos)
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

        _refresh_status["running"] = True
        _refresh_status["error"] = None
        _refresh_status["started_at"] = time.time()
        _refresh_status["scope"] = scope_label

        if is_partial:
            _refresh_status["repos_total"] = len(repos)
            _refresh_status["progress"] = f"Fetching PRs for {scope_label}..."
            logger.info("Refresh (partial): repos=%s, since=%s, until=%s", repos, since, until)
        else:
            _refresh_status["progress"] = "Discovering repos..."
            repos = get_fusion_repos()
            _refresh_status["repos_total"] = len(repos)
            _refresh_status["progress"] = f"Found {len(repos)} repos. Fetching PRs..."
            logger.info("Refresh: found %d fusion repos", len(repos))

        def progress_cb(repo_name, current, total):
            _refresh_status["current_repo"] = repo_name
            _refresh_status["repos_done"] = current - 1
            _refresh_status["progress"] = f"Fetching {repo_name} ({current}/{total})..."
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
        _refresh_status["prs_fetched"] = total_prs
        _refresh_status["repos_done"] = len(repos)
        _refresh_status["progress"] = "Computing metrics..."
        logger.info("Refresh: fetched %d total PRs, computing metrics...", total_prs)

        if is_partial:
            # --- Partial refresh: merge new data into existing store --------
            # Update only the refreshed repos in raw_prs
            merged_raw = dict(_data_store.get("raw_prs", {}))
            for repo_name in raw_prs:
                merged_raw[repo_name] = raw_prs[repo_name]

            # Recompute ALL metrics from the full merged raw_prs dict
            all_metrics = compute_all_metrics(merged_raw)
            repo_summaries = all_metrics.get("repo_summaries", [])
            pr_metrics = all_metrics.get("pr_metrics", {})
            bottlenecks = all_metrics.get("bottlenecks", [])

            _refresh_status["progress"] = "Saving cache..."
            cache_payload = {
                "raw_prs": merged_raw,
                "repo_summaries": repo_summaries,
                "pr_metrics": pr_metrics,
                "bottlenecks": bottlenecks,
            }
            save_cache(cache_payload, CACHE_PATH)

            _data_store.update(cache_payload)
            _data_store["loaded"] = True
        else:
            # --- Full refresh: replace everything --------------------------
            all_metrics = compute_all_metrics(raw_prs)
            repo_summaries = all_metrics.get("repo_summaries", [])
            pr_metrics = all_metrics.get("pr_metrics", {})
            bottlenecks = all_metrics.get("bottlenecks", [])

            _refresh_status["progress"] = "Saving cache..."
            cache_payload = {
                "raw_prs": raw_prs,
                "repo_summaries": repo_summaries,
                "pr_metrics": pr_metrics,
                "bottlenecks": bottlenecks,
            }
            save_cache(cache_payload, CACHE_PATH)

            _data_store.update(cache_payload)
            _data_store["loaded"] = True

        _refresh_status["progress"] = "Complete!"
        _refresh_status["running"] = False
        logger.info("Refresh complete: %d repos, %d PRs", len(repos), total_prs)

    except RefreshCancelled:
        logger.info("Refresh cancelled by user.")
        _refresh_status["running"] = False
        _refresh_status["progress"] = "Cancelled."
        _refresh_status["error"] = None
    except Exception as exc:
        logger.exception("Refresh failed")
        _refresh_status["error"] = str(exc)
        _refresh_status["running"] = False
        _refresh_status["progress"] = f"Error: {exc}"


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
    if _refresh_status["running"]:
        return jsonify({"status": "already_running", **_refresh_status})

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
    elapsed = None
    if _refresh_status["started_at"]:
        elapsed = round(time.time() - _refresh_status["started_at"], 1)

    # Build human-readable reset time
    reset_str = None
    if rate_limit_info["reset_at"]:
        reset_str = time.strftime("%H:%M:%S UTC", time.gmtime(rate_limit_info["reset_at"]))

    return jsonify({
        **_refresh_status,
        "elapsed_seconds": elapsed,
        "data_loaded": _data_store["loaded"],
        "rate_limit": {
            "remaining": rate_limit_info["remaining"],
            "limit": rate_limit_info["limit"],
            "used": rate_limit_info["used"],
            "resets_at": reset_str,
            "is_throttled": rate_limit_info["is_throttled"],
            "throttled_until": rate_limit_info["throttled_until"],
        },
    })


@app.route("/api/refresh/cancel", methods=["GET", "POST"])
def api_refresh_cancel():
    """Cancel a running refresh."""
    if not _refresh_status["running"]:
        return jsonify({"status": "not_running", "message": "No refresh is currently running."})

    cancel_refresh()
    logger.info("Refresh cancel requested by user.")
    return jsonify({"status": "cancelling", "message": "Cancel signal sent. Refresh will stop shortly."})


# 4. Summary ------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    """Return repo summaries and overview statistics (from cache)."""
    if not _data_store["loaded"]:
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
        s for s in _data_store["repo_summaries"]
        if s.get("total_prs") and s["total_prs"] > 0
    ]
    overview = _build_overview(repo_summaries)

    return jsonify({"repo_summaries": repo_summaries, "overview": overview})


# 5. Single repo detail -------------------------------------------------------
@app.route("/api/repo/<repo_name>")
def api_repo(repo_name: str):
    """Return detailed PR metrics for a specific repository."""
    if not _data_store["loaded"]:
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
        (s for s in _data_store["repo_summaries"] if s.get("repo") == repo_name),
        None,
    )
    if repo_summary is None:
        return jsonify({"error": f"Repository '{repo_name}' not found"}), 404

    # Grab PR-level metrics and sort by cycle time descending
    prs = list(_data_store["pr_metrics"].get(repo_name, []))
    prs.sort(key=lambda p: p.get("total_cycle_time_hours", 0) or 0, reverse=True)

    return jsonify({"repo_summary": repo_summary, "prs": prs})


# 6. Single PR detail ---------------------------------------------------------
@app.route("/api/pr/<repo_name>/<int:pr_number>")
def api_pr(repo_name: str, pr_number: int):
    """Return full details for a single PR including metrics and bottlenecks."""
    if not _data_store["loaded"]:
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
    pr_metrics_list = _data_store["pr_metrics"].get(repo_name, [])
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
        for b in _data_store["bottlenecks"]
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
    if not _data_store["loaded"]:
        return (
            jsonify(
                {
                    "error": "No data available. Please call /api/refresh first to "
                    "fetch data from GitHub."
                }
            ),
            400,
        )

    bottlenecks = list(_data_store["bottlenecks"])

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


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
# Load cache into memory when the module is first imported / the app starts.
_load_cache_into_store()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
