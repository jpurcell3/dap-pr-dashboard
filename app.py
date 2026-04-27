"""
Flask web server for the DAP PR Dashboard.

Serves a dashboard UI and exposes JSON APIs for PR metrics,
bottleneck detection, and repository summaries sourced from
GitHub / GitHub Enterprise.
"""

__version__ = "1.1.0"

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, request

from github_collector import (
    fetch_all_data, fetch_commit_checks, get_github_token,
    get_filtered_repos, get_all_org_repos, load_cache, save_cache,
    cancel_refresh, RefreshCancelled,
    GITHUB_API_BASE, ORG_NAME, REPO_FILTER,
    DEFAULT_PR_LOOKBACK_DAYS, MAX_PRS_PER_REPO,
)
from metrics import compute_all_metrics, compute_pr_metrics, detect_bottlenecks
from jenkins_client import fetch_build_details, is_configured as jenkins_is_configured
from reminder import (
    reminder_is_enabled, send_reminders, find_stale_reviews,
    REMINDER_THRESHOLD_HOURS, REMINDER_INTERVAL_HOURS, REMINDER_DRY_RUN,
)
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
    return render_template("index.html", version=__version__)


# 1b. Config info -------------------------------------------------------------
@app.route("/api/config")
def api_config():
    """Return the current configuration (non-sensitive)."""
    return jsonify({
        "github_api_url": GITHUB_API_BASE,
        "github_web_url": GITHUB_WEB_URL,
        "github_org": ORG_NAME,
        "repo_filter": REPO_FILTER or "",
        "default_lookback_days": DEFAULT_PR_LOOKBACK_DAYS,
        "max_prs_per_repo": MAX_PRS_PER_REPO,
        "env_file": str(os.path.join(os.path.dirname(__file__), ".env")),
    })


# 1c. Teams mapping ------------------------------------------------------------
TEAMS_FILE = os.environ.get("TEAMS_FILE", os.path.join(os.path.dirname(__file__), "teams.json"))


def _load_teams():
    """Load teams mapping from JSON file. Returns {} if file doesn't exist."""
    try:
        with open(TEAMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.route("/api/teams")
def api_teams():
    """Return team-to-repo mapping from teams.json."""
    return jsonify(_load_teams())


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
        filter_match}``.  The list is sorted by descending priority.
    """
    try:
        show_all = request.args.get("all", "").lower() in ("true", "1", "yes")

        if not show_all:
            # Simple mode: only repos with data (string list)
            repos = get_filtered_repos()
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
        filter_pat = re.compile(REPO_FILTER, re.IGNORECASE) if REPO_FILTER else None

        scored: list[dict] = []
        for name in all_names:
            prs = raw_prs.get(name, [])
            pr_count = len(prs) if prs else 0
            has_data = pr_count > 0
            filter_match = bool(filter_pat.search(name)) if filter_pat else True

            # Priority: repos with data first, then filter matches, then alphabetical
            priority = (2 if has_data else 0) + (1 if filter_match else 0)

            scored.append({
                "name": name,
                "priority": priority,
                "has_data": has_data,
                "pr_count": pr_count,
                "filter_match": filter_match,
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
            repos = get_filtered_repos()
            refresh_status_bulk_set({
                "repos_total": len(repos),
                "progress": f"Found {len(repos)} repos. Fetching PRs...",
            })
            logger.info("Refresh: found %d repos", len(repos))

        def progress_cb(repo_name, current, total):
            refresh_status_bulk_set({
                "current_repo": repo_name,
                "repos_done": current - 1,
                "progress": f"Fetching {repo_name} ({current}/{total})...",
            })
            logger.info("Refresh progress: %s (%d/%d)", repo_name, current, total)

        raw_prs = fetch_all_data(repos, progress_callback=progress_cb, since=since)

        # --- Re-check pass: re-fetch checks for PRs with pending results ---
        # Some checks are posted asynchronously minutes after the PR is
        # created.  Identify PRs that still have pending or very few checks
        # and re-fetch just their check data.
        pending_prs: list[tuple[str, dict]] = []   # (repo_name, pr)
        for repo_name, prs in raw_prs.items():
            for pr in prs:
                checks = pr.get("checks", {})
                if checks.get("pending", 0) > 0:
                    pending_prs.append((repo_name, pr))

        if pending_prs:
            delay = 30  # seconds
            refresh_status_set(
                "progress",
                f"Waiting {delay}s to re-check {len(pending_prs)} PR(s) "
                f"with pending checks...",
            )
            logger.info(
                "Re-check pass: %d PRs with pending checks, waiting %ds",
                len(pending_prs), delay,
            )
            time.sleep(delay)

            refresh_status_set(
                "progress",
                f"Re-fetching checks for {len(pending_prs)} PR(s)...",
            )
            recheck_token = get_github_token()
            for i, (repo_name, pr) in enumerate(pending_prs, 1):
                try:
                    commits = pr.get("commits", [])
                    if not commits:
                        continue
                    head_sha = commits[-1]["sha"]
                    pr["checks"] = fetch_commit_checks(
                        repo_name, head_sha, token=recheck_token,
                    )
                    logger.debug(
                        "Re-check %s#%d: %d checks (%d failures)",
                        repo_name, pr["number"],
                        pr["checks"]["total"], pr["checks"]["failure"],
                    )
                except Exception:
                    logger.debug(
                        "Re-check failed for %s#%d",
                        repo_name, pr.get("number"), exc_info=True,
                    )
            logger.info("Re-check pass complete: updated %d PRs", len(pending_prs))

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

        # Backfill Jenkins results for any checks the sync missed
        # (e.g. Jenkins was briefly unreachable during the sync).
        _backfill_jenkins_results()

        # Send reviewer reminders for stale reviews (background thread).
        _run_reminders()

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
        date.  Defaults to ``DEFAULT_PR_LOOKBACK_DAYS`` days ago (7 by
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
            # Extract check details: list of {name, conclusion} per check run
            checks_raw = pr.get("checks") if isinstance(pr.get("checks"), dict) else {}
            check_runs = checks_raw.get("checks", [])
            checks_summary = [
                {"name": c.get("name", ""), "conclusion": c.get("conclusion", "")}
                for c in check_runs
            ]
            # Distinct check names for this PR
            check_names = sorted({c["name"] for c in checks_summary if c["name"]})
            # Names of failed checks only
            failed_checks = sorted({
                c["name"] for c in checks_summary
                if c["conclusion"] in ("failure", "cancelled", "timed_out", "action_required")
            })

            # --- Check timing metrics ---
            # Compute duration (hours) for each check run that has timestamps
            check_durations = []   # [{name, conclusion, duration_hours}, ...]
            failed_check_time_hours = 0.0   # total hours spent on failed checks
            passed_check_time_hours = 0.0   # total hours spent on passed checks
            for c in check_runs:
                sa = c.get("started_at", "")
                ca = c.get("completed_at", "")
                if sa and ca:
                    try:
                        t_start = datetime.fromisoformat(sa.replace("Z", "+00:00"))
                        t_end = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                        dur_hrs = round((t_end - t_start).total_seconds() / 3600, 3)
                    except (ValueError, TypeError):
                        dur_hrs = None
                else:
                    dur_hrs = None
                cname = c.get("name", "")
                conc = c.get("conclusion", "")
                if dur_hrs is not None:
                    check_durations.append({
                        "name": cname,
                        "conclusion": conc,
                        "duration_hours": dur_hrs,
                    })
                    if conc in ("failure", "cancelled", "timed_out", "action_required"):
                        failed_check_time_hours += dur_hrs
                    else:
                        passed_check_time_hours += dur_hrs

            all_prs.append({
                "repo": repo_name,
                "number": pr.get("number"),
                "title": pr.get("title"),
                "author": pr.get("author") or pr.get("user"),
                "state": pr.get("state"),
                "created_at": pr.get("created_at"),
                "merged_at": pr.get("merged_at"),
                "total_cycle_time_hours": pr.get("total_cycle_time_hours"),
                "time_to_first_review_hours": pr.get("time_to_first_review_hours"),
                "first_review_to_approval_hours": pr.get("first_review_to_approval_hours"),
                "approval_to_merge_hours": pr.get("approval_to_merge_hours"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "review_rounds": pr.get("review_rounds"),
                "bottleneck_count": len(pr.get("bottleneck_flags", []) or pr.get("bottlenecks", []) or []),
                "checks_overall": checks_raw.get("overall_state", "unknown"),
                "check_names": check_names,
                "failed_checks": failed_checks,
                "check_durations": check_durations,
                "failed_check_time_hours": round(failed_check_time_hours, 3),
                "passed_check_time_hours": round(passed_check_time_hours, 3),
                "total_check_time_hours": round(failed_check_time_hours + passed_check_time_hours, 3),
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

    # --- Reviewer reminders ---
    checks["reminders"] = {
        "enabled": reminder_is_enabled(),
        "threshold_hours": REMINDER_THRESHOLD_HOURS,
        "interval_hours": REMINDER_INTERVAL_HOURS,
        "dry_run": REMINDER_DRY_RUN,
        "last_run": (
            datetime.fromtimestamp(_reminder_last_run, tz=timezone.utc).isoformat()
            if _reminder_last_run else None
        ),
        "last_result": _reminder_last_result,
    }

    status_code = 200 if healthy else 503
    return jsonify({"status": "healthy" if healthy else "degraded", "checks": checks}), status_code


# 9. Reviewer reminders --------------------------------------------------------

@app.route("/api/reminders")
def api_reminders():
    """Return stale review requests and reminder status.

    Query params:
        threshold – override default threshold hours (optional)
        send      – "true" to actually send reminders now (POST-like via GET)
    """
    raw_prs = data_store_get("raw_prs", {})
    if not raw_prs:
        return jsonify({"error": "No data loaded — run a sync first."}), 404

    threshold = request.args.get("threshold", type=float)
    stale = find_stale_reviews(raw_prs, threshold_hours=threshold)

    result: dict = {
        "enabled": reminder_is_enabled(),
        "threshold_hours": threshold or REMINDER_THRESHOLD_HOURS,
        "interval_hours": REMINDER_INTERVAL_HOURS,
        "dry_run": REMINDER_DRY_RUN,
        "stale_count": len(stale),
        "stale_reviews": stale,
        "last_run": (
            datetime.fromtimestamp(_reminder_last_run, tz=timezone.utc).isoformat()
            if _reminder_last_run else None
        ),
        "last_result": _reminder_last_result,
    }

    # Allow triggering reminders on demand
    if request.args.get("send", "").lower() in ("1", "true", "yes"):
        if not reminder_is_enabled():
            result["send_error"] = "Reminders are disabled (set REMINDER_ENABLED=true)"
        else:
            send_result = send_reminders(raw_prs)
            result["send_result"] = send_result

    return jsonify(result)


# 10. Jenkins build details ----------------------------------------------------
# In-memory cache: build_url → {data, fetched_at}
_jenkins_cache: dict[str, dict] = {}
_JENKINS_CACHE_TTL = 600  # seconds

@app.route("/api/jenkins/build")
def api_jenkins_build():
    """Fetch Jenkins build details (stages, test report) for a build URL.

    Query params:
        url — full Jenkins build URL (from commit-status target_url)

    Returns JSON with ``build``, ``stages``, and ``test_report`` keys.
    Results are cached in-memory for 10 minutes.
    """
    build_url = request.args.get("url", "").strip()
    if not build_url:
        return jsonify({"error": "Missing 'url' query parameter"}), 400

    if not jenkins_is_configured():
        return jsonify({"error": "Jenkins credentials not configured"}), 503

    # Check in-memory cache
    cached = _jenkins_cache.get(build_url)
    if cached and (time.time() - cached["fetched_at"]) < _JENKINS_CACHE_TTL:
        return jsonify(cached["data"])

    details = fetch_build_details(build_url)
    if details is None:
        return jsonify({"error": "Could not reach Jenkins or build not found"}), 502

    # Cache the result
    _jenkins_cache[build_url] = {"data": details, "fetched_at": time.time()}

    return jsonify(details)


# ---------------------------------------------------------------------------
# Jenkins result backfill
# ---------------------------------------------------------------------------

def _backfill_jenkins_results():
    """Enrich cached Jenkins checks that are missing ``jenkins_result``.

    Runs in a background thread so it never blocks startup or the sync
    response.  When finished, recomputes metrics and saves the cache only
    if at least one check was enriched.
    """
    if not jenkins_is_configured():
        return
    if not data_store_loaded():
        return

    raw_prs = data_store_get("raw_prs", {})
    # Collect checks that need enrichment
    tasks: list[dict] = []
    for prs in raw_prs.values():
        if not isinstance(prs, list):
            continue
        for pr in prs:
            checks = pr.get("checks")
            if not isinstance(checks, dict):
                continue
            for c in checks.get("checks", []):
                if (
                    c.get("name", "").startswith("continuous-integration/jenkins/")
                    and c.get("details_url")
                    and not c.get("jenkins_result")
                ):
                    tasks.append(c)

    if not tasks:
        return

    logger.info("Jenkins backfill: %d checks to enrich", len(tasks))

    def _run():
        from jenkins_client import fetch_build_info
        from concurrent.futures import ThreadPoolExecutor

        enriched = 0
        def _fetch(check):
            try:
                info = fetch_build_info(check["details_url"])
                if info and info.get("result"):
                    check["jenkins_result"] = info["result"]
                    return True
            except Exception:
                pass
            return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_fetch, tasks))
        enriched = sum(1 for r in results if r)

        if enriched == 0:
            logger.info("Jenkins backfill: no new results (0/%d reachable)", len(tasks))
            return

        logger.info("Jenkins backfill: enriched %d/%d checks, recomputing metrics", enriched, len(tasks))
        all_metrics = compute_all_metrics(raw_prs)
        cache_payload = {
            "raw_prs": raw_prs,
            "repo_summaries": all_metrics["repo_summaries"],
            "pr_metrics": all_metrics["pr_metrics"],
            "bottlenecks": all_metrics["bottlenecks"],
        }
        data_store_update(cache_payload)
        data_store_set("loaded", True)
        save_cache(cache_payload, CACHE_PATH)
        logger.info("Jenkins backfill complete — cache saved")

    thread = threading.Thread(target=_run, daemon=True, name="jenkins-backfill")
    thread.start()


# ---------------------------------------------------------------------------
# Reviewer reminders
# ---------------------------------------------------------------------------
_reminder_last_run: float | None = None
_reminder_last_result: dict | None = None


def _run_reminders():
    """Post reviewer reminder comments for stale reviews.

    Runs in a background thread so it never blocks sync or startup.
    """
    global _reminder_last_run, _reminder_last_result

    if not reminder_is_enabled():
        return
    if not data_store_loaded():
        return

    raw_prs = data_store_get("raw_prs", {})
    if not raw_prs:
        return

    logger.info("Reviewer reminders: starting scan...")

    def _run():
        global _reminder_last_run, _reminder_last_result
        try:
            result = send_reminders(raw_prs)
            _reminder_last_run = time.time()
            _reminder_last_result = result
        except Exception:
            logger.exception("Reviewer reminders failed")
            _reminder_last_run = time.time()
            _reminder_last_result = {"error": "exception — see server log"}

    thread = threading.Thread(target=_run, daemon=True, name="reviewer-reminders")
    thread.start()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
# Load cache into memory when the module is first imported / the app starts.
_load_cache_into_store()

# Backfill any Jenkins checks missing jenkins_result (runs in background).
_backfill_jenkins_results()

# Run reviewer reminders if enabled (runs in background).
_run_reminders()

# Clear stale refresh state left over from a previous crash / restart.
# If Redis says "running" but no thread is actually active, reset it.
if refresh_status_get("running"):
    logger.warning("Stale refresh state detected on startup — clearing.")
    refresh_status_bulk_set({
        "running": False,
        "progress": "Cleared stale sync on startup",
        "error": "",
    })


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
