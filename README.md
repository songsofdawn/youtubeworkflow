# YouTube Workflow — Stage 1

This repository currently implements only Stage 1: discovering daily YouTube
video candidates for manual rights review. It does not download, translate,
dub, render, or publish videos.

## Setup

1. Run `setup_stage1_fixed.bat`.
2. Set `YOUTUBE_API_KEY` in `.env`.
3. Adjust candidate rules in `config\trending_config.json` if needed.

## Run

Run `run_fetch_candidates_fixed.bat`, or use:

```bat
.venv\Scripts\python.exe src\fetch_daily_candidates.py --config config\trending_config.json --limit 20
```

The command writes top-50 CSV, JSON, grouped HTML, raw-pool JSON, and metrics
JSON files to `candidates`. Discovery is search-led (24-hour fresh plus 72-hour
growth modes); `mostPopular` contributes only a small wildcard pool.
The default daily plan rotates 24 query groups and runs two modes per group
(48 `search.list` calls), below the configured 60-call operational budget.
Every result starts with `rights_status=PENDING` and `selected=0`. Manual review
is required; this stage never treats a video as approved automatically.

## Test

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use mocks and do not consume YouTube API quota.
