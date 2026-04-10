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
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
REPO_FILTER = os.environ.get("GITHUB_REPO_FILTER", "").strip()
SSL_VERIFY = os.environ.get("SSL_VERIFY", "false").lower() not in ("false", "0", "no")
DEFAULT_CACHE_PATH = Path(__file__).parent / "pr_cache.json"
DEFAULT_PR_LOOKBACK_DAYS = int(os.environ.get("DEFAULT_PR_LOOKBACK_DAYS", "7"))
MAX_PRS_PER_REPO = int(os.environ.get("MAX_PRS_PER_REPO", "500"))

# Concurrency settings — control parallelism during refresh.
# MAX_CONCURRENT_REPOS: how many repos to enrich in parallel.
# MAX_CONCURRENT_PRS: how many PRs to enrich in parallel *within* a repo.
# Keep the product modest to avoid rate-limit exhaustion.
MAX_CONCURRENT_REPOS = int(os.environ.get("MAX_CONCURRENT_REPOS", "4"))
MAX_CONCURRENT_PRS = int(os.environ.get("MAX_CONCURRENT_PRS", "6"))

# HTTP timeout as (connect, read) in seconds.  A short connect timeout
# catches unreachable hosts quickly; the read timeout caps how long we
# wait for a response body.
HTTP_TIMEOUT = (10, 20)

logger.info(
    "Config loaded — API: %s | Org: %s | Repo filter: %r | SSL verify: %s",
    GITHUB_API_BASE, ORG_NAME, REPO_FILTER or "(none)", SSL_VERIFY,
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

# Shared rate-limit state accessible from other modules (e.g. app.py).
# When REDIS_URL is set, reads/writes go through Redis; otherwise in-memory.
from redis_state import rate_limit_set, rate_limit_get, rate_limit_bulk_set

# Keep the dict around as an importable alias for backward compat (read-only
# snapshot used only by app.py's old import; app.py no longer imports this).
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
    rate_limit_bulk_set({
        "remaining": remaining,
        "limit": int(limit) if limit else None,
        "used": int(used) if used else None,
        "reset_at": int(reset_at) if reset_at else None,
    })

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

        rate_limit_bulk_set({
            "is_throttled": True,
            "throttled_until": reset_time,
        })

        logger.warning(
            "Rate limit nearly exhausted (%d remaining). "
            "Sleeping for %d seconds until %s.",
            remaining,
            sleep_seconds,
            reset_time,
        )
        time.sleep(sleep_seconds)
        rate_limit_bulk_set({
            "is_throttled": False,
            "throttled_until": None,
        })


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
        _check_cancelled()
        response = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT, verify=SSL_VERIFY)
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

def get_filtered_repos(token: str | None = None) -> list[str]:
    """Return a sorted list of repo names in the configured org.

    When ``GITHUB_REPO_FILTER`` is set, only repos whose name matches the
    regex (case-insensitive search) are returned.  When unset/empty, **all**
    repos in the org are returned.
    """
    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/orgs/{ORG_NAME}/repos"
    params = {"per_page": 100, "type": "all"}

    all_repos = _get_paginated(url, headers, params)
    if REPO_FILTER:
        pattern = re.compile(REPO_FILTER, re.IGNORECASE)
        filtered_repos = sorted(
            repo["name"]
            for repo in all_repos
            if repo.get("name") and pattern.search(repo["name"])
        )
    else:
        filtered_repos = sorted(repo["name"] for repo in all_repos if repo.get("name"))
    logger.info(
        "Found %d repos in %s (filter=%r).", len(filtered_repos), ORG_NAME, REPO_FILTER or "(none)"
    )
    return filtered_repos


def get_all_org_repos(token: str | None = None) -> list[str]:
    """Return a sorted list of *every* repo name in the configured org."""
    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/orgs/{ORG_NAME}/repos"
    params = {"per_page": 100, "type": "all"}

    all_repos = _get_paginated(url, headers, params)
    names = sorted(repo["name"] for repo in all_repos if repo.get("name"))
    logger.info("Found %d total repos in %s.", len(names), ORG_NAME)
    return names


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
            _check_cancelled()
            response = requests.get(
                current_url, headers=headers, params=current_params,
                timeout=HTTP_TIMEOUT, verify=SSL_VERIFY,
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

    # Fire both paginated requests concurrently.
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_review = pool.submit(_get_paginated, review_comments_url, headers)
        future_issue = pool.submit(_get_paginated, issue_comments_url, headers)

        try:
            review_comments = future_review.result()
        except requests.HTTPError as exc:
            logger.error(
                "Failed to fetch review comments for %s#%d: %s",
                repo_name,
                pr_number,
                exc,
            )

        try:
            issue_comments = future_issue.result()
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
# Commit & check-run helpers
# ---------------------------------------------------------------------------

def fetch_pr_commits(
    repo_name: str,
    pr_number: int,
    token: str | None = None,
) -> list[dict]:
    """Fetch all commits for a pull request.

    Returns a list of commit dicts, each containing:
    ``sha``, ``message``, ``author_name``, ``author_login``, ``date``.
    """
    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}/pulls/{pr_number}/commits"

    try:
        raw = _get_paginated(url, headers)
    except requests.HTTPError as exc:
        logger.error(
            "Failed to fetch commits for %s#%d: %s", repo_name, pr_number, exc
        )
        return []

    commits: list[dict] = []
    for c in raw:
        commits.append({
            "sha": c.get("sha", ""),
            "message": (c.get("commit", {}).get("message") or "").split("\n", 1)[0],
            "author_name": c.get("commit", {}).get("author", {}).get("name", ""),
            "author_login": (c.get("author") or {}).get("login", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
        })
    return commits


def fetch_commit_checks(
    repo_name: str,
    sha: str,
    token: str | None = None,
) -> dict:
    """Fetch check-runs and combined status for a single commit.

    Returns a summary dict with ``total``, ``success``, ``failure``,
    ``pending``, ``checks`` (list of individual results), and
    ``overall_state``.
    """
    headers = _build_headers(token)

    def _fetch_check_runs() -> list[dict]:
        """GitHub Apps check-runs."""
        results: list[dict] = []
        try:
            url = (
                f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}"
                f"/commits/{sha}/check-runs"
            )
            headers_cr = {**headers, "Accept": "application/vnd.github.v3+json"}
            resp = requests.get(
                url, headers=headers_cr, verify=SSL_VERIFY, timeout=HTTP_TIMEOUT
            )
            _check_rate_limit(resp)
            resp.raise_for_status()
            data = resp.json()
            for cr in data.get("check_runs", []):
                conclusion = (cr.get("conclusion") or "pending").lower()
                check_name = cr.get("name", "")
                raw_summary = (cr.get("output") or {}).get("summary", "")
                raw_title = (cr.get("output") or {}).get("title", "")
                # DRP Checkers: keep only the overall status + failed sub-checks.
                if _is_drp_checker(check_name):
                    summary_text = _extract_drp_failures(raw_summary)
                # Ticket check: extract structured summary with error table.
                elif _is_ticket_check(check_name):
                    summary_text = _extract_ticket_summary(raw_summary)
                # SonarQube: extract Quality Gate section only.
                elif _is_sonarqube_check(check_name):
                    summary_text = _extract_sonarqube_summary(raw_summary)
                # Security scan checks: extract only the Summary section,
                # discarding verbose tool descriptions and boilerplate.
                elif _is_summary_only_check(check_name):
                    summary_text = _extract_scan_summary(raw_summary)
                else:
                    summary_text = _strip_html(raw_summary)
                # Drop the output title when it just repeats the check name
                # (e.g. Twistlock sets title="twistlock") to avoid duplication.
                output_title = "" if raw_title.strip().lower() == check_name.strip().lower() else raw_title
                results.append({
                    "name": check_name,
                    "status": cr.get("status", ""),
                    "conclusion": conclusion,
                    "started_at": cr.get("started_at", ""),
                    "completed_at": cr.get("completed_at", ""),
                    "details_url": cr.get("details_url") or cr.get("html_url", ""),
                    "output_title": output_title,
                    "output_summary": summary_text,
                })
        except Exception:
            logger.debug(
                "Failed to fetch check-runs for %s@%s", repo_name, sha[:8],
                exc_info=True,
            )
        return results

    def _fetch_commit_statuses() -> list[dict]:
        """Classic commit statuses."""
        results: list[dict] = []
        try:
            url = (
                f"{GITHUB_API_BASE}/repos/{ORG_NAME}/{repo_name}"
                f"/commits/{sha}/status"
            )
            resp = requests.get(url, headers=headers, verify=SSL_VERIFY, timeout=HTTP_TIMEOUT)
            _check_rate_limit(resp)
            resp.raise_for_status()
            status_data = resp.json()
            for s in status_data.get("statuses", []):
                results.append({
                    "name": s.get("context", ""),
                    "status": "completed",
                    "conclusion": s.get("state", "pending"),
                    "details_url": s.get("target_url", ""),
                    "output_title": s.get("description", ""),
                    "output_summary": "",
                })
        except Exception:
            logger.debug(
                "Failed to fetch commit status for %s@%s", repo_name, sha[:8],
                exc_info=True,
            )
        return results

    # Fire both requests concurrently.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_cr = pool.submit(_fetch_check_runs)
        f_st = pool.submit(_fetch_commit_statuses)
        checks: list[dict] = f_cr.result() + f_st.result()

    # --- Build summary --------------------------------------------------------
    total = len(checks)
    success = sum(1 for c in checks if c["conclusion"] in ("success", "neutral", "skipped"))
    failure = sum(1 for c in checks if c["conclusion"] in ("failure", "cancelled", "timed_out", "action_required"))
    pending = total - success - failure

    # Overall state: failure if any failed, pending if any pending, else success
    if failure > 0:
        overall = "failure"
    elif pending > 0:
        overall = "pending"
    elif total > 0:
        overall = "success"
    else:
        overall = "unknown"

    return {
        "total": total,
        "success": success,
        "failure": failure,
        "pending": pending,
        "overall_state": overall,
        "checks": checks,
    }


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string (best-effort)."""
    return re.sub(r"<[^>]+>", "", text).strip()


# Check names (lowercased) whose output should be reduced to the Summary
# section only.  Uses substring matching so e.g. "blackduck" catches all
# Blackduck variants.
_SUMMARY_ONLY_KEYWORDS: list[str] = [
    "twistlock",
    "secrets scanner",
    "checkmarx",
    "blackduck",
    "black duck",
    "mcafee",
    "acronix",
    "powerapi-linter",
]


def _is_summary_only_check(check_name: str) -> bool:
    """Return True if *check_name* is one of the security-scan checks whose
    output should be trimmed to the Summary section only."""
    lower = check_name.lower()
    return any(kw in lower for kw in _SUMMARY_ONLY_KEYWORDS)


def _is_sonarqube_check(check_name: str) -> bool:
    """Return True if *check_name* is a SonarQube / SonarCloud check."""
    return "sonar" in check_name.lower()


def _strip_markdown(text: str) -> str:
    """Strip markdown image and link syntax, returning only visible text.

    * ``[![alt](img_url)](link_url)`` → removed (image-only links)
    * ``![alt](url)`` → removed (inline images)
    * ``[text](url)`` → ``text`` (keep link text)
    * ``##`` heading markers → removed
    """
    # Remove image-links: [![...](img)](url)
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", text)
    # Remove standalone images: ![...](url)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Convert text links to just the link text: [text](url) → text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove heading markers
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text


def _extract_sonarqube_summary(raw_summary: str) -> str:
    """Extract just the failed conditions from SonarQube check output.

    Returns a compact block like::

        Failed conditions
         11 Security Hotspots
         8.3% Duplication on New Code (required ≤ 3%)

    The ``output_title`` ("Quality Gate failed") is rendered separately in
    the UI, so this function only needs to produce the condition list.
    Returns empty string when no meaningful conditions are found.
    """
    if not raw_summary:
        return ""

    # Strip HTML first, then markdown syntax.
    text = _strip_markdown(_strip_html(raw_summary))

    lines = text.splitlines()

    # Look for the "Failed conditions" section.
    start = -1
    for i, ln in enumerate(lines):
        if "failed conditions" in ln.lower():
            start = i
            break

    if start == -1:
        # No failed-conditions block (e.g. promotional text only).
        return ""

    kept: list[str] = []
    for ln in lines[start:]:
        stripped = ln.strip()
        # Stop at boilerplate lines
        if re.match(r"^see analysis details", stripped, re.IGNORECASE):
            break
        if re.match(r"^catch issues before", stripped, re.IGNORECASE):
            break
        if not stripped:
            continue
        # "Failed conditions" becomes the header; condition lines get a leading space.
        if kept:
            kept.append(" " + stripped)
        else:
            kept.append(stripped)

    return "\n".join(kept) if kept else ""


def _is_ticket_check(check_name: str) -> bool:
    """Return True if *check_name* is the Ticket validation check."""
    return check_name.strip().lower() == "ticket"


def _extract_ticket_summary(raw_summary: str) -> str:
    """Extract structured data from a Ticket check output.

    Parses the raw HTML to pull the summary line and error table rows
    from the ``<details>`` section.  Returns a JSON string::

        {"type": "ticket",
         "summary": "2 errors, 0 warnings",
         "errors": [{"commit": "abc123", "message": "No tickets specified"}, ...]}

    The frontend can detect ``"type":"ticket"`` and render an inline
    table.  Falls back to plain text if parsing fails.
    """
    if not raw_summary:
        return ""

    # --- 1. Extract the summary line (e.g. "2 errors, 0 warnings") ---
    # Appears in bold: **Summary: 2 errors, 0 warnings**
    summary_line = ""
    m = re.search(r"\*{2}Summary:\s*(.+?)\*{2}", raw_summary)
    if m:
        summary_line = m.group(1).strip()
    else:
        # Fallback: look in stripped text
        text = _strip_html(raw_summary)
        m2 = re.search(r"(?:summary:\s*)?(\d+\s+error\S*.*?\d+\s+warning\S*)", text, re.IGNORECASE)
        if m2:
            summary_line = m2.group(1).strip()

    # --- 2. Extract the error table from the first <details> block ---
    errors: list[dict] = []
    # Find the Errors details section (contains "Errors" in the summary)
    details_match = re.search(
        r"<details>\s*<summary>.*?Error.*?</summary>\s*<table>(.*?)</table>",
        raw_summary, re.DOTALL | re.IGNORECASE,
    )
    if details_match:
        table_html = details_match.group(1)
        # Parse each <tr> with <td> cells (skip header rows with <th>)
        for row_match in re.finditer(r"<tr>((?:<td>.*?</td>)+)</tr>", table_html, re.DOTALL):
            cells = re.findall(r"<td>(.*?)</td>", row_match.group(1), re.DOTALL)
            if len(cells) >= 2:
                commit_sha = _strip_html(cells[0]).strip()
                message = _strip_html(cells[1]).strip()
                # Shorten SHA to 7 chars for display
                if re.match(r"^[0-9a-f]{8,40}$", commit_sha):
                    commit_sha = commit_sha[:7]
                errors.append({"commit": commit_sha, "message": message})

    # --- 3. Build result ---
    if summary_line or errors:
        result: dict = {"type": "ticket"}
        if summary_line:
            result["summary"] = summary_line
        if errors:
            result["errors"] = errors
        return json.dumps(result)

    # Fallback: plain text
    return _strip_html(raw_summary)


def _is_drp_checker(check_name: str) -> bool:
    """Return True if *check_name* looks like a DRP Checkers aggregate check."""
    return bool(re.search(r"drp.check", check_name, re.IGNORECASE))


def _extract_drp_failures(raw_summary: str) -> str:
    """Extract only the failed sub-check names from DRP Checkers output.

    The raw summary contains HTML with emoji/image markers for each
    sub-check (tick = pass, crossed = fail).  We parse the raw text
    *before* stripping HTML so markers stay associated with their
    component name.

    Returns a comma-separated list of failed check names, e.g.
    ``"Ticket"`` or ``"Ticket, checkmarx"``.  Returns empty string
    when all checks passed or the format cannot be parsed.
    """
    if not raw_summary:
        return ""

    # Work on the HTML-stripped text.  The GitHub check output renders
    # emoji as literal text or shortcodes after stripping.
    text = _strip_html(raw_summary)

    # Normalise whitespace but keep newlines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # ---- Identify the component-list section ----
    # Component lines look like:
    #   "tick  Blackduck Signature Checker Service"
    #   "crossed  Ticket"
    #   "✅  checkmarx"
    # We look for lines that START with a known marker word/emoji.
    _component_re = re.compile(
        r"^(?:"
        r"tick|crossed|timed_out\w*"                       # text markers
        r"|[\u2705\u2714\u2713\u2611"                      # ✅ ✔ ✓ ☑
        r"\u274C\u274E\u2716\u2718\u26D4"                  # ❌ ❎ ✖ ✘ ⛔
        r"\u23F3\u231B\u26A0\u2757\u2753"                  # ⏳ ⌛ ⚠ ❗ ❓
        r"\U0001F552\U0001F6D1]"                           # 🕒 🛑
        r"|:[a-z_]+:"                                      # :shortcode:
        r")\s+(.+)",
        re.IGNORECASE,
    )

    _pass_marker_re = re.compile(
        r"^(?:tick|[\u2705\u2714\u2713\u2611]"
        r"|:white_check_mark:|:heavy_check_mark:|:check:)",
        re.IGNORECASE,
    )

    failed: list[str] = []
    for ln in lines:
        m = _component_re.match(ln)
        if not m:
            continue
        # Skip lines that are clearly headers/status, not component entries
        component_name = m.group(1).strip()
        if not component_name or "overall status" in ln.lower():
            continue
        # If the marker is a pass marker, skip
        if _pass_marker_re.match(ln):
            continue
        failed.append(component_name)

    return ", ".join(failed)


def _extract_scan_summary(raw_summary: str) -> str:
    """Extract the key details from a security-scan check output.

    Tries several patterns in priority order:

    1. ``***Scan Summary Result:*** ***…***`` / ``***Recommended Action:*** ***…***``
       (Twistlock / Secrets Scanner / checkmarx style)
    2. A markdown ``## Summary`` (or ``### Summary``) section — returns
       everything from that heading until the next heading or end of text.
    3. A bold ``**Summary**`` or ``***Summary***`` label — returns the
       paragraph following it.
    4. Fallback: strip HTML and return the full text (unchanged behaviour).
    """
    if not raw_summary:
        return ""

    # --- Pattern 1: Twistlock-style bold markers ---
    scan_match = re.search(
        r"\*{2,3}Scan Summary Result:?\*{2,3}\s*\*{2,3}(.*?)\*{2,3}",
        raw_summary, re.DOTALL,
    )
    action_match = re.search(
        r"\*{2,3}Recommended Action:?\*{2,3}\s*\*{2,3}(.*?)\*{2,3}",
        raw_summary, re.DOTALL,
    )
    if scan_match or action_match:
        parts = []
        if scan_match:
            parts.append(scan_match.group(1).strip())
        if action_match:
            parts.append(action_match.group(1).strip())
        return "\n".join(parts)

    # --- Pattern 2: Markdown heading  ## Summary  /  ### Summary ---
    md_match = re.search(
        r"^#{2,4}\s+Summary\b[^\n]*\n(.*?)(?=^#{2,4}\s|\Z)",
        raw_summary, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if md_match:
        section = _strip_html(md_match.group(1)).strip()
        if section:
            return section

    # --- Pattern 3: Bold **Summary** or ***Summary*** inline label ---
    bold_match = re.search(
        r"\*{2,3}Summary:?\*{2,3}\s*(.*?)(?=\*{2,3}[A-Z]|\n\n|\Z)",
        raw_summary, re.DOTALL | re.IGNORECASE,
    )
    if bold_match:
        section = _strip_html(bold_match.group(1)).strip()
        if section:
            return section

    # --- Fallback: strip HTML, return full text ---
    return _strip_html(raw_summary)


# ---------------------------------------------------------------------------
# Aggregate fetch
# ---------------------------------------------------------------------------

def _enrich_single_pr(repo_name: str, pr: dict, token: str) -> dict:
    """Enrich a single PR dict with reviews, timeline, comments, commits, and checks.

    All five enrichment calls are fired concurrently using a
    :class:`ThreadPoolExecutor`.  The ``commits`` result is needed before
    ``checks`` can be fetched (we need the HEAD SHA), so commits is fetched
    in the same pool and checks is chained from its result.
    """
    pr_number: int = pr["number"]

    # --- helper closures (submitted to pool) --------------------------------
    def _reviews():
        _check_cancelled()
        try:
            return fetch_pr_reviews(repo_name, pr_number, token=token)
        except Exception:
            logger.error("Error fetching reviews for %s#%d", repo_name, pr_number, exc_info=True)
            return []

    def _timeline():
        _check_cancelled()
        try:
            return fetch_pr_timeline(repo_name, pr_number, token=token)
        except Exception:
            logger.error("Error fetching timeline for %s#%d", repo_name, pr_number, exc_info=True)
            return []

    def _comments():
        _check_cancelled()
        try:
            return fetch_pr_comments(repo_name, pr_number, token=token)
        except Exception:
            logger.error("Error fetching comments for %s#%d", repo_name, pr_number, exc_info=True)
            return {"review_comments": [], "issue_comments": []}

    def _commits():
        _check_cancelled()
        try:
            return fetch_pr_commits(repo_name, pr_number, token=token)
        except Exception:
            logger.error("Error fetching commits for %s#%d", repo_name, pr_number, exc_info=True)
            return []

    empty_checks = {"total": 0, "success": 0, "failure": 0,
                    "pending": 0, "overall_state": "unknown", "checks": []}

    # Submit the four independent calls concurrently.
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_reviews = pool.submit(_reviews)
        f_timeline = pool.submit(_timeline)
        f_comments = pool.submit(_comments)
        f_commits = pool.submit(_commits)

    _check_cancelled()
    pr["reviews"] = f_reviews.result()
    pr["timeline_events"] = f_timeline.result()
    comments = f_comments.result()
    pr["review_comments"] = comments["review_comments"]
    pr["issue_comments"] = comments["issue_comments"]
    pr["commits"] = f_commits.result()

    # Checks depend on commits (need HEAD SHA) — fetch sequentially after.
    _check_cancelled()
    try:
        if pr["commits"]:
            head_sha = pr["commits"][-1]["sha"]
            pr["checks"] = fetch_commit_checks(repo_name, head_sha, token=token)
        else:
            pr["checks"] = empty_checks
    except Exception:
        logger.error("Error fetching checks for %s#%d", repo_name, pr_number, exc_info=True)
        pr["checks"] = empty_checks

    return pr


def _enrich_repo(
    repo_name: str,
    token: str,
    since: str | None,
) -> list[dict]:
    """Fetch and enrich all PRs for a single repo.

    PRs within the repo are enriched in parallel (up to
    ``MAX_CONCURRENT_PRS`` at a time).
    """
    _check_cancelled()
    prs = fetch_prs_for_repo(repo_name, token=token, since=since)
    if not prs:
        return []

    enriched: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PRS) as pool:
        futures = {
            pool.submit(_enrich_single_pr, repo_name, pr, token): pr
            for pr in prs
        }
        for future in as_completed(futures):
            _check_cancelled()
            enriched.append(future.result())

    return enriched


def fetch_all_data(
    repos: list[str] | None = None,
    progress_callback=None,
    token: str | None = None,
    since: str | None = None,
) -> dict[str, list[dict]]:
    """Fetch all PRs (with reviews, timeline, and comments) for fusion repos.

    Repos are processed in parallel (up to ``MAX_CONCURRENT_REPOS``).
    Within each repo, individual PRs are enriched concurrently (up to
    ``MAX_CONCURRENT_PRS``).

    Parameters
    ----------
    repos:
        Explicit list of repo names. If ``None``, discovers repos via
        :func:`get_filtered_repos`.
    progress_callback:
        Optional callable ``(repo_name, current_index, total_repos) -> None``
        invoked once per repo when it *completes* (ordering may differ from
        the input list due to parallelism).
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
        repos = get_filtered_repos(token=token)

    total = len(repos)
    result: dict[str, list[dict]] = {}

    # Clear cancel flag at start of a new fetch
    _cancel_event.clear()

    # Track completion order for progress reporting.
    _completed_count = [0]  # mutable container for closure access
    _lock = threading.Lock()

    def _repo_task(repo_name: str) -> tuple[str, list[dict]]:
        """Fetch + enrich a single repo; report progress on completion."""
        _check_cancelled()
        logger.info("Processing repo: %s", repo_name)
        enriched = _enrich_repo(repo_name, token, since)
        # Report progress (thread-safe counter).
        with _lock:
            _completed_count[0] += 1
            done = _completed_count[0]
        if progress_callback is not None:
            try:
                progress_callback(repo_name, done, total)
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)
        logger.info("Finished %s: %d PRs collected and enriched.", repo_name, len(enriched))
        return repo_name, enriched

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REPOS) as pool:
        futures = {pool.submit(_repo_task, rn): rn for rn in repos}
        for future in as_completed(futures):
            _check_cancelled()
            repo_name, enriched_prs = future.result()
            result[repo_name] = enriched_prs

    return result


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def save_cache(
    data: dict[str, list[dict]],
    filepath: str | Path = DEFAULT_CACHE_PATH,
) -> None:
    """Persist *data* as JSON to *filepath* (atomic write)."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file first, then rename for crash safety.
    tmp = filepath.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    # os.replace is atomic on POSIX; on Windows it's as close as we get.
    os.replace(tmp, filepath)
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
