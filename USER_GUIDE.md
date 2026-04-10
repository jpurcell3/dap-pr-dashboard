# DAP PR Dashboard — User Guide

A comprehensive guide to navigating and using the DAP PR Dashboard.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Navigation](#navigation)
3. [Overview Page](#overview-page)
4. [PR Insights Page](#pr-insights-page)
   - [PR Checks Tab](#pr-checks-tab)
   - [Bottlenecks Tab](#bottlenecks-tab)
5. [Repo Detail Page](#repo-detail-page)
6. [PR Detail Page](#pr-detail-page)
7. [Syncing Data](#syncing-data)
8. [Teams & Repo Filtering](#teams--repo-filtering)
9. [Bottleneck Types Reference](#bottleneck-types-reference)
10. [Keyboard Shortcuts](#keyboard-shortcuts)
11. [Theme Toggle](#theme-toggle)
12. [Configuration Reference](#configuration-reference)

---

## Getting Started

Open the dashboard in your browser (default: `http://localhost:5000`). On first load you will see an empty Overview page. Click the **Sync** button in the top-right corner to fetch PR data from GitHub. The first sync may take a few minutes depending on the number of repos and PRs in your organization.

Once the sync completes, the dashboard auto-refreshes and displays your data.

---

## Navigation

The sticky navbar at the top provides access to all major features:

| Element | Description |
|---------|-------------|
| **Overview** | Dashboard home — repo summaries, charts, and aggregate stats |
| **PR Insights** | Cross-repo analysis with two tabs: PR Checks and Bottlenecks |
| **Team selector** | Dropdown to filter all views by team (or "All Repos") |
| **Theme toggle** | Switch between dark and light mode (moon/sun icon) |
| **Last refreshed** | Timestamp of the most recent sync |
| **Sync** | Trigger a data refresh; the dropdown arrow opens advanced sync options |
| **Status** | Opens the sync status modal with detailed progress and rate-limit info |

Breadcrumb navigation appears on Repo Detail and PR Detail pages for easy backtracking (e.g. `Overview > fusion-manager > PR #1989`).

---

## Overview Page

The default landing page. Provides a high-level view across all repositories (or the selected team).

### Stat Cards

Six summary cards at the top:

- **Repos** — Total number of repositories with PR data
- **Total PRs** — All pull requests in the current dataset
- **Merged** — PRs that have been merged
- **Open** — PRs currently open
- **Avg Cycle Time** — Average time from PR creation to merge/close
- **Most Bottlenecks** — The repo(s) with the highest bottleneck count

### Charts

- **Cycle Time Distribution** — Histogram showing how many PRs fall into each time bracket. Color-coded: green (fast), yellow (moderate), red (slow).
- **PR State Distribution** — Pie chart of open / merged / closed counts.
- **Bottleneck Types** — Bar chart of the most common bottleneck types across all repos.

### Repository Table

A sortable, searchable table with one row per repo:

| Column | Description |
|--------|-------------|
| Repo | Repository name (click to open Repo Detail) |
| Total PRs | Number of PRs in the dataset |
| Open / Merged / Closed | PR counts by state |
| Avg Cycle Time | Average hours, color-coded (green ≤24h, yellow ≤72h, red >72h) |
| Median Cycle Time | Median hours |
| Avg Time to 1st Review | Average hours until the first review is submitted |
| Bottlenecks | Number of PRs with at least one bottleneck flag |

Click any column header to sort. Use the search box (top-right of the table) to filter. Click a repo name to drill into the Repo Detail page.

---

## PR Insights Page

A cross-repo analysis view with two tabs: **PR Checks** and **Bottlenecks**. Switch between them using the toggle buttons at the top. Filters reset automatically when switching tabs.

### Shared Filters

Both tabs share these filter controls:

- **Repo** dropdown — Narrow results to a single repository
- **State** toggle buttons — Filter by Open, Merged, and/or Closed (multi-select)

A filter count indicator appears when any filters are active (e.g. "Showing 42 of 310"). Use the **Clear All** button to reset.

---

### PR Checks Tab

Focused on CI/CD check results across all PRs. By default shows only PRs with failures.

#### Charts

- **Failed Checks by Group** (doughnut) — Click any slice to filter the table to that check group.
- **Failure Time Impact** (horizontal bar, collapsible) — Shows how much time PRs with failed checks spent in each check group. Sort is by failed hours descending.

#### Filters

- **Show All / Failures Only** toggle — Switch between all PRs and only those with at least one failed check.
- **Check group** dropdown — Filter by a specific CI check group (e.g. Jenkins, SonarQube, DRP Checkers). Includes a search box.
- **Check result** dropdown — Filter by passed or failed.

#### Table Columns

| Column | Description |
|--------|-------------|
| Repo | Repository name |
| PR | PR number (click to open PR Detail) |
| Title | PR title |
| State | Open / Merged / Closed badge |
| Check Groups | CI check groups present on this PR |
| Failed Checks | Names of failing checks |

---

### Bottlenecks Tab

Focused on cycle-time bottlenecks and PR health issues.

#### Charts

- **Bottleneck Types** (doughnut) — Distribution of bottleneck types. Click a slice to filter the table.
- **Time Phases** (stacked bar) — Average time spent in each cycle phase: Wait for Review, Review to Approval, Approval to Merge.

#### Filters

- **Type** dropdown — Filter by bottleneck type (e.g. slow_first_review, unstable_build)
- **Severity** dropdown — Filter by High, Medium, or Low

#### Table Columns

| Column | Description |
|--------|-------------|
| Repo | Repository name (click to open Repo Detail) |
| PR | PR number (click to open PR Detail) |
| Title | PR title |
| Bottleneck Type | The detected issue (badge) |
| Severity | High (red) or Medium (yellow) badge |
| Description | Human-readable explanation with actual values |

Use the **CSV Export** button to download the current (filtered) table.

---

## Repo Detail Page

Navigate here by clicking a repo name anywhere in the dashboard, or via the URL hash `#repo/<repo_name>`.

### Summary Stats

A stats card showing totals for the repository: PR counts (open, merged, closed), average cycle time, median cycle time, average time to first review, and bottleneck count.

### Charts

- **Top Slowest PRs** (horizontal bar) — The longest PRs by cycle time. Click a bar to jump to that PR's detail page.
- **Time Breakdown** (stacked horizontal bar) — Per-PR breakdown into four phases: Time to First Review, Review to Approval, Approval to Merge, and Rework/Unattributed.
- **Bottleneck Distribution** (doughnut) — Which bottleneck types affect this repo.

### PR Table

Sortable, searchable table of all PRs in this repo:

| Column | Description |
|--------|-------------|
| PR # | PR number (click to open PR Detail) |
| Title | PR title |
| Author | PR author |
| State | Open / Merged / Closed badge |
| Cycle Time | Total hours, color-coded |
| Bottlenecks | Count of bottleneck flags |

State filter buttons (Open / Merged / Closed) are available above the table for quick filtering.

---

## PR Detail Page

Navigate here by clicking a PR number anywhere, or via `#pr/<repo>/<number>`.

### Layout

The page is split into two columns:

**Left column (main content):**

- **PR header** — Title, state badge, author, creation date, and a link to the PR on GitHub.
- **Metadata grid** — Base/head branch, lines added/deleted, files changed, commit count.
- **Timeline** — Visual timeline from creation through first review, approval, and merge/close. Shows elapsed time between each event. Dots are color-coded by event type.
- **CI/CD Checks** — Summary bar showing pass/fail/pending distribution, followed by individual checks. Failed checks are shown first with details expanded. Passed checks are hidden behind a "show N passed checks" toggle.
- **Jenkins Builds** — Appears automatically when the PR has Jenkins checks. Shows:
  - Build result badge (SUCCESS / FAILURE / UNSTABLE / BUILDING)
  - Build duration and link to Jenkins
  - Pipeline stage breakdown with a progress bar; failed/unstable stages listed first, others collapsible
  - Test report (if available): pass/fail/skip counts, and a list of failed tests with error details
- **Commits** — List of commits with SHA, message, and author.
- **Bottleneck Flags** — Alert cards for each detected bottleneck, color-coded by severity (red = high, yellow = medium), with a description explaining the issue and threshold.

**Right column (sidebar):**

- **Metrics card** — Cycle time, time to first review, review to approval, approval to merge, and unattributed time.
- **Reviewers card** — Each reviewer with their avatar, name, review state (approved, changes requested, commented), and an icon.

---

## Syncing Data

### Quick Sync

Click the **Sync** button in the navbar. This refreshes all repos using the default lookback window (7 days by default).

### Advanced Sync Options

Click the dropdown arrow next to the Sync button to open the options panel:

- **Date range** — Quick-select pills (7d, 30d, 60d, 90d, 6 months, 1 year) or manual From/To date pickers.
- **Team filter** — Select a team to auto-check only its repos.
- **Repository selection** — Search and check/uncheck individual repos. Repos with existing data show a "synced" badge. Use "Select All" / "Clear All" for bulk selection.
- **Refresh Selected** — Syncs only the checked repos with the selected date range.

### Monitoring Progress

During a sync:

- The Sync button icon spins and a green **LIVE** badge appears in the navbar.
- A **floating progress widget** appears in the bottom-right showing: scope, current repo, repos done/total, PRs fetched, elapsed time, and rate limit status.
- Open the **Status** modal (navbar button) for detailed progress: progress bar, rate limit bar (color-coded green/yellow/red), and action buttons.

### Cancelling

Click **Cancel** in either the floating widget or the Status modal to stop a sync in progress. Partially fetched data is retained.

### Auto-Refresh

Set `AUTO_REFRESH_INTERVAL_MINUTES` in your `.env` file (e.g. `60` for hourly). The dashboard will periodically sync in the background using the default lookback window. Check the health endpoint (`/api/health`) for auto-refresh status.

---

## Teams & Repo Filtering

### Setting Up Teams

Edit `teams.json` in the project root. The format is a JSON object mapping team names to arrays of repo names:

```json
{
  "MyTeam": [
    "repo-name-1",
    "repo-name-2"
  ],
  "AnotherTeam": [
    "repo-name-3"
  ]
}
```

Teams appear in the navbar dropdown and in the sync options panel.

### Using the Team Selector

Select a team from the navbar dropdown. This immediately filters all views (Overview, PR Insights, Repo Detail) to show only repos belonging to that team. Select "All Repos" to clear the filter.

The team filter also applies when syncing — selecting a team in the sync options auto-checks only that team's repos.

---

## Bottleneck Types Reference

The dashboard automatically detects the following bottleneck types on each PR:

| Type | Severity | Trigger |
|------|----------|---------|
| **slow_first_review** | Medium | First review took more than 24 hours |
| | High | First review took more than 72 hours |
| **slow_approval** | Medium | Approval took more than 48 hours after first review |
| | High | Approval took more than 120 hours after first review |
| **slow_merge** | Medium | Merge took more than 24 hours after approval |
| | High | Merge took more than 72 hours after approval |
| **excessive_review_rounds** | Medium | 3 or more rounds of changes requested |
| | High | 5 or more rounds of changes requested |
| **stale_pr** | Medium | PR has been open for more than 7 days |
| | High | PR has been open for more than 30 days |
| **large_pr** | Medium | More than 500 lines changed |
| | High | More than 1,000 lines changed |
| **unstable_build** | Medium | 1 Jenkins build returned UNSTABLE |
| | High | 2 or more Jenkins builds returned UNSTABLE |

Bottlenecks surface in three places:
1. **PR Detail** — Bottleneck Flags section
2. **PR Insights > Bottlenecks** — Filterable table and doughnut chart
3. **Repo Detail** — Bottleneck distribution chart and counts in the summary

> **Note:** The `unstable_build` bottleneck requires Jenkins credentials to be configured. During sync, the dashboard queries the Jenkins API to get the actual build result (SUCCESS / FAILURE / UNSTABLE), since GitHub commit statuses do not distinguish UNSTABLE from FAILURE.

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `g` then `o` | Go to Overview |
| `g` then `p` | Go to PR Insights (PR Checks tab) |
| `g` then `b` | Go to PR Insights (Bottlenecks tab) |
| `g` then `s` | Open Sync Status modal |
| `r` | Trigger Sync |
| `t` | Toggle dark/light theme |
| `Esc` | Go back (PR Detail → Repo → Overview) or close modal |
| `?` | Show keyboard shortcuts help |

---

## Theme Toggle

Click the moon/sun icon in the navbar to switch between dark and light themes. Your preference is saved in the browser and persists across sessions.

---

## Configuration Reference

All settings are configured via environment variables or a `.env` file. Copy `.env.example` to `.env` and edit as needed.

### Required

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | Personal access token with `repo` scope |

### GitHub

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_API_URL` | `https://api.github.com` | API base URL. For GitHub Enterprise: `https://your-host/api/v3` |
| `GITHUB_ORG` | `fusion-e` | Organization name |
| `GITHUB_REPO_FILTER` | *(empty)* | Regex to filter repo names (empty = all repos) |
| `GITHUB_WEB_URL` | *(auto-derived)* | Override the web URL used for PR links |
| `SSL_VERIFY` | `true` | Set `false` for self-signed certificates |

### Data Fetching

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_PR_LOOKBACK_DAYS` | `7` | Default date window for syncs |
| `MAX_PRS_PER_REPO` | `500` | Safety cap on PRs fetched per repo |
| `MAX_CONCURRENT_REPOS` | `4` | Parallel repo fetch count during sync |
| `MAX_CONCURRENT_PRS` | `6` | Parallel PR enrichment count per repo |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_PATH` | `pr_cache.json` | Cache file location |
| `REDIS_URL` | *(unset)* | Redis URL for shared state across workers/containers |

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_CONCURRENCY` | `4` (Redis) / `1` (no Redis) | Gunicorn worker count |
| `AUTO_REFRESH_INTERVAL_MINUTES` | `0` (disabled) | Periodic background sync interval in minutes |
| `LOG_TO_STDOUT` | *(unset)* | Set `true` to log to stdout only (no `server.log`) |

### Jenkins (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `JENKINS_USER` | *(unset)* | Jenkins username |
| `JENKINS_API_TOKEN` | *(unset)* | Jenkins API token |
| `JENKINS_URLS` | *(unset)* | Comma-separated Jenkins base URLs (leave unset to allow any) |

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/config` | Current configuration (non-sensitive) |
| GET | `/api/health` | Service health (Redis, cache, refresh, rate limit) |
| GET | `/api/teams` | Team-to-repo mapping |
| GET | `/api/repos` | Repos with PR data (`?all=true` for all org repos) |
| GET | `/api/summary` | Repo summaries and overview stats |
| GET | `/api/prs` | All PRs with metrics (`?limit=N`) |
| GET | `/api/repo/<name>` | Detailed PR metrics for a repo |
| GET | `/api/pr/<repo>/<number>` | Full PR detail with commits and checks |
| GET | `/api/bottlenecks` | Bottleneck flags (filterable) |
| GET | `/api/refresh` | Start background refresh (`?repo=X&since=YYYY-MM-DD`) |
| GET | `/api/refresh/status` | Poll refresh progress |
| GET/POST | `/api/refresh/cancel` | Cancel running refresh |
| POST/DELETE | `/api/repo/<name>/purge` | Remove a repo from the dashboard |
| GET | `/api/jenkins/build` | Fetch Jenkins build details (`?url=...`) |
