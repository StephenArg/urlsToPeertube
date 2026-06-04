# urlsToPeertube

Python utilities for managing a PeerTube channel via the REST API. All scripts share the same `.env` file and OAuth login flow (`peertube_import.py` is the reference).

Run everything with [uv](https://docs.astral.sh/uv/) from the project directory:

```bash
uv run <script>.py [options]
```

---

## Requirements

- Python **3.10+**
- **uv** (installs `httpx` automatically from each script’s inline metadata)

Optional:

- **yt-dlp** — used only by `peertube_import.py` to fetch video titles from URLs

---

## Configuration (`.env`)

Create `.env` next to the scripts:

```env
PEERTUBE_INSTANCE="https://peertube.example.com"
PEERTUBE_USERNAME="your_username"
PEERTUBE_PASSWORD="your_password"
PEERTUBE_CHANNEL_ID="123"

# Optional
PEERTUBE_PRIVACY="1"
PEERTUBE_LANGUAGE="en"
PEERTUBE_SLEEP="3600"
PEERTUBE_TIMEOUT="30"
```

| Variable | Used by | Description |
|----------|---------|-------------|
| `PEERTUBE_INSTANCE` | All | Instance URL (no trailing slash) |
| `PEERTUBE_USERNAME` | All | Account username |
| `PEERTUBE_PASSWORD` | All | Account password |
| `PEERTUBE_CHANNEL_ID` | All | Default channel ID (scope for list/delete/retry scripts) |
| `PEERTUBE_PRIVACY` | Import only | Privacy on import (default `1`) |
| `PEERTUBE_LANGUAGE` | Import only | Language on import (default `en`) |
| `PEERTUBE_SLEEP` | Import only | Seconds between import attempts (default `3600`) |
| `PEERTUBE_TIMEOUT` | All | HTTP timeout in seconds (default `30`) |

**Privacy values** (from `GET /api/v1/videos/privacies` on your instance):

| Value | Typical label |
|-------|----------------|
| 1 | Public |
| 2 | Unlisted |
| 3 | Private |
| 4 | Internal |
| 5 | Password protected |

**Finding `PEERTUBE_CHANNEL_ID`:** open the channel in the PeerTube UI and check the URL, or use `GET /api/v1/video-channels`.

**Shared flags** (all maintenance scripts): `--env PATH`, `--channel-id INT`, `--all-channels`, `--timeout FLOAT`, `--insecure`, `-v` / `--verbose`.

---

## `peertube_import.py` — bulk URL import

Imports one URL per interval into your channel, with resume support. Reads `urls.txt`, writes progress to `last_ran_url.txt`, and appends failures to `failed_urls.txt`.

### Quick start

```bash
uv run peertube_import.py

# Override channel or privacy for this run
uv run peertube_import.py --channel-id 12 --privacy 2

# Faster interval (or set PEERTUBE_SLEEP in .env)
uv run peertube_import.py --sleep 60

# Without yt-dlp titles
uv run peertube_import.py --no-yt-dlp

# Dry run
uv run peertube_import.py --dry-run -v
```

**PM2 example:**

```bash
pm2 start peertube_import.py --interpreter python3
```

### `urls.txt` format

One URL per line. Blank lines and lines starting with `#` are ignored.

```text
https://www.youtube.com/watch?v=abc123
https://www.youtube.com/watch?v=def456
```

### Options

| Flag | Description |
|------|-------------|
| `--urls PATH` | URL list file (default: `./urls.txt`) |
| `--failed-urls PATH` | Failed URL log (default: `./failed_urls.txt`) |
| `--state-file PATH` | Last processed URL for resume (default: `./last_ran_url.txt`) |
| `--env PATH` | `.env` path (default: `./.env`) |
| `--channel-id INT` | Override channel ID |
| `--privacy INT` | Override privacy (1–5) |
| `--language STR` | Override language (e.g. `en`) |
| `--sleep FLOAT` | Seconds between attempts (default from env or `3600`) |
| `--timeout FLOAT` | HTTP timeout |
| `--insecure` | Disable TLS verification |
| `--no-yt-dlp` | Do not call yt-dlp for titles |
| `--dry-run` | Log only, no imports or state updates |
| `--fail-fast` | Exit on first failed import |
| `-v`, `--verbose` | Verbose logging |

---

## `peertube_duplicate_titles.py` — duplicate titles

Lists videos that share the same title on your channel (default: `PEERTUBE_CHANNEL_ID`). Optionally deletes older copies and keeps the newest (by `publishedAt`, then `createdAt`).

### Commands

```bash
# List duplicates
uv run peertube_duplicate_titles.py

# All channels
uv run peertube_duplicate_titles.py --all-channels

# Case-insensitive title matching
uv run peertube_duplicate_titles.py --ignore-case

# Preview deletions
uv run peertube_duplicate_titles.py --dry-run --delete

# Delete older duplicates (keeps newest per title)
uv run peertube_duplicate_titles.py --delete

# JSON output
uv run peertube_duplicate_titles.py --json
```

### Options

| Flag | Description |
|------|-------------|
| `--env PATH` | `.env` path |
| `--channel-id INT` | Only this channel |
| `--all-channels` | Ignore `PEERTUBE_CHANNEL_ID` |
| `--ignore-case` | Treat titles that differ only by case as duplicates |
| `--timeout FLOAT` | HTTP timeout |
| `--insecure` | Disable TLS verification |
| `--json` | JSON output |
| `--delete` | Delete older duplicates (requires confirmation via flag) |
| `--dry-run` | With `--delete`, show what would be deleted |
| `--fail-fast` | Stop on first failed delete |
| `-v`, `--verbose` | Verbose logging |

Exit code `1` if duplicates exist (list mode) or any delete fails.

---

## `peertube_retry_failed_imports.py` — retry failed imports

Finds failed video imports (import state **Failed**, or video state **Import failed**) and optionally retries them via `POST /api/v1/videos/imports/{id}/retry` (**PeerTube ≥ 8.0**).

### Commands

```bash
# List failed imports
uv run peertube_retry_failed_imports.py

# Count only (for scripts)
uv run peertube_retry_failed_imports.py --count-only

# Preview retries
uv run peertube_retry_failed_imports.py --dry-run --retry

# Retry all failed imports
uv run peertube_retry_failed_imports.py --retry

# Retry with delay between requests
uv run peertube_retry_failed_imports.py --retry --sleep 2 -v
```

### Cache (`--cache`)

Avoids scanning every import and video on each run by caching failed imports in `failed_imports_cache.json` (gitignored).

```bash
# First run: full API scan, write cache
uv run peertube_retry_failed_imports.py --cache

# Later runs: read cache only (fast)
uv run peertube_retry_failed_imports.py --cache --count-only

# Retry using cache; each success removes that id from the cache immediately
uv run peertube_retry_failed_imports.py --cache --retry

# Rebuild cache from API
uv run peertube_retry_failed_imports.py --cache --refresh-cache

# Custom cache path
uv run peertube_retry_failed_imports.py --cache --cache-file /path/to/cache.json
```

If the cache file is deleted, the next `--cache` run rebuilds it. Cache is tied to instance, username, and channel scope; changing those triggers a refresh.

### Options

| Flag | Description |
|------|-------------|
| `--env PATH` | `.env` path |
| `--channel-id INT` | Only this channel |
| `--all-channels` | Ignore `PEERTUBE_CHANNEL_ID` |
| `--timeout FLOAT` | HTTP timeout |
| `--insecure` | Disable TLS verification |
| `--json` | JSON output |
| `--count-only` | Print only the number of failed imports |
| `--retry` | Retry each failed import |
| `--dry-run` | With `--retry`, no API retry calls |
| `--sleep FLOAT` | Seconds between retries (default `0`) |
| `--fail-fast` | Stop on first failed retry |
| `--cache` | Use local cache file |
| `--cache-file PATH` | Cache path (default: `./failed_imports_cache.json`) |
| `--refresh-cache` | Ignore cache and rescan API |
| `-v`, `--verbose` | Verbose logging |

---

## Troubleshooting

**“Cannot fetch Information from Import URL”** — Often YouTube/extractor issues on the instance. Update PeerTube and dependencies, or try another source URL.

**Retry import returns errors** — Retry requires PeerTube 8.0+. Check instance version and that HTTP/URL import is enabled.

**Import script stuck / resume** — Check `last_ran_url.txt` and `failed_urls.txt`. Delete or edit `last_ran_url.txt` to restart from a specific point.

---

## Security

`.env` and `failed_imports_cache.json` contain sensitive data. They are listed in `.gitignore`; do not commit them.

```bash
echo ".env" >> .gitignore
echo "failed_imports_cache.json" >> .gitignore
```
