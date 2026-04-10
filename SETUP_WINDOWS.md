# DAP PR Dashboard - Windows Setup

## Quick Start

1. **Extract** the zip to any folder
2. **Copy** `.env.example` to `.env` and fill in your GitHub token and org:
   ```
   copy .env.example .env
   notepad .env
   ```
3. **Double-click** `start.bat`
4. **Open** http://localhost:5000 in your browser

## Requirements

- Python 3.10+ (https://www.python.org/downloads/)
  - Make sure "Add Python to PATH" is checked during install

## Manual Setup (if start.bat doesn't work)

```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Configuration

Edit `.env` to configure:
- `GITHUB_TOKEN` - Your GitHub Personal Access Token (required)
- `GITHUB_ORG` - Your GitHub organization name
- `GITHUB_API_URL` - API URL (change for GitHub Enterprise)
- `GITHUB_REPO_FILTER` - Regex to filter repos by name (leave empty for all)
- `DEFAULT_PR_LOOKBACK_DAYS` - How far back to fetch PRs (default: 7)

See `.env.example` for all available options.
