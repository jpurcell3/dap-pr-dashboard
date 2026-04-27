# DAP PR Dashboard — Project Notes

## Quick Start (local dev)
```
python app.py
# Opens on http://localhost:5000
```

## Build Commands
- **Docker**: `docker build -t dap-pr-dashboard:latest .`
- **Docker Compose**: `docker compose up --build`
- **Windows zip**: Run the Python script that packs files into `dap-pr-dashboard-windows.zip`

## Key Files
- `app.py` — Flask server, API routes, Jenkins backfill, reminder wiring, refresh logic
- `github_collector.py` — GitHub API client, PR enrichment, Jenkins result enrichment during sync
- `metrics.py` — Cycle-time metrics, bottleneck detection (7 types including `unstable_build`)
- `jenkins_client.py` — Jenkins API client (build info, stages, test reports)
- `reminder.py` — Reviewer reminder system (stale review detection, GitHub comment posting)
- `redis_state.py` — Redis-backed shared state with in-memory fallback
- `templates/index.html` — Single-page dashboard UI (Chart.js, DataTables, Bootstrap 5)
- `teams.json` — Team-to-repo mapping
- `.env` — Configuration (not committed)

## Testing
- No formal test suite; use inline Python scripts for verification
- Syntax check: `python -c "import ast; ast.parse(open('file.py', encoding='utf-8').read())"`
- Bottleneck detection can be unit-tested via `from metrics import detect_bottlenecks`

## Architecture Notes
- Jenkins `UNSTABLE` detection requires `JENKINS_USER` and `JENKINS_API_TOKEN` in `.env`
- Jenkins results are enriched during sync AND backfilled automatically on app startup/post-sync
- The backfill runs in a background daemon thread and only processes checks missing `jenkins_result`
- Two Jenkins servers: `osj-isg-03-prd` (reachable), `osj-isg-01-prd` (often returns 502)
- PR Insights has two tabs (PR Checks / Bottlenecks); filters clear on tab switch

## Reviewer Reminders
- Controlled by `REMINDER_ENABLED`, `REMINDER_THRESHOLD_HOURS`, `REMINDER_INTERVAL_HOURS`, `REMINDER_DRY_RUN`
- `reminder.py` scans open PRs for `requested_reviewers` who haven't submitted a review
- Posts @mention comments on GitHub via `post_pr_comment()` in `github_collector.py`
- Runs automatically after each sync and on startup (background daemon thread)
- `reminder_ledger.json` tracks sent reminders to avoid duplicates (gitignored)
- API endpoint: `/api/reminders` — shows stale reviews, supports `?send=true` to trigger on demand
- PR detail view shows a yellow "Waiting for Review" badge and pending reviewer list

## Distributions
- **Docker**: `dap-pr-dashboard:latest` — Linux container with gunicorn
- **Windows**: `dap-pr-dashboard-windows.zip` — includes `start.bat` launcher

## Remote
- GitHub: https://github.com/jpurcell3/dap-pr-dashboard
- Branch: `master`
