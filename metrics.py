"""
PR Metrics Analysis Module

Analyzes PR data from the GitHub API and computes cycle-time metrics
and bottleneck flags.
"""

from datetime import datetime, timezone
import statistics
from typing import Any


def _parse_datetime(dt_string: str | None) -> datetime | None:
    """Parse an ISO datetime string, handling the 'Z' suffix."""
    if dt_string is None:
        return None
    # GitHub API returns 'Z' suffix for UTC; replace with +00:00 for fromisoformat
    dt_string = dt_string.replace("Z", "+00:00")
    return datetime.fromisoformat(dt_string)


def _hours_between(start: datetime | None, end: datetime | None) -> float | None:
    """Compute hours between two datetimes. Returns None if either is None."""
    if start is None or end is None:
        return None
    delta = end - start
    return delta.total_seconds() / 3600.0


def _safe_avg(values: list[float]) -> float:
    """Compute the average of a list, returning 0.0 if empty."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _safe_median(values: list[float]) -> float:
    """Compute the median of a list, returning 0.0 if empty."""
    if not values:
        return 0.0
    return statistics.median(values)


def compute_pr_metrics(pr_data: dict[str, Any]) -> dict[str, Any]:
    """
    Compute cycle-time metrics for a single PR.

    Args:
        pr_data: A PR dict from the GitHub API, augmented with
                 'reviews', 'timeline_events', 'review_comments',
                 and 'issue_comments' fields.

    Returns:
        A metrics dict with timing breakdowns and bottleneck flags.
    """
    created_at = _parse_datetime(pr_data.get("created_at"))
    merged_at = _parse_datetime(pr_data.get("merged_at"))
    closed_at = _parse_datetime(pr_data.get("closed_at"))
    is_merged = merged_at is not None

    # Determine the effective end time for cycle time calculation
    if merged_at is not None:
        end_time = merged_at
    elif closed_at is not None:
        end_time = closed_at
    else:
        end_time = datetime.now(timezone.utc)

    total_cycle_time_hours = _hours_between(created_at, end_time)
    if total_cycle_time_hours is None:
        total_cycle_time_hours = 0.0

    # Determine state
    if is_merged:
        state = "merged"
    elif pr_data.get("state") == "closed":
        state = "closed"
    else:
        state = pr_data.get("state", "open")

    # --- Review analysis ---
    reviews = pr_data.get("reviews", []) or []

    # Sort reviews by submitted_at
    sorted_reviews = sorted(
        [r for r in reviews if r.get("submitted_at")],
        key=lambda r: r["submitted_at"],
    )

    # First review submission time
    first_review_dt = None
    if sorted_reviews:
        first_review_dt = _parse_datetime(sorted_reviews[0]["submitted_at"])

    time_to_first_review_hours = _hours_between(created_at, first_review_dt)

    # First APPROVED review
    first_approval_dt = None
    for review in sorted_reviews:
        if review.get("state") == "APPROVED":
            first_approval_dt = _parse_datetime(review["submitted_at"])
            break

    first_review_to_approval_hours = _hours_between(first_review_dt, first_approval_dt)
    approval_to_merge_hours = _hours_between(first_approval_dt, merged_at)

    # Unattributed time: total cycle time minus the known phase segments.
    # Covers rework after review, CI wait time, and any gaps where no
    # formal approval was recorded before merge.
    attributed = sum(
        v for v in (
            time_to_first_review_hours,
            first_review_to_approval_hours,
            approval_to_merge_hours,
        )
        if v is not None
    )
    unattributed_hours = max(total_cycle_time_hours - attributed, 0.0)

    # Review rounds: count of CHANGES_REQUESTED reviews
    review_rounds = sum(
        1 for r in sorted_reviews if r.get("state") == "CHANGES_REQUESTED"
    )

    # Number of reviews
    num_reviews = len(reviews)

    # Comments: review_comments + issue_comments
    review_comments = pr_data.get("review_comments", []) or []
    issue_comments = pr_data.get("issue_comments", []) or []
    num_comments = len(review_comments) + len(issue_comments)

    # Unique reviewers
    reviewer_logins = list(
        {
            r["user"]["login"]
            for r in reviews
            if r.get("user") and r["user"].get("login")
        }
    )
    num_reviewers = len(reviewer_logins)

    # Build partial metrics (without bottlenecks) so we can pass to detect_bottlenecks
    metrics = {
        "number": pr_data.get("number"),
        "pr_number": pr_data.get("number"),
        "title": pr_data.get("title", ""),
        "author": pr_data.get("user", {}).get("login", ""),
        "state": state,
        "is_merged": is_merged,
        "created_at": pr_data.get("created_at"),
        "merged_at": pr_data.get("merged_at"),
        "closed_at": pr_data.get("closed_at"),
        "head_branch": pr_data.get("head", {}).get("ref", ""),
        "base_branch": pr_data.get("base", {}).get("ref", ""),
        "additions": pr_data.get("additions", 0),
        "deletions": pr_data.get("deletions", 0),
        "changed_files": pr_data.get("changed_files", 0),
        "total_cycle_time_hours": round(total_cycle_time_hours, 2),
        "time_to_first_review_hours": (
            round(time_to_first_review_hours, 2)
            if time_to_first_review_hours is not None
            else None
        ),
        "first_review_to_approval_hours": (
            round(first_review_to_approval_hours, 2)
            if first_review_to_approval_hours is not None
            else None
        ),
        "approval_to_merge_hours": (
            round(approval_to_merge_hours, 2)
            if approval_to_merge_hours is not None
            else None
        ),
        "unattributed_hours": round(unattributed_hours, 2),
        "review_rounds": review_rounds,
        "num_reviews": num_reviews,
        "num_comments": num_comments,
        "num_reviewers": num_reviewers,
        "reviewers": reviewer_logins,
        # Commit & check data (passed through from collector)
        "total_commits": len(pr_data["commits"]) if isinstance(pr_data.get("commits"), list) else (pr_data.get("commits") or 0),
        "commits": pr_data["commits"] if isinstance(pr_data.get("commits"), list) else [],
        "checks": pr_data.get("checks") if isinstance(pr_data.get("checks"), dict) else {
            "total": 0, "success": 0, "failure": 0,
            "pending": 0, "overall_state": "unknown", "checks": [],
        },
        # Requested reviewers (passthrough for reminder system)
        "requested_reviewers": [
            r.get("login", "") for r in (pr_data.get("requested_reviewers") or [])
        ],
    }

    # Detect and attach bottlenecks
    metrics["bottlenecks"] = detect_bottlenecks(metrics)

    return metrics


def detect_bottlenecks(metrics: dict[str, Any]) -> list[dict[str, str]]:
    """
    Detect bottleneck flags from a PR metrics dict.

    Args:
        metrics: A metrics dict as produced by compute_pr_metrics
                 (may not yet contain the 'bottlenecks' key).

    Returns:
        A list of bottleneck flag dicts, each with 'type', 'severity',
        and 'description' keys.
    """
    bottlenecks: list[dict[str, str]] = []

    # --- slow_first_review: >24h medium, >72h high ---
    tfr = metrics.get("time_to_first_review_hours")
    if tfr is not None:
        if tfr > 72:
            bottlenecks.append(
                {
                    "type": "slow_first_review",
                    "severity": "high",
                    "description": (
                        f"First review took {tfr:.1f} hours (>72h threshold)."
                    ),
                }
            )
        elif tfr > 24:
            bottlenecks.append(
                {
                    "type": "slow_first_review",
                    "severity": "medium",
                    "description": (
                        f"First review took {tfr:.1f} hours (>24h threshold)."
                    ),
                }
            )

    # --- slow_approval: >48h medium, >120h high (first review to approval) ---
    fra = metrics.get("first_review_to_approval_hours")
    if fra is not None:
        if fra > 120:
            bottlenecks.append(
                {
                    "type": "slow_approval",
                    "severity": "high",
                    "description": (
                        f"Approval took {fra:.1f} hours after first review "
                        f"(>120h threshold)."
                    ),
                }
            )
        elif fra > 48:
            bottlenecks.append(
                {
                    "type": "slow_approval",
                    "severity": "medium",
                    "description": (
                        f"Approval took {fra:.1f} hours after first review "
                        f"(>48h threshold)."
                    ),
                }
            )

    # --- slow_merge: >24h medium, >72h high (approval to merge) ---
    atm = metrics.get("approval_to_merge_hours")
    if atm is not None:
        if atm > 72:
            bottlenecks.append(
                {
                    "type": "slow_merge",
                    "severity": "high",
                    "description": (
                        f"Merge took {atm:.1f} hours after approval "
                        f"(>72h threshold)."
                    ),
                }
            )
        elif atm > 24:
            bottlenecks.append(
                {
                    "type": "slow_merge",
                    "severity": "medium",
                    "description": (
                        f"Merge took {atm:.1f} hours after approval "
                        f"(>24h threshold)."
                    ),
                }
            )

    # --- excessive_review_rounds: >=3 medium, >=5 high ---
    rr = metrics.get("review_rounds", 0)
    if rr >= 5:
        bottlenecks.append(
            {
                "type": "excessive_review_rounds",
                "severity": "high",
                "description": (
                    f"PR went through {rr} rounds of changes requested "
                    f"(>=5 threshold)."
                ),
            }
        )
    elif rr >= 3:
        bottlenecks.append(
            {
                "type": "excessive_review_rounds",
                "severity": "medium",
                "description": (
                    f"PR went through {rr} rounds of changes requested "
                    f"(>=3 threshold)."
                ),
            }
        )

    # --- stale_pr: open >7 days medium, >30 days high ---
    state = metrics.get("state", "")
    if state == "open":
        cycle_hours = metrics.get("total_cycle_time_hours", 0)
        cycle_days = cycle_hours / 24.0 if cycle_hours else 0
        if cycle_days > 30:
            bottlenecks.append(
                {
                    "type": "stale_pr",
                    "severity": "high",
                    "description": (
                        f"PR has been open for {cycle_days:.1f} days "
                        f"(>30 days threshold)."
                    ),
                }
            )
        elif cycle_days > 7:
            bottlenecks.append(
                {
                    "type": "stale_pr",
                    "severity": "medium",
                    "description": (
                        f"PR has been open for {cycle_days:.1f} days "
                        f"(>7 days threshold)."
                    ),
                }
            )

    # --- large_pr: >500 lines changed medium, >1000 high ---
    additions = metrics.get("additions", 0) or 0
    deletions = metrics.get("deletions", 0) or 0
    total_lines = additions + deletions
    if total_lines > 1000:
        bottlenecks.append(
            {
                "type": "large_pr",
                "severity": "high",
                "description": (
                    f"PR changes {total_lines} lines "
                    f"(>1000 lines threshold)."
                ),
            }
        )
    elif total_lines > 500:
        bottlenecks.append(
            {
                "type": "large_pr",
                "severity": "medium",
                "description": (
                    f"PR changes {total_lines} lines "
                    f"(>500 lines threshold)."
                ),
            }
        )

    # --- unstable_build: Jenkins build(s) returned UNSTABLE ---
    checks_data = metrics.get("checks") or {}
    unstable_checks = [
        c for c in checks_data.get("checks", [])
        if (c.get("jenkins_result") or "").upper() == "UNSTABLE"
    ]
    if len(unstable_checks) >= 2:
        names = ", ".join(
            c.get("name", "").replace("continuous-integration/jenkins/", "")
            for c in unstable_checks
        )
        bottlenecks.append(
            {
                "type": "unstable_build",
                "severity": "high",
                "description": (
                    f"{len(unstable_checks)} Jenkins builds are unstable"
                    f" ({names})."
                ),
            }
        )
    elif len(unstable_checks) == 1:
        name = unstable_checks[0].get("name", "").replace(
            "continuous-integration/jenkins/", ""
        )
        bottlenecks.append(
            {
                "type": "unstable_build",
                "severity": "medium",
                "description": (
                    f"Jenkins build is unstable ({name})."
                ),
            }
        )

    return bottlenecks


def compute_repo_summary(
    repo_name: str, prs_metrics: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Compute aggregate summary statistics for a repository.

    Args:
        repo_name: The repository name (e.g. 'org/repo').
        prs_metrics: A list of PR metrics dicts as produced by
                     compute_pr_metrics.

    Returns:
        A summary dict with counts, averages, medians, bottleneck info,
        and the top 5 longest PRs by cycle time.
    """
    total_prs = len(prs_metrics)
    open_prs = sum(1 for m in prs_metrics if m.get("state") == "open")
    merged_prs = sum(1 for m in prs_metrics if m.get("is_merged"))
    closed_prs = sum(
        1 for m in prs_metrics if m.get("state") == "closed" and not m.get("is_merged")
    )

    # Collect non-None values for averaging
    cycle_times = [
        m["total_cycle_time_hours"]
        for m in prs_metrics
        if m.get("total_cycle_time_hours") is not None
    ]
    first_review_times = [
        m["time_to_first_review_hours"]
        for m in prs_metrics
        if m.get("time_to_first_review_hours") is not None
    ]
    # avg_time_to_merge_hours: total cycle time for merged PRs only
    merge_times = [
        m["total_cycle_time_hours"]
        for m in prs_metrics
        if m.get("is_merged") and m.get("total_cycle_time_hours") is not None
    ]

    # PRs with at least one bottleneck
    prs_with_bottlenecks = sum(
        1 for m in prs_metrics if m.get("bottlenecks")
    )

    # Top bottleneck types by count
    bottleneck_type_counts: dict[str, int] = {}
    for m in prs_metrics:
        for b in m.get("bottlenecks", []):
            btype = b.get("type", "unknown")
            bottleneck_type_counts[btype] = bottleneck_type_counts.get(btype, 0) + 1

    # Top 5 longest PRs by cycle time
    sorted_by_cycle = sorted(
        prs_metrics,
        key=lambda m: m.get("total_cycle_time_hours") or 0,
        reverse=True,
    )
    longest_prs = [
        {
            "pr_number": m["pr_number"],
            "title": m["title"],
            "total_cycle_time_hours": m["total_cycle_time_hours"],
        }
        for m in sorted_by_cycle[:5]
    ]

    return {
        "repo": repo_name,
        "repo_name": repo_name,
        "total_prs": total_prs,
        "open_prs": open_prs,
        "open": open_prs,
        "merged_prs": merged_prs,
        "merged": merged_prs,
        "closed_prs": closed_prs,
        "avg_cycle_time_hours": round(_safe_avg(cycle_times), 2),
        "median_cycle_time_hours": round(_safe_median(cycle_times), 2),
        "avg_time_to_first_review_hours": round(
            _safe_avg(first_review_times), 2
        ),
        "median_time_to_first_review_hours": round(
            _safe_median(first_review_times), 2
        ),
        "avg_time_to_merge_hours": round(_safe_avg(merge_times), 2),
        "prs_with_bottlenecks": prs_with_bottlenecks,
        "bottleneck_count": prs_with_bottlenecks,
        "top_bottleneck_types": bottleneck_type_counts,
        "longest_prs": longest_prs,
    }


def compute_all_metrics(
    all_data: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """
    Compute metrics for all repositories.

    Args:
        all_data: A dict mapping repo names to lists of PR dicts
                  (as returned by the GitHub API, augmented with
                  reviews, timeline_events, review_comments, and
                  issue_comments).

    Returns:
        A dict with:
          - 'repo_summaries': list of repo summary dicts
          - 'pr_metrics': dict mapping repo name to list of
                          PR metrics dicts
          - 'bottlenecks': flat list of bottleneck dicts with
                           repo/PR context
    """
    all_pr_metrics: dict[str, list[dict[str, Any]]] = {}
    repo_summaries: list[dict[str, Any]] = []
    all_bottlenecks: list[dict[str, Any]] = []

    for repo_name, prs in all_data.items():
        pr_metrics_list = [compute_pr_metrics(pr) for pr in prs]
        all_pr_metrics[repo_name] = pr_metrics_list
        repo_summaries.append(
            compute_repo_summary(repo_name, pr_metrics_list)
        )
        # Collect bottlenecks from each PR, tagging with repo/PR info
        for pm in pr_metrics_list:
            for b in pm.get("bottlenecks", []):
                all_bottlenecks.append({
                    "repo": repo_name,
                    "pr_number": pm.get("pr_number"),
                    "pr_title": pm.get("title", ""),
                    "bottleneck_type": b.get("type", ""),
                    "severity": b.get("severity", ""),
                    "description": b.get("description", ""),
                })

    return {
        "repo_summaries": repo_summaries,
        "pr_metrics": all_pr_metrics,
        "bottlenecks": all_bottlenecks,
    }
