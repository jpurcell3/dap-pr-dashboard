# DAP PR Dashboard

A Flask web app that aggregates pull request data from GitHub / GitHub Enterprise and displays cycle-time metrics, bottleneck detection, CI/CD check status, and commit details.

## Features

- **Overview dashboard** with repo summaries, cycle-time charts, and PR counts
- **Repo detail** pages with Top Slowest PRs, Bottleneck Distribution, and Time Breakdown charts
- **PR detail** pages with timeline, metrics, reviewers, commits, and CI/CD check results (with failure details)
- **Bottleneck detection** (slow review, slow approval, stale PRs, large PRs, excessive review rounds)
- **Repo picker** with search across all org repos
- **Date filtering** and configurable lookback window
- **Persistent cache** so data survives server restarts

## Quick Start

### 1. Clone / copy the project

```bash
git clone <this-repo>
cd fusion-pr-dashboard
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:
- `GITHUB_TOKEN` - A personal access token with `repo` scope
- `GITHUB_ORG` - Your GitHub organization name
- `GITHUB_API_URL` - For GHE, use `https://your-host/api/v3`

### 4. Run

```bash
python app.py
```

Open http://localhost:5000 in your browser.

### 5. Sync data

Click the **Sync** button in the nav bar (or call `GET /api/refresh`) to fetch PR data from GitHub. The first sync may take a few minutes depending on the number of repos and PRs.

## Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL |
| `GITHUB_TOKEN` | *(required)* | Personal access token |
| `GITHUB_ORG` | `fusion-e` | Organization name |
| `GITHUB_REPO_PREFIX` | `fusion` | Only sync repos matching this prefix (empty = all) |
| `SSL_VERIFY` | `true` | Set `false` for self-signed certs |
| `GITHUB_WEB_URL` | *(auto-derived)* | Override the web URL for PR links |
| `DEFAULT_PR_LOOKBACK_DAYS` | `90` | Default date window for refreshes |
| `MAX_PRS_PER_REPO` | `500` | Safety cap on PRs fetched per repo |
| `CACHE_PATH` | `pr_cache.json` (next to app.py) | Override the cache file location |
| `LOG_TO_STDOUT` | *(unset)* | Set `true` to skip writing `server.log` |
| `REDIS_URL` | *(unset)* | Redis connection URL for shared state (e.g. `redis://localhost:6379/0`). When unset, in-memory dicts are used. |
| `WEB_CONCURRENCY` | `4` (with Redis) / `1` (without) | Number of gunicorn worker processes. Auto-scales based on Redis availability. |

## Running with Docker

### Quick start (container only)

```bash
cp .env.example .env   # edit with your values
docker compose up --build
```

Open http://localhost:5001 in your browser.

### Development workflow (local + container side by side)

```bash
# Terminal 1: local dev server
python app.py                     # http://localhost:5000

# Terminal 2: container mirror
docker compose up --build         # http://localhost:5001
```

The `docker-compose.yml` bind-mounts your project directory into the container, and gunicorn runs with `--reload`. Any file you save locally is picked up by both the local dev server and the container automatically.

The Docker Compose setup includes a Redis sidecar. The dashboard container connects to it automatically via `REDIS_URL`. This means multiple gunicorn workers (or container replicas) share refresh status and data state through Redis.

If you change `requirements.txt`, rebuild the container:

```bash
docker compose up --build
```

Or use Compose watch mode (Docker Compose 2.22+) to handle rebuilds automatically:

```bash
docker compose watch
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/config` | Current configuration (non-sensitive) |
| GET | `/api/repos` | Repos with PR data (add `?all=true` for all org repos) |
| GET | `/api/summary` | Repo summaries and overview stats |
| GET | `/api/repo/<name>` | Detailed PR metrics for a repo |
| GET | `/api/pr/<repo>/<number>` | Full PR detail with commits and checks |
| GET | `/api/refresh?repo=X&since=YYYY-MM-DD` | Start background data refresh |
| GET | `/api/refresh/status` | Poll refresh progress |
| GET/POST | `/api/refresh/cancel` | Cancel running refresh |
| POST/DELETE | `/api/repo/<name>/purge` | Remove a repo from the dashboard |

## Project Structure

```
fusion-pr-dashboard/
  app.py                 # Flask server and API routes
  github_collector.py    # GitHub API client (PRs, reviews, commits, checks)
  metrics.py             # Cycle-time metrics and bottleneck detection
  redis_state.py         # Redis-backed shared state (falls back to in-memory)
  gunicorn.conf.py       # Gunicorn settings (workers, threads, reload)
  templates/index.html   # Single-page dashboard UI
  requirements.txt       # Python dependencies
  .env.example           # Configuration template
  Dockerfile             # Container image definition
  docker-compose.yml     # Dev container with Redis sidecar
  .dockerignore
  .gitignore
```
