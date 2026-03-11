#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Bulk-import (YouTube/HTTP/etc.) URLs into PeerTube using the REST API.

Defaults:
- reads urls from ./urls.txt (same directory as this script)
- reads config from ./.env (same directory as this script)

Run with uv:
  uv run peertube_import.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent


# ----------------------------
# .env parsing (no extra deps)
# ----------------------------

def parse_env_file(path: Path) -> dict[str, str]:
    """
    Minimal .env parser:
      - KEY=VALUE
      - optional quotes: KEY="VALUE" / KEY='VALUE'
      - ignores blank lines and lines starting with '#'
      - supports 'export KEY=VALUE'
    """
    if not path.exists():
        return {}

    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ("'", '"')):
            v = v[1:-1]
        out[k] = v
    return out


def getenv_prefer_runtime(key: str, dotenv: dict[str, str], default: Optional[str] = None) -> Optional[str]:
    """
    Precedence:
      1) real environment variables
      2) .env file values
      3) provided default
    """
    return os.environ.get(key) or dotenv.get(key) or default


# ----------------------------
# Config
# ----------------------------

@dataclass(frozen=True)
class Config:
    instance: str
    username: str
    password: str
    channel_id: int
    privacy: int
    language: str
    sleep_seconds: float
    timeout_seconds: float
    verify_tls: bool
    use_yt_dlp: bool
    dry_run: bool
    fail_fast: bool
    verbose: bool


def normalize_instance_url(url: str) -> str:
    return url.strip().rstrip("/")


def clamp_title(title: str, fallback_url: str) -> str:
    title = (title or "").strip()
    if len(title) < 3:
        title = f"Imported: {fallback_url}".strip()

    # PeerTube expects 3..120 chars (per common API validation); clamp to be safe.
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    if len(title) < 3:
        title = "Imported video"
    return title


def maybe_title_from_yt_dlp(url: str, *, timeout: float = 30.0) -> str:
    """
    If yt-dlp is installed as a CLI, try to extract the title.
    Returns "" if unavailable.
    """
    try:
        p = subprocess.run(
            ["yt-dlp", "--print", "%(title)s", url],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return ""
    if p.returncode != 0:
        return ""
    out = (p.stdout or "").strip()
    if not out:
        return ""
    # yt-dlp may output multiple lines; keep the first non-empty line
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


# ----------------------------
# PeerTube API
# ----------------------------

def build_client(cfg: Config) -> httpx.Client:
    return httpx.Client(
        base_url=f"{cfg.instance}/api/v1",
        timeout=httpx.Timeout(cfg.timeout_seconds),
        verify=cfg.verify_tls,
        headers={
            "Accept": "application/json",
            "User-Agent": "peertube-bulk-import/1.0",
        },
        follow_redirects=True,
    )


def get_oauth_client_tokens(client: httpx.Client) -> Tuple[str, str]:
    r = client.get("/oauth-clients/local")
    r.raise_for_status()
    data = r.json()
    return data["client_id"], data["client_secret"]


def get_access_token(client: httpx.Client, *, username: str, password: str) -> str:
    client_id, client_secret = get_oauth_client_tokens(client)
    r = client.post(
        "/users/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "password",
            "response_type": "code",
            "username": username,
            "password": password,
        },
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("No access_token in token response.")
    return token


def post_with_basic_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    files: list[tuple[str, tuple[None, str]]],
    max_retries: int = 3,
    verbose: bool = False,
) -> httpx.Response:
    """
    Retries on:
      - 429 with Retry-After
      - transient 5xx
    """
    attempt = 0
    while True:
        attempt += 1
        r = client.post(url, headers=headers, files=files)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 5.0
            if verbose:
                print(f"[rate-limit] 429 received; sleeping {delay:.1f}s then retrying...", file=sys.stderr)
            time.sleep(delay)
        elif 500 <= r.status_code <= 599 and attempt < max_retries:
            delay = 2.0 * attempt
            if verbose:
                print(f"[server] {r.status_code} received; sleeping {delay:.1f}s then retrying...", file=sys.stderr)
            time.sleep(delay)
        else:
            return r

        if attempt >= max_retries:
            return r


def import_url(
    client: httpx.Client,
    *,
    token: str,
    channel_id: int,
    privacy: int,
    language: str,
    url: str,
    title: str,
    comments_enabled: bool = False,
    download_enabled: bool = True,
    support: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
    fail_fast: bool = False,
) -> bool:
    title = clamp_title(title, url)
    support = support or url

    multipart_fields: list[tuple[str, tuple[None, str]]] = [
        ("channelId", (None, str(channel_id))),
        ("name", (None, title)),
        ("targetUrl", (None, url)),
        ("privacy", (None, str(privacy))),
        ("language", (None, language)),
        ("commentsEnabled", (None, "true" if comments_enabled else "false")),
        ("downloadEnabled", (None, "true" if download_enabled else "false")),
        ("support", (None, support)),
    ]

    if dry_run:
        print(f"[dry-run] would import: {url}")
        if verbose:
            print(f"          title={title!r} channelId={channel_id} privacy={privacy} language={language}")
        return True

    headers = {"Authorization": f"Bearer {token}"}
    r = post_with_basic_retry(
        client,
        "/videos/imports",
        headers=headers,
        files=multipart_fields,
        verbose=verbose,
    )

    if r.status_code == 200:
        if verbose:
            try:
                j = r.json()
                vid = None
                if isinstance(j, dict):
                    vid = j.get("video") or j.get("videoImport") or j.get("uuid") or j.get("id")
                print(f"[ok] {url} -> imported ({vid if vid else 'created'})")
            except Exception:
                print(f"[ok] {url} -> imported")
        else:
            print(f"[ok] {url}")
        return True

    detail = ""
    try:
        err = r.json()
        detail = json.dumps(err, ensure_ascii=False)
    except Exception:
        detail = (r.text or "").strip()

    print(f"[error] {url} -> HTTP {r.status_code}: {detail}", file=sys.stderr)
    if fail_fast:
        raise RuntimeError(f"Import failed for {url} (HTTP {r.status_code})")
    return False


# ----------------------------
# I/O helpers
# ----------------------------

def iter_urls(path: Path) -> Iterable[str]:
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        yield s


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="peertube_import.py",
        description="Bulk import URLs into a PeerTube channel using the PeerTube REST API.",
    )

    ap.add_argument(
        "--urls",
        default=str(SCRIPT_DIR / "urls.txt"),
        help="Path to urls.txt (one URL per line). Default: ./urls.txt next to the script.",
    )
    ap.add_argument(
        "--env",
        default=str(SCRIPT_DIR / ".env"),
        help="Path to .env file. Default: ./.env next to the script.",
    )

    ap.add_argument("--channel-id", type=int, default=None, help="Override channel ID for this run.")
    ap.add_argument("--privacy", type=int, default=None, help="Override privacy (1..5) for this run.")
    ap.add_argument("--language", type=str, default=None, help="Override language for this run (e.g. en, it).")

    ap.add_argument("--sleep", type=float, default=None, help="Seconds to sleep between imports (default from env or 1).")
    ap.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default from env or 30).")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed certs).",
    )
    ap.add_argument(
        "--no-yt-dlp",
        action="store_true",
        help="Do not try to call yt-dlp to fetch titles (faster, fewer dependencies).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print what would happen without importing anything.")
    ap.add_argument("--fail-fast", action="store_true", help="Exit on first failed import.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")

    args = ap.parse_args(argv)

    env_path = Path(args.env).expanduser()
    dotenv = parse_env_file(env_path)

    instance = getenv_prefer_runtime("PEERTUBE_INSTANCE", dotenv)
    username = getenv_prefer_runtime("PEERTUBE_USERNAME", dotenv)
    password = getenv_prefer_runtime("PEERTUBE_PASSWORD", dotenv)
    channel_id_s = getenv_prefer_runtime("PEERTUBE_CHANNEL_ID", dotenv)
    privacy_s = getenv_prefer_runtime("PEERTUBE_PRIVACY", dotenv, "1")
    language = getenv_prefer_runtime("PEERTUBE_LANGUAGE", dotenv, "en")
    sleep_s = getenv_prefer_runtime("PEERTUBE_SLEEP", dotenv, "1")
    timeout_s = getenv_prefer_runtime("PEERTUBE_TIMEOUT", dotenv, "30")

    missing = [k for k, v in {
        "PEERTUBE_INSTANCE": instance,
        "PEERTUBE_USERNAME": username,
        "PEERTUBE_PASSWORD": password,
        "PEERTUBE_CHANNEL_ID": channel_id_s,
    }.items() if not v]
    if missing:
        print(
            "Missing required config: " + ", ".join(missing) + "\n"
            f"Looked in {env_path} and environment variables.\n"
            "See README for .env format.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = Config(
            instance=normalize_instance_url(str(instance)),
            username=str(username),
            password=str(password),
            channel_id=int(args.channel_id if args.channel_id is not None else int(str(channel_id_s))),
            privacy=int(args.privacy if args.privacy is not None else int(str(privacy_s))),
            language=str(args.language if args.language is not None else str(language)),
            sleep_seconds=float(args.sleep if args.sleep is not None else float(str(sleep_s))),
            timeout_seconds=float(args.timeout if args.timeout is not None else float(str(timeout_s))),
            verify_tls=not bool(args.insecure),
            use_yt_dlp=not bool(args.no_yt_dlp),
            dry_run=bool(args.dry_run),
            fail_fast=bool(args.fail_fast),
            verbose=bool(args.verbose),
        )
    except ValueError as e:
        print(f"Invalid numeric config value: {e}", file=sys.stderr)
        return 2

    urls_path = Path(args.urls).expanduser()
    if not urls_path.is_file():
        print(f"URLs file not found: {urls_path}", file=sys.stderr)
        return 2

    if cfg.verbose:
        print(f"[config] instance={cfg.instance}")
        print(f"[config] user={cfg.username} channel_id={cfg.channel_id} privacy={cfg.privacy} language={cfg.language}")
        print(f"[config] urls={urls_path} env={env_path}")
        print(f"[config] yt-dlp={'on' if cfg.use_yt_dlp else 'off'} tls_verify={'on' if cfg.verify_tls else 'off'}")

    urls = list(iter_urls(urls_path))
    if not urls:
        print("No URLs found in urls file (empty or only comments).", file=sys.stderr)
        return 0

    ok = 0
    failed = 0

    with build_client(cfg) as client:
        try:
            token = get_access_token(client, username=cfg.username, password=cfg.password)
        except Exception as e:
            print(f"Failed to authenticate: {e}", file=sys.stderr)
            return 1

        for i, url in enumerate(urls, start=1):
            if cfg.verbose:
                print(f"[{i}/{len(urls)}] importing {url}")

            title = ""
            if cfg.use_yt_dlp:
                title = maybe_title_from_yt_dlp(url)

            try:
                success = import_url(
                    client,
                    token=token,
                    channel_id=cfg.channel_id,
                    privacy=cfg.privacy,
                    language=cfg.language,
                    url=url,
                    title=title,
                    dry_run=cfg.dry_run,
                    verbose=cfg.verbose,
                    fail_fast=cfg.fail_fast,
                )
            except Exception as e:
                print(f"[fatal] {url} -> {e}", file=sys.stderr)
                return 1

            if success:
                ok += 1
            else:
                failed += 1

            if i < len(urls) and cfg.sleep_seconds > 0:
                time.sleep(cfg.sleep_seconds)

    print(f"Done. OK={ok} Failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))