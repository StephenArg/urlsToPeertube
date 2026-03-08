## PeerTube bulk URL importer (YouTube/HTTP) — `peertube_import.py`

This script bulk-imports online video URLs into a PeerTube channel using the PeerTube REST API (the same flow as the `curl` script you posted, but in Python).

It defaults to reading:

- `urls.txt` in the **same directory** as `peertube_import.py`
- `.env` in the **same directory** as `peertube_import.py`

It supports overriding `channelId` and `privacy` per run via CLI flags.

---

## Requirements

- Python **3.10+**
- Uses `httpx` for HTTP calls (handled automatically by **uv** via the inline script metadata)

Optional:

- `yt-dlp` (CLI) — if installed, the script will try to fetch nicer video titles from URLs. If not installed, it falls back to `Imported: <url>`.

---

## Quick start (recommended: `uv`)

From the directory containing `peertube_import.py`, `urls.txt`, and `.env`:

```bash
uv run peertube_import.py

# Override channel or privacy for this batch:
uv run peertube_import.py --channel-id 12 --privacy 2

# Run without calling yt-dlp:
uv run peertube_import.py --no-yt-dlp

# Dry-run:
uv run peertube_import.py --dry-run -v
```

## urls.txt format

One URL per line:

[https://www.youtube.com/watch?v=abc123](https://www.youtube.com/watch?v=abc123)
[https://www.youtube.com/watch?v=def456](https://www.youtube.com/watch?v=def456)

Blank lines and lines starting with # are ignored.

.env configuration

Create a .env file next to the script:

PEERTUBE_INSTANCE="[https://peertube.example.com](https://peertube.example.com)"
PEERTUBE_USERNAME="your_username"
PEERTUBE_PASSWORD="your_password"
PEERTUBE_CHANNEL_ID="123"

# Optional defaults

PEERTUBE_PRIVACY="1"
PEERTUBE_LANGUAGE="en"
PEERTUBE_SLEEP="1"
PEERTUBE_TIMEOUT="30"
Privacy values

PeerTube exposes privacy labels via GET /api/v1/videos/privacies. A common mapping is:

1 = Public
2 = Unlisted
3 = Private
4 = Internal
5 = Password protected

Command-line options
--urls PATH          Path to urls.txt (default: ./urls.txt next to the script)
--env PATH           Path to .env (default: ./.env next to the script)

--channel-id INT     Override channel ID for this run
--privacy INT        Override privacy (1..5) for this run
--language STR       Override language for this run (e.g. en, it)

--sleep FLOAT        Seconds to sleep between imports
--timeout FLOAT      HTTP timeout seconds
--insecure           Disable TLS certificate verification (self-signed certs)
--no-yt-dlp          Do not try to call yt-dlp for titles

--dry-run            Print what would happen without importing
--fail-fast          Exit on first failed import
-v, --verbose        Verbose logging

## Notes & troubleshooting  
“Cannot fetch Information from Import URL”

This can happen with YouTube if the instance’s extractor stack can’t fetch/parse the page (IP blocks, outdated extractors, etc.). In that case, try updating PeerTube (and its dependencies), or test importing a different HTTP source.

Finding your channelId

In the PeerTube UI, open the channel and look for its ID in the URL, or use the API endpoint GET /api/v1/video-channels and search for your channel.

## Security

Your .env contains credentials. Keep it out of git:

echo ".env" >> .gitignore

PeerTube privacy mapping shown in the README is based on the instance endpoint response from PeerTube’s public test server. :contentReference[oaicite:0]{index=0}
::contentReference[oaicite:1]{index=1}
 ​:contentReference[oaicite:2]{index=2}​

```

```

