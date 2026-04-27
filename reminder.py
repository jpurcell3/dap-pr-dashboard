"""
Reviewer Reminder System.

Identifies PRs with stale review requests and posts @mention reminder
comments on GitHub.  Designed to run as a background daemon thread after
each data sync (same pattern as Jenkins backfill).

Configuration (env vars / .env):
    REMINDER_ENABLED            – "true" to activate (default: false)
    REMINDER_THRESHOLD_HOURS    – hours before first reminder (default: 24)
    REMINDER_INTERVAL_HOURS     – hours between repeat reminders (default: 48)
    REMINDER_DRY_RUN            – "true" to log without posting (default: false)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REMINDER_ENABLED = os.environ.get("REMINDER_ENABLED", "false").lower() in ("1", "true", "yes")
REMINDER_THRESHOLD_HOURS = float(os.environ.get("REMINDER_THRESHOLD_HOURS", "24"))
REMINDER_INTERVAL_HOURS = float(os.environ.get("REMINDER_INTERVAL_HOURS", "48"))
REMINDER_DRY_RUN = os.environ.get("REMINDER_DRY_RUN", "false").lower() in ("1", "true", "yes")

# Persistent ledger of sent reminders (avoids duplicate posts across restarts).
_LEDGER_PATH = Path(__file__).parent / "reminder_ledger.json"

# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _load_ledger() -> dict:
    """Load the reminder ledger from disk.

    Returns a dict keyed by ``"<repo>/<pr_number>/<reviewer_login>"``
    with values being the ISO-8601 timestamp of the last reminder sent.
    """
    if _LEDGER_PATH.exists():
        try:
            return json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Corrupt reminder ledger — starting fresh.")
    return {}


def _save_ledger(ledger: dict) -> None:
    _LEDGER_PATH.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def _ledger_key(repo: str, pr_number: int, reviewer: str) -> str:
    return f"{repo}/{pr_number}/{reviewer}"


# ---------------------------------------------------------------------------
# Stale-review detection
# ---------------------------------------------------------------------------

def _parse_dt(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime."""
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def find_stale_reviews(raw_prs: dict, threshold_hours: float | None = None) -> list[dict]:
    """Scan all cached PRs and return a list of stale review requests.

    Each entry is a dict with keys:
        repo, pr_number, pr_title, pr_url, reviewer_login,
        requested_at (ISO str), waiting_hours (float)
    """
    if threshold_hours is None:
        threshold_hours = REMINDER_THRESHOLD_HOURS

    now = datetime.now(timezone.utc)
    stale: list[dict] = []

    for repo, prs in raw_prs.items():
        if not isinstance(prs, list):
            continue
        for pr in prs:
            if pr.get("state") != "open":
                continue

            pr_number = pr.get("number")
            pr_title = pr.get("title", "")
            pr_url = pr.get("html_url", "")

            # Determine who has already reviewed
            reviewed_by: set[str] = set()
            for review in (pr.get("reviews") or []):
                login = (review.get("user") or {}).get("login", "")
                if login:
                    reviewed_by.add(login)

            # Build a map: reviewer_login -> earliest request timestamp
            reviewer_requested_at: dict[str, datetime] = {}

            # Source 1: timeline review_requested events
            for evt in (pr.get("timeline_events") or []):
                if evt.get("event") != "review_requested":
                    continue
                reviewer = (evt.get("requested_reviewer") or {}).get("login", "")
                if not reviewer:
                    continue
                ts = _parse_dt(evt.get("created_at"))
                if ts and (reviewer not in reviewer_requested_at or ts < reviewer_requested_at[reviewer]):
                    reviewer_requested_at[reviewer] = ts

            # Source 2: requested_reviewers array (current snapshot from GH API)
            # These don't have timestamps, so use PR created_at as fallback.
            pr_created = _parse_dt(pr.get("created_at"))
            for rr in (pr.get("requested_reviewers") or []):
                login = rr.get("login", "")
                if login and login not in reviewer_requested_at:
                    reviewer_requested_at[login] = pr_created or now

            # Filter: remove anyone who already reviewed
            for reviewer, requested_at in reviewer_requested_at.items():
                if reviewer in reviewed_by:
                    continue
                waiting = (now - requested_at).total_seconds() / 3600
                if waiting >= threshold_hours:
                    stale.append({
                        "repo": repo,
                        "pr_number": pr_number,
                        "pr_title": pr_title,
                        "pr_url": pr_url,
                        "reviewer_login": reviewer,
                        "requested_at": requested_at.isoformat(),
                        "waiting_hours": round(waiting, 1),
                    })

    # Sort: longest-waiting first
    stale.sort(key=lambda x: x["waiting_hours"], reverse=True)
    return stale


# ---------------------------------------------------------------------------
# Comment posting
# ---------------------------------------------------------------------------

_COMMENT_TEMPLATE = (
    "**Review Reminder** — @{reviewer}, this PR has been waiting for your "
    "review for **{hours}** hours (requested {requested_at_human}).\n\n"
    "_Automated reminder from DAP PR Dashboard._"
)


def _format_comment(entry: dict) -> str:
    requested_at = _parse_dt(entry["requested_at"])
    human = requested_at.strftime("%b %d at %H:%M UTC") if requested_at else "unknown"
    return _COMMENT_TEMPLATE.format(
        reviewer=entry["reviewer_login"],
        hours=int(entry["waiting_hours"]),
        requested_at_human=human,
    )


def send_reminders(raw_prs: dict, token: str | None = None, dry_run: bool | None = None) -> dict:
    """Post reminder comments for all stale reviews.

    Returns a summary dict: {sent: int, skipped: int, errors: int, details: [...]}
    """
    from github_collector import post_pr_comment, get_github_token

    if dry_run is None:
        dry_run = REMINDER_DRY_RUN

    if token is None:
        token = get_github_token()

    stale = find_stale_reviews(raw_prs)
    if not stale:
        logger.info("Reminder: no stale reviews found.")
        return {"sent": 0, "skipped": 0, "errors": 0, "details": []}

    ledger = _load_ledger()
    now = datetime.now(timezone.utc)
    interval_secs = REMINDER_INTERVAL_HOURS * 3600

    sent = 0
    skipped = 0
    errors = 0
    details: list[dict] = []

    for entry in stale:
        key = _ledger_key(entry["repo"], entry["pr_number"], entry["reviewer_login"])

        # Check if we already sent a reminder recently
        last_sent_str = ledger.get(key)
        if last_sent_str:
            last_sent = _parse_dt(last_sent_str)
            if last_sent and (now - last_sent).total_seconds() < interval_secs:
                skipped += 1
                continue

        body = _format_comment(entry)
        detail = {
            "repo": entry["repo"],
            "pr_number": entry["pr_number"],
            "reviewer": entry["reviewer_login"],
            "waiting_hours": entry["waiting_hours"],
        }

        if dry_run:
            logger.info("Reminder [DRY RUN] %s#%d -> @%s (%dh waiting)",
                        entry["repo"], entry["pr_number"],
                        entry["reviewer_login"], int(entry["waiting_hours"]))
            detail["status"] = "dry_run"
            sent += 1
        else:
            try:
                post_pr_comment(entry["repo"], entry["pr_number"], body, token=token)
                logger.info("Reminder sent: %s#%d -> @%s (%dh waiting)",
                            entry["repo"], entry["pr_number"],
                            entry["reviewer_login"], int(entry["waiting_hours"]))
                detail["status"] = "sent"
                sent += 1
            except Exception as exc:
                logger.error("Reminder failed: %s#%d -> @%s: %s",
                             entry["repo"], entry["pr_number"],
                             entry["reviewer_login"], exc)
                detail["status"] = "error"
                detail["error"] = str(exc)
                errors += 1
                details.append(detail)
                continue

        # Record in ledger
        ledger[key] = now.isoformat()
        details.append(detail)

    _save_ledger(ledger)
    summary = {"sent": sent, "skipped": skipped, "errors": errors, "details": details}
    logger.info("Reminder summary: sent=%d  skipped=%d  errors=%d", sent, skipped, errors)
    return summary


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def reminder_is_enabled() -> bool:
    return REMINDER_ENABLED


def purge_ledger_for_pr(repo: str, pr_number: int) -> None:
    """Remove all ledger entries for a given PR (e.g. when it is merged)."""
    ledger = _load_ledger()
    prefix = f"{repo}/{pr_number}/"
    keys_to_remove = [k for k in ledger if k.startswith(prefix)]
    if keys_to_remove:
        for k in keys_to_remove:
            del ledger[k]
        _save_ledger(ledger)
