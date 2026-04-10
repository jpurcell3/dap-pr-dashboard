"""Jenkins API client for fetching build details, pipeline stages, and test reports.

Build URLs are extracted from GitHub commit-status ``target_url`` fields
(context ``continuous-integration/jenkins/*``).  Credentials come from
environment variables ``JENKINS_USER`` and ``JENKINS_API_TOKEN``.

This module is intentionally lightweight — it does **not** depend on the
``python-jenkins`` package.  All calls use plain ``requests``.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any

import requests
import urllib3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SSL_VERIFY = os.environ.get("SSL_VERIFY", "false").lower() not in ("false", "0", "no")
HTTP_TIMEOUT = 15  # seconds

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _get_credentials() -> tuple[str, str] | None:
    """Return ``(user, token)`` or *None* if Jenkins is not configured."""
    user = os.environ.get("JENKINS_USER", "").strip()
    token = os.environ.get("JENKINS_API_TOKEN", "").strip()
    if user and token:
        return (user, token)
    return None


def is_configured() -> bool:
    """Return *True* if Jenkins credentials are present in the environment."""
    return _get_credentials() is not None


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_REDIRECT_SUFFIX = re.compile(r"/display/redirect/?$", re.IGNORECASE)
_CONSOLE_SUFFIX = re.compile(r"/console/?$", re.IGNORECASE)


def normalize_build_url(raw_url: str) -> str:
    """Strip trailing ``/display/redirect`` or ``/console`` from a Jenkins URL.

    Returns a clean build URL like ``https://host/job/Foo/job/Bar/42``.
    """
    url = raw_url.rstrip("/")
    url = _REDIRECT_SUFFIX.sub("", url)
    url = _CONSOLE_SUFFIX.sub("", url)
    return url


def _api_url(build_url: str, path: str = "") -> str:
    """Build a Jenkins JSON-API URL from a normalised build URL."""
    base = normalize_build_url(build_url)
    if path:
        return f"{base}/{path}"
    return f"{base}/api/json"


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _get_json(url: str, auth: tuple[str, str], params: dict | None = None) -> dict | None:
    """GET *url* as JSON, returning *None* on any failure."""
    try:
        resp = requests.get(
            url, auth=auth, params=params,
            verify=SSL_VERIFY, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.debug("Jenkins API %s returned %s", url, resp.status_code)
    except Exception as exc:
        logger.debug("Jenkins API error for %s: %s", url, exc)
    return None


def fetch_build_info(build_url: str) -> dict[str, Any] | None:
    """Fetch core build metadata (result, duration, timestamp).

    Returns a dict with keys: ``result``, ``duration_ms``, ``timestamp_ms``,
    ``display_name``, ``building``.  Returns *None* if unreachable.
    """
    auth = _get_credentials()
    if not auth:
        return None

    url = _api_url(build_url)
    data = _get_json(
        url, auth,
        params={"tree": "result,duration,timestamp,displayName,building"},
    )
    if not data:
        return None

    return {
        "result": data.get("result") or ("BUILDING" if data.get("building") else "UNKNOWN"),
        "duration_ms": data.get("duration", 0),
        "timestamp_ms": data.get("timestamp", 0),
        "display_name": data.get("displayName", ""),
        "building": bool(data.get("building")),
    }


def fetch_stages(build_url: str) -> list[dict[str, Any]]:
    """Fetch pipeline stage breakdown via the Workflow API.

    Returns a list of dicts, each with ``name``, ``status``, ``duration_ms``.
    Returns an empty list on failure or if the build isn't a Pipeline.
    """
    auth = _get_credentials()
    if not auth:
        return []

    url = f"{normalize_build_url(build_url)}/wfapi/describe"
    data = _get_json(url, auth)
    if not data:
        return []

    stages = []
    for s in data.get("stages", []):
        stages.append({
            "name": s.get("name", ""),
            "status": s.get("status", "UNKNOWN"),
            "duration_ms": s.get("durationMillis", 0),
        })
    return stages


def fetch_test_report(build_url: str) -> dict[str, Any] | None:
    """Fetch aggregated test report for a build.

    Returns a dict with ``pass_count``, ``fail_count``, ``skip_count``,
    ``total_count``, ``duration_s``, and ``failures`` (list of failed test
    dicts with ``name``, ``suite``, ``error_details``, ``duration_s``).
    Returns *None* if no test report exists.
    """
    auth = _get_credentials()
    if not auth:
        return None

    url = f"{normalize_build_url(build_url)}/testReport/api/json"
    data = _get_json(
        url, auth,
        params={
            "tree": (
                "failCount,passCount,skipCount,totalCount,duration,"
                "suites[name,cases[name,className,status,duration,errorDetails]]"
            ),
        },
    )
    if not data:
        return None

    failures: list[dict[str, Any]] = []
    for suite in data.get("suites", []):
        suite_name = suite.get("name", "")
        for case in suite.get("cases", []):
            status = (case.get("status") or "").upper()
            if status in ("FAILED", "REGRESSION", "ERROR"):
                failures.append({
                    "name": case.get("name", ""),
                    "suite": suite_name,
                    "class_name": case.get("className", ""),
                    "error_details": (case.get("errorDetails") or "")[:500],
                    "duration_s": case.get("duration", 0),
                })

    total = data.get("totalCount", 0)
    if total == 0:
        total = (data.get("passCount", 0) + data.get("failCount", 0)
                 + data.get("skipCount", 0))

    return {
        "pass_count": data.get("passCount", 0),
        "fail_count": data.get("failCount", 0),
        "skip_count": data.get("skipCount", 0),
        "total_count": total,
        "duration_s": data.get("duration", 0),
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# High-level: fetch everything for a build
# ---------------------------------------------------------------------------

def fetch_build_details(build_url: str) -> dict[str, Any] | None:
    """Fetch all available Jenkins data for a single build URL.

    Returns a combined dict with ``build``, ``stages``, ``test_report`` keys,
    or *None* if Jenkins is unreachable / not configured.
    """
    if not is_configured():
        return None

    norm_url = normalize_build_url(build_url)
    build_info = fetch_build_info(norm_url)
    if not build_info:
        return None

    stages = fetch_stages(norm_url)
    test_report = fetch_test_report(norm_url)

    return {
        "build_url": norm_url,
        "build": build_info,
        "stages": stages,
        "test_report": test_report,
    }
