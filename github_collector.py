"""
GitHub PR Data Collector.

Fetches PR data from the GitHub API for repositories in a configured
organization. Supports caching, pagination, and rate-limit handling.

Configuration is read from environment variables or a .env file in the
project directory. See .env for available settings.
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import requests
import urllib3

# Suppress InsecureRequestWarning when SSL verification is disabled
# (common in corporate environments with proxy/firewall certificates)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — loaded from .env file, then overridden by env vars
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"


def _load_env_file():
    """Parse a simple KEY=VALUE .env file into os.environ (does not
    overwrite existing env vars)."""
    if not _ENV_FILE.exists():
        return
    with open(_ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Do NOT overwrite existing env vars so that real env takes precedence
            if key not in os.environ:
                os.environ[key] = value


_load_env_file()

GITHUB_API_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
ORG_NAME = os.environ.get("GITHUB_ORG", "fusion-e")
REPO_PREFIX = os.environ.get("GITHUB_REPO_PREFIX", "fusion").lower()
SSL_VERIFY = os.environ.get("SSL_VERIFY", "false").lower() not in ("false", "0", "no")
DEFAULT_CACHE_PATH = Path(__file__).parent / "pr_cache.json"
DEFAULT_PR_LOOKBACK_DAYS = int(os.environ.get("DEFAULT_PR_LOOKBACK_DAYS", "90"))
MAX_PRS_PER_REPO = int(os.environ.get("MAX_PRS_PER_REPO", "500"))

logger.info(
    "Config loaded — API: %s | Org: %s | Prefix: %r | SSL verify: %s",
    GITHUB_API_BASE, ORG_NAME, REPO_PREFIX, SSL_VERIFY,
)

# Cancellation event — set this to signal fetch_all_data to stop early.
_cancel_event = threading.Event()


class RefreshCancelled(Exception):
    """Raised when a refresh is cancelled via the cancel event."""


def cancel_refresh():
    """Signal any running fetch_all_data to stop."""
    _cancel_event.set()


def _check_cancelled():
    """Raise RefreshCancelled if cancellation has been requested."""
    if _cancel_event.is_set():
        raise RefreshCancelled("Refresh cancelled by user.")

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_github_token() -> str:
    """Return a GitHub token from env vars (GITHUB_TOKEN or GH_TOKEN) or the ``gh`` CLI.

    Raises
    ------
    RuntimeError
        If no token can be obtained from any source.
    """
    # Check GITHUB_TOKEN first (from .env), then GH_TOKEN
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(var)
        if token:
            logger.debug("Using GitHub token from %s environment variable.", var)
            return token.strip()

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        token = result.stdout.strip()
        if token:
            logger.debug("Using GitHub token from gh CLI.")
            return token
    except FileNotFoundError:
        logger.warning("gh CLI not found on PATH.")
    except subprocess.CalledProcessError as exc:
        logger.warning("gh auth token failed: %s", exc.stderr.strip())
    except subprocess.TimeoutExpired:
        logger.warning("gh auth token timed out.")

    raise RuntimeError(
        "Could not obtain a GitHub token. Set the GH_TOKEN environment "
        "variable or authenticate with `gh auth login`."
    )


def _build_headers(token: str | None = None) -> dict[str, str]:
    """Build common request headers, including auth if a token is available."""
    if token is None:
        token = get_github_token()
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------

# Shared rate-limit state accessible from other modules (e.g. app.py)
rate_limit_info: dict = {
    "remaining": None,
    "limit": None,
    "used": None,
    "reset_at": None,       # UTC epoch timestamp
    "is_throttled": False,
    "throttled_until": None, # human-readable reset time when throttled
}

def _check_rate_limit(response: requests.Response) -> None:
    """Update rate-limit tracking and sleep if nearly exhausted."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    limit = response.headers.get("X-RateLimit-Limit")
    used = response.headers.get("X-RateLimit-Used")
    reset_at = response.headers.get("X-RateLimit-Reset")

    if remaining is None:
        return

    remaining = int(remaining)
    rate_limit_info["remaining"] = remaining
    rate_limit_info["limit"] = int(limit) if limit else None
    rate_limit_info["used"] = int(used) if used else None
    rate_limit_info["reset_at"] = int(reset_at) if reset_at else None

    # Log every 50 requests or when getting low
    if remaining % 50 == 0 or remaining <= 100:
        logger.info(
            "GitHub API rate limit: %d/%s remaining (used: %s)",
            remaining,
            limit or "?",
            used or "?",
        )

    if remaining <= 5:
        if reset_at is not None:
            sleep_seconds = max(int(reset_at) - int(time.time()), 0) + 1
            reset_time = time.strftime("%H:%M:%S UTC", time.gmtime(int(reset_at)))
        else:
            sleep_seconds = 60
            reset_time = "~1 minute"

        rate_limit_info["is_throttled"] = True
        rate_limit_info["throttled_until"] = reset_time

        logger.warning(
            "Rate limit nearly exhausted (%d remaining). "
            "Sleeping for %d seconds until %s.",
            remaining,
            sleep_seconds,
            reset_time,
        )
        time.sleep(sleep_seconds)
        rate_limit_info["is_throttled"] = False
        rate_limit_info["throttled_until"] = None


def _get_paginated(url: str, headers: dict, params: dict | None = None,
                    max_results: int | None = None) -> list[dict]:
    """Follow GitHub pagination links and return all results as a flat list.

    Parameters
    ----------
    max_results:
        Optional cap on the number of results to collect.  When reached,
        pagination stops early.
    """
    params = dict(params) if params else {}
    params.setdefault("per_page", 100)
    results: list[dict] = []

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30, verify=SSL_VERIFY)
        _check_rate_limit(response)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.extend(data.get("items", [data]))

        if max_results is not None and len(results) >= max_results:
            results = results[:max_results]
            break

        # Follow the "next" link if present
        url = response.links.get("next", {}).get("url")
        # After the first request the full URL already contains params
        params = {}

    return results


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------

def get_fusion_repos(token: str | None = None) -> list[str]:
    """Return a sorted list of repo names starting with ``fusion`` in *fusion-e*.

    Uses the GitHub ``/orgs/{org}/repos`` endpoint with pagination.
    """
    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/orgs/{ORG_NAME}/repos"
    params = {"per_page": 100, "type": "all"}

    all_repos = _get_paginated(url, headers, params)
    if REPO_PREFIX:
        filtered_repos = sorted(
            repo["name"]
            for repo in all_repos
            if repo.get("name", "").lower().startswith(REPO_PREFIX)
        )
    else:
        # Empty prefix means include all repos in the org
        filtered_repos = sorted(repo["name"] for repo in all_repos)
    logger.info(
        "Found %d repos in %s (prefix=%r).", len(filtered_repos), ORG_NAME, REPO_PREFIX
    )
    return filtered_repos


# ---------------------------------------------------------------------------
# PR fetching
# ---------------------------------------------------------------------------

def fetch_prs_for_repo(
    repo_name: str,
    state: str = "all",
    per_page: int = 100,
    token: str | None = None,
    since: str | None = None,
    max_prs: int | None = None,
) -> list[dict]:
    """Fetch all PRs for *repo_name* in the org, with pagination.

    Parameters
    ----------
    repo_name:
        Repository name (not full slug).
    state:
        ``open``, ``closed``, or ``all``.
    per_page:
        Results per page (max 100).
    token:
        Optional pre-fetched GitHub token.
    since:
        Optional ISO date string (e.g. ``"2024-01-01"``).  When provided,
        only PRs whose ``created_at`` is on or after this date are returned.
        Because results are sorted by ``updated`` descending, pagination
        stops early once a PR is found whose ``created_at`` **and**
        ``updated_at`` are both before the *since* date.
    max_prs:
        Optional maximum number of PRs to return.  When set, pagination stops
        once this many PRs have been collected.  Defaults to
        :data:`MAX_PRS_PER_REPO`.

    Returns
    -------
    list[dict]
        List of PR objects from the GitHub API.
    """
    if max_prs is None:
        max_prs = MAX_PRS_PER_REPO

    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}/pulls"
    params = {"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"}

    if since is None:
        # No date filter – paginate with a max_prs safety cap.
        try:
            prs = _get_paginated(url, headers, params, max_results=max_prs)
            if len(prs) >= max_prs:
                logger.warning(
                    "Hit max_prs cap (%d) for %s/%s — some older PRs may be excluded.",
                    max_prs, ORG_NAME, repo_name,
                )
            logger.info("Fetched %d PRs from %s/%s.", len(prs), ORG_NAME, repo_name)
            return prs
        except requests.HTTPError as exc:
            logger.error("Failed to fetch PRs for %s: %s", repo_name, exc)
            return []

    # --- Manual pagination with date-based early stop ----------------------
    try:
        since_date = since[:10]  # normalise to "YYYY-MM-DD"
        filtered_prs: list[dict] = []
        current_url: str | None = url
        current_params: dict = dict(params)
        stop_paginating = False

        while current_url and not stop_paginating:
            response = requests.get(
                current_url, headers=headers, params=current_params,
                timeout=30, verify=SSL_VERIFY,
            )
            _check_rate_limit(response)
            response.raise_for_status()
            page_data = response.json()
            if not isinstance(page_data, list):
                page_data = page_data.get("items", [page_data])

            for pr in page_data:
                created = pr.get("created_at", "")[:10]
                updated = pr.get("updated_at", "")[:10]

                # Early stop: PR was created AND last updated before the
                # since date.  Because results are sorted by updated desc,
                # all remaining PRs will also be older.
                if created < since_date and updated < since_date:
                    stop_paginating = True
                    break

                # Keep only PRs created on or after the since date.
                if created >= since_date:
                    filtered_prs.append(pr)

                # Respect max_prs cap
                if len(filtered_prs) >= max_prs:
                    stop_paginating = True
                    break

            # Follow the "next" link if present
            current_url = response.links.get("next", {}).get("url")
            current_params = {}  # URL already contains params after first request

        if len(filtered_prs) >= max_prs:
            logger.warning(
                "Hit max_prs cap (%d) for %s/%s (since=%s) — some PRs may be excluded.",
                max_prs, ORG_NAME, repo_name, since,
            )
        logger.info(
            "Fetched %d PRs from %s/%s (since=%s).",
            len(filtered_prs), ORG_NAME, repo_name, since,
        )
        return filtered_prs

    except requests.HTTPError as exc:
        logger.error("Failed to fetch PRs for %s: %s", repo_name, exc)
        return []


def fetch_pr_reviews(
    repo_name: str,
    pr_number: int,
    token: str | None = None,
) -> list[dict]:
    """Fetch all reviews for a pull request."""
    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}/pulls/{pr_number}/reviews"
    try:
        return _get_paginated(url, headers)
    except requests.HTTPError as exc:
        logger.error(
            "Failed to fetch reviews for %s#%d: %s", repo_name, pr_number, exc
        )
        return []


def fetch_pr_timeline(
    repo_name: str,
    pr_number: int,
    token: str | None = None,
) -> list[dict]:
    """Fetch timeline events for a pull request.

    Uses the Mockingbird preview header required by the timeline API.
    """
    headers = _build_headers(token)
    headers["Accept"] = "application/vnd.github.mockingbird-preview+json"
    url = (
        f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}/issues/{pr_number}/timeline"
    )
    try:
        return _get_paginated(url, headers)
    except requests.HTTPError as exc:
        logger.error(
            "Failed to fetch timeline for %s#%d: %s", repo_name, pr_number, exc
        )
        return []


def fetch_pr_comments(
    repo_name: str,
    pr_number: int,
    token: str | None = None,
) -> dict[str, list[dict]]:
    """Fetch both review comments and issue comments for a pull request.

    Returns
    -------
    dict
        ``{"review_comments": [...], "issue_comments": [...]}``
    """
    headers = _build_headers(token)

    review_comments_url = (
        f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}/pulls/{pr_number}/comments"
    )
    issue_comments_url = (
        f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}/issues/{pr_number}/comments"
    )

    review_comments: list[dict] = []
    issue_comments: list[dict] = []

    try:
        review_comments = _get_paginated(review_comments_url, headers)
    except requests.HTTPError as exc:
        logger.error(
            "Failed to fetch review comments for %s#%d: %s",
            repo_name,
            pr_number,
            exc,
        )

    try:
        issue_comments = _get_paginated(issue_comments_url, headers)
    except requests.HTTPError as exc:
        logger.error(
            "Failed to fetch issue comments for %s#%d: %s",
            repo_name,
            pr_number,
            exc,
        )

    return {
        "review_comments": review_comments,
        "issue_comments": issue_comments,
    }


# ---------------------------------------------------------------------------
# Aggregate fetch
# ---------------------------------------------------------------------------

def fetch_all_data(
    repos: list[str] | None = None,
    progress_callback=None,
    token: str | None = None,
    since: str | None = None,
) -> dict[str, list[dict]]:
    """Fetch all PRs (with reviews, timeline, and comments) for fusion repos.

    Parameters
    ----------
    repos:
        Explicit list of repo names. If ``None``, discovers repos via
        :func:`get_fusion_repos`.
    progress_callback:
        Optional callable ``(repo_name, current_index, total_repos) -> None``
        invoked once per repo before fetching begins.
    token:
        Optional pre-fetched GitHub token.
    since:
        Optional ISO date string (e.g. ``"2024-01-01"``).  Passed through
        to :func:`fetch_prs_for_repo` to limit PRs by ``created_at`` date.

    Returns
    -------
    dict[str, list[dict]]
        Mapping of repo name to list of enriched PR dicts.
    """
    if token is None:
        token = get_github_token()

    if repos is None:
        repos = get_fusion_repos(token=token)

    total = len(repos)
    result: dict[str, list[dict]] = {}

    # Clear cancel flag at start of a new fetch
    _cancel_event.clear()

    for idx, repo_name in enumerate(repos, start=1):
        _check_cancelled()

        if progress_callback is not None:
            try:
                progress_callback(repo_name, idx, total)
            except Exception:
                logger.debug("progress_callback raised an exception", exc_info=True)

        logger.info("Processing repo %d/%d: %s", idx, total, repo_name)
        prs = fetch_prs_for_repo(repo_name, token=token, since=since)

        enriched_prs: list[dict] = []
        for pr in prs:
            _check_cancelled()
            pr_number: int = pr["number"]
            try:
                pr["reviews"] = fetch_pr_reviews(repo_name, pr_number, token=token)
            except Exception:
                logger.error(
                    "Unexpected error fetching reviews for %s#%d",
                    repo_name,
                    pr_number,
                    exc_info=True,
                )
                pr["reviews"] = []

            try:
                pr["timeline_events"] = fetch_pr_timeline(
                    repo_name, pr_number, token=token
                )
            except Exception:
                logger.error(
                    "Unexpected error fetching timeline for %s#%d",
                    repo_name,
                    pr_number,
                    exc_info=True,
                )
                pr["timeline_events"] = []

            try:
                comments = fetch_pr_comments(repo_name, pr_number, token=token)
                pr["review_comments"] = comments["review_comments"]
                pr["issue_comments"] = comments["issue_comments"]
            except Exception:
                logger.error(
                    "Unexpected error fetching comments for %s#%d",
                    repo_name,
                    pr_number,
                    exc_info=True,
                )
                pr["review_comments"] = []
                pr["issue_comments"] = []

            enriched_prs.append(pr)

        result[repo_name] = enriched_prs
        logger.info(
            "Finished %s: %d PRs collected and enriched.", repo_name, len(enriched_prs)
        )

    return result


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def save_cache(
    data: dict[str, list[dict]],
    filepath: str | Path = DEFAULT_CACHE_PATH,
) -> None:
    """Persist *data* as JSON to *filepath*."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    logger.info("Cache saved to %s (%d bytes).", filepath, filepath.stat().st_size)


def load_cache(
    filepath: str | Path = DEFAULT_CACHE_PATH,
) -> dict[str, list[dict]] | None:
    """Load cached data from *filepath*.

    Returns ``None`` if the file does not exist or cannot be parsed.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        logger.info("No cache file found at %s.", filepath)
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        logger.info("Cache loaded from %s.", filepath)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load cache from %s: %s", filepath, exc)
        return None


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    def _progress(repo: str, current: int, total: int) -> None:
        print(f"  [{current}/{total}] Fetching data for {repo} ...")

    print("Starting GitHub data collection for fusion-e/fusion* repos ...")
    all_data = fetch_all_data(progress_callback=_progress)
    save_cache(all_data)
    total_prs = sum(len(prs) for prs in all_data.values())
    print(f"Done. {total_prs} PRs across {len(all_data)} repos cached.")
