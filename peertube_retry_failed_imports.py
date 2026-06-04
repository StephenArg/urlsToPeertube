#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Find failed PeerTube video imports and retry them.

Checks GET /api/v1/users/me/videos/imports for imports whose state is Failed
(state id 3, label such as "Import failed") and optionally POSTs
/api/v1/videos/imports/{id}/retry (PeerTube >= 8.0).

Uses the same .env and OAuth flow as peertube_import.py.

With --cache, failed imports are stored in failed_imports_cache.json (by default).
Later runs read the cache instead of listing every import/video. Each successful retry
removes that import from the cache file immediately (safe if the process is killed).
Delete the cache file or pass --refresh-cache to rebuild from the API.

Run with uv:
  uv run peertube_retry_failed_imports.py
  uv run peertube_retry_failed_imports.py --dry-run --retry
  uv run peertube_retry_failed_imports.py --retry -v
  uv run peertube_retry_failed_imports.py --count-only
  uv run peertube_retry_failed_imports.py --cache
  uv run peertube_retry_failed_imports.py --cache --retry
  uv run peertube_retry_failed_imports.py --cache --refresh-cache
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_FILE = SCRIPT_DIR / "failed_imports_cache.json"
CACHE_VERSION = 1
PAGE_SIZE = 100
IMPORT_FAILED_STATE_ID = 3
VIDEO_IMPORT_FAILED_STATE_ID = 12


# ----------------------------
# .env parsing (no extra deps)
# ----------------------------

def parse_env_file(path: Path) -> dict[str, str]:
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
    return os.environ.get(key) or dotenv.get(key) or default


# ----------------------------
# Config
# ----------------------------

@dataclass(frozen=True)
class Config:
    instance: str
    username: str
    password: str
    channel_id: Optional[int]
    timeout_seconds: float
    verify_tls: bool
    verbose: bool


def normalize_instance_url(url: str) -> str:
    return url.strip().rstrip("/")


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
            "User-Agent": "peertube-retry-failed-imports/1.0",
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


def iter_user_videos(
    client: httpx.Client,
    token: str,
    *,
    page_size: int = PAGE_SIZE,
    verbose: bool = False,
) -> Iterable[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    start = 0
    total: Optional[int] = None

    while True:
        r = client.get(
            "/users/me/videos",
            headers=headers,
            params={"start": start, "count": page_size},
        )
        r.raise_for_status()
        body = r.json()
        batch = body.get("data") or []
        if total is None:
            total = body.get("total")
            if verbose and total is not None:
                print(f"[fetch] {total} video(s) reported by API", file=sys.stderr)

        if not batch:
            break

        yield from batch
        start += len(batch)

        if total is not None and start >= total:
            break
        if len(batch) < page_size:
            break


def iter_user_imports(
    client: httpx.Client,
    token: str,
    *,
    page_size: int = PAGE_SIZE,
    verbose: bool = False,
) -> Iterable[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    start = 0
    total: Optional[int] = None

    while True:
        r = client.get(
            "/users/me/videos/imports",
            headers=headers,
            params={"start": start, "count": page_size},
        )
        r.raise_for_status()
        body = r.json()
        batch = body.get("data") or []
        if total is None:
            total = body.get("total")
            if verbose and total is not None:
                print(f"[fetch] {total} import(s) reported by API", file=sys.stderr)

        if not batch:
            break

        yield from batch
        start += len(batch)

        if total is not None and start >= total:
            break
        if len(batch) < page_size:
            break


def import_state_label(imp: dict[str, Any]) -> str:
    state = imp.get("state")
    if isinstance(state, dict):
        return str(state.get("label") or "")
    return ""


def import_state_id(imp: dict[str, Any]) -> Optional[int]:
    state = imp.get("state")
    if isinstance(state, dict) and state.get("id") is not None:
        return int(state["id"])
    return None


def video_state_id(video: dict[str, Any]) -> Optional[int]:
    state = video.get("state")
    if isinstance(state, dict) and state.get("id") is not None:
        return int(state["id"])
    return None


def video_state_label(video: dict[str, Any]) -> str:
    state = video.get("state")
    if isinstance(state, dict):
        return str(state.get("label") or "")
    return ""


def is_video_import_failed(video: dict[str, Any]) -> bool:
    sid = video_state_id(video)
    if sid == VIDEO_IMPORT_FAILED_STATE_ID:
        return True
    label = video_state_label(video).casefold()
    return "import failed" in label


def is_import_failed(imp: dict[str, Any]) -> bool:
    sid = import_state_id(imp)
    if sid == IMPORT_FAILED_STATE_ID:
        return True
    label = import_state_label(imp).casefold()
    return "import failed" in label or label == "failed"


def import_channel_id(imp: dict[str, Any]) -> Optional[int]:
    video = imp.get("video")
    if not isinstance(video, dict):
        return None
    channel = video.get("channel")
    if not isinstance(channel, dict):
        return None
    cid = channel.get("id")
    return int(cid) if cid is not None else None


def summarize_import(instance: str, imp: dict[str, Any]) -> dict[str, Any]:
    video = imp.get("video") if isinstance(imp.get("video"), dict) else {}
    channel = video.get("channel") if isinstance(video.get("channel"), dict) else {}
    video_state = video.get("state") if isinstance(video.get("state"), dict) else {}
    return {
        "importId": imp.get("id"),
        "targetUrl": imp.get("targetUrl"),
        "magnetUri": imp.get("magnetUri"),
        "state": import_state_label(imp) or import_state_id(imp),
        "error": imp.get("error"),
        "updatedAt": imp.get("updatedAt"),
        "videoId": video.get("id"),
        "videoName": video.get("name"),
        "videoState": video_state.get("label") or video_state.get("id"),
        "channelId": channel.get("id"),
        "channelName": channel.get("name"),
        "instance": instance,
    }


def retry_import(
    client: httpx.Client,
    token: str,
    import_id: int,
    *,
    dry_run: bool,
    verbose: bool,
    quiet: bool = False,
) -> bool:
    if dry_run:
        if not quiet:
            print(f"[dry-run] would retry import id={import_id}")
        return True

    headers = {"Authorization": f"Bearer {token}"}
    r = client.post(f"/videos/imports/{import_id}/retry", headers=headers)
    if r.status_code == 204:
        if not quiet:
            if verbose:
                print(f"[retry] import id={import_id} -> queued")
            else:
                print(f"[retry] import id={import_id}")
        return True

    detail = ""
    try:
        detail = json.dumps(r.json(), ensure_ascii=False)
    except Exception:
        detail = (r.text or "").strip()
    print(
        f"[error] retry failed import id={import_id} -> HTTP {r.status_code}: {detail}",
        file=sys.stderr,
    )
    return False


def fetch_import_for_video(
    client: httpx.Client,
    token: str,
    video_id: int,
) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get(
        "/users/me/videos/imports",
        headers=headers,
        params={"videoId": video_id, "count": 1},
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    return data[0] if data else None


def find_failed_imports(
    client: httpx.Client,
    token: str,
    imports: Iterable[dict[str, Any]],
    videos: Iterable[dict[str, Any]],
    *,
    instance: str,
    channel_id: Optional[int],
    verbose: bool,
) -> list[dict[str, Any]]:
    by_import_id: dict[int, dict[str, Any]] = {}

    for imp in imports:
        if not is_import_failed(imp):
            continue
        if channel_id is not None and import_channel_id(imp) != channel_id:
            continue
        import_id = imp.get("id")
        if import_id is None:
            continue
        by_import_id[int(import_id)] = summarize_import(instance, imp)

    for video in videos:
        if not is_video_import_failed(video):
            continue
        ch = video.get("channel")
        if channel_id is not None:
            if not isinstance(ch, dict) or ch.get("id") != channel_id:
                continue
        video_id = video.get("id")
        if video_id is None:
            continue
        if any(row.get("videoId") == video_id for row in by_import_id.values()):
            continue
        imp = fetch_import_for_video(client, token, int(video_id))
        if imp is None:
            if verbose:
                print(
                    f"[warn] video id={video_id} is import-failed but no import row found",
                    file=sys.stderr,
                )
            continue
        import_id = imp.get("id")
        if import_id is None:
            continue
        by_import_id[int(import_id)] = summarize_import(instance, imp)

    failed = list(by_import_id.values())
    failed.sort(key=lambda row: (str(row.get("updatedAt") or ""), row.get("importId") or 0))
    return failed


# ----------------------------
# Local cache
# ----------------------------

def read_cache_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[cache] could not read {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"[cache] invalid format in {path}", file=sys.stderr)
        return None
    return data


def cache_meta_matches(cached: dict[str, Any], cfg: Config) -> bool:
    return (
        cached.get("version") == CACHE_VERSION
        and cached.get("instance") == cfg.instance
        and cached.get("username") == cfg.username
        and cached.get("channelId") == cfg.channel_id
    )


def failed_imports_from_cache(cached: dict[str, Any]) -> list[dict[str, Any]]:
    items = cached.get("failedImports")
    if not isinstance(items, list):
        return []
    failed: list[dict[str, Any]] = []
    for row in items:
        if isinstance(row, dict) and row.get("importId") is not None:
            failed.append(row)
    failed.sort(key=lambda row: (str(row.get("updatedAt") or ""), row.get("importId") or 0))
    return failed


def write_cache_file(path: Path, cfg: Config, failed: list[dict[str, Any]]) -> None:
    payload = {
        "version": CACHE_VERSION,
        "instance": cfg.instance,
        "username": cfg.username,
        "channelId": cfg.channel_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "failedImports": failed,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def remove_import_id_from_cache_file(
    path: Path,
    cfg: Config,
    import_id: int,
    *,
    verbose: bool = False,
) -> bool:
    cached = read_cache_file(path)
    if not cached or not cache_meta_matches(cached, cfg):
        if verbose:
            print(
                f"[cache] could not remove import id={import_id}; cache missing or stale",
                file=sys.stderr,
            )
        return False

    remaining = [
        row for row in failed_imports_from_cache(cached)
        if int(row["importId"]) != import_id
    ]
    write_cache_file(path, cfg, remaining)
    if verbose:
        print(
            f"[cache] removed import id={import_id}; {len(remaining)} remaining in {path}",
            file=sys.stderr,
        )
    return True


def print_human(
    failed: list[dict[str, Any]],
    *,
    imports_scanned: int,
    videos_scanned: int,
    from_cache: bool = False,
    cache_file: Optional[Path] = None,
) -> None:
    if not failed:
        if from_cache and cache_file:
            print(f"No failed imports in cache ({cache_file}).")
        else:
            print(
                f"No failed imports found "
                f"({imports_scanned} import(s), {videos_scanned} video(s) scanned)."
            )
        return

    if from_cache and cache_file:
        header = f"Found {len(failed)} failed import(s) (from cache {cache_file}):\n"
    else:
        header = (
            f"Found {len(failed)} failed import(s) "
            f"({imports_scanned} import(s), {videos_scanned} video(s) scanned):\n"
        )
    print(header)
    for row in failed:
        title = row.get("videoName") or row.get("targetUrl") or row.get("magnetUri") or "?"
        err = row.get("error") or ""
        err_bit = f"  error={err!r}" if err else ""
        print(
            f"  import id={row['importId']}  state={row['state']!r}\n"
            f"    {title}\n"
            f"    url={row.get('targetUrl') or row.get('magnetUri') or '?'}"
            f"  channel={row.get('channelName') or '?'}{err_bit}"
        )
        print()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="peertube_retry_failed_imports.py",
        description="Find failed PeerTube video imports and retry them.",
    )
    ap.add_argument(
        "--env",
        default=str(SCRIPT_DIR / ".env"),
        help="Path to .env file. Default: ./.env next to the script.",
    )
    ap.add_argument("--channel-id", type=int, default=None, help="Only imports on this channel ID.")
    ap.add_argument(
        "--all-channels",
        action="store_true",
        help="Scan all channels (ignore PEERTUBE_CHANNEL_ID).",
    )
    ap.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default from env or 30).")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed certs).",
    )
    ap.add_argument("--json", action="store_true", help="Print results as JSON.")
    ap.add_argument(
        "--count-only",
        action="store_true",
        help="Print only the number of failed imports (for scripts).",
    )
    ap.add_argument(
        "--retry",
        action="store_true",
        help="Retry each failed import via POST /videos/imports/{id}/retry.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="With --retry, show retries without calling the API.",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to wait between retries (default 0).",
    )
    ap.add_argument("--fail-fast", action="store_true", help="Stop on first failed retry.")
    ap.add_argument(
        "--cache",
        action="store_true",
        help="Cache failed imports locally; reuse cache on later runs (skip full API scan).",
    )
    ap.add_argument(
        "--cache-file",
        default=str(DEFAULT_CACHE_FILE),
        help=f"Path to the cache file (default: {DEFAULT_CACHE_FILE.name} next to the script).",
    )
    ap.add_argument(
        "--refresh-cache",
        action="store_true",
        help="With --cache, ignore existing cache and rebuild from the API.",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")

    args = ap.parse_args(argv)

    env_path = Path(args.env).expanduser()
    dotenv = parse_env_file(env_path)

    instance = getenv_prefer_runtime("PEERTUBE_INSTANCE", dotenv)
    username = getenv_prefer_runtime("PEERTUBE_USERNAME", dotenv)
    password = getenv_prefer_runtime("PEERTUBE_PASSWORD", dotenv)
    channel_id_s = getenv_prefer_runtime("PEERTUBE_CHANNEL_ID", dotenv)
    timeout_s = getenv_prefer_runtime("PEERTUBE_TIMEOUT", dotenv, "30")

    missing = [k for k, v in {
        "PEERTUBE_INSTANCE": instance,
        "PEERTUBE_USERNAME": username,
        "PEERTUBE_PASSWORD": password,
    }.items() if not v]
    if missing:
        print(
            "Missing required config: " + ", ".join(missing) + "\n"
            f"Looked in {env_path} and environment variables.\n"
            "See README for .env format.",
            file=sys.stderr,
        )
        return 2

    channel_id: Optional[int]
    if args.all_channels:
        channel_id = None
    elif args.channel_id is not None:
        channel_id = args.channel_id
    elif channel_id_s:
        try:
            channel_id = int(str(channel_id_s))
        except ValueError as e:
            print(f"Invalid PEERTUBE_CHANNEL_ID: {e}", file=sys.stderr)
            return 2
    else:
        channel_id = None

    try:
        cfg = Config(
            instance=normalize_instance_url(str(instance)),
            username=str(username),
            password=str(password),
            channel_id=channel_id,
            timeout_seconds=float(args.timeout if args.timeout is not None else float(str(timeout_s))),
            verify_tls=not bool(args.insecure),
            verbose=bool(args.verbose),
        )
    except ValueError as e:
        print(f"Invalid numeric config value: {e}", file=sys.stderr)
        return 2

    if cfg.verbose:
        scope = "all channels" if cfg.channel_id is None else f"channel_id={cfg.channel_id}"
        print(f"[config] instance={cfg.instance} user={cfg.username} scope={scope}", file=sys.stderr)

    retried = 0
    retry_failed = 0
    imports_scanned = 0
    videos_scanned = 0
    from_cache = False
    cache_path = Path(args.cache_file).expanduser()
    use_cache = bool(args.cache)

    failed: list[dict[str, Any]] = []

    with build_client(cfg) as client:
        token = get_access_token(client, username=cfg.username, password=cfg.password)

        if use_cache and cache_path.is_file() and not args.refresh_cache:
            cached = read_cache_file(cache_path)
            if cached and cache_meta_matches(cached, cfg):
                failed = failed_imports_from_cache(cached)
                from_cache = True
                if cfg.verbose:
                    print(
                        f"[cache] loaded {len(failed)} failed import(s) from {cache_path}",
                        file=sys.stderr,
                    )
            elif cfg.verbose:
                reason = "invalid or stale" if cached else "unreadable"
                print(f"[cache] {reason}; fetching from API", file=sys.stderr)

        if not from_cache:
            imports = list(iter_user_imports(client, token, verbose=cfg.verbose))
            videos = list(iter_user_videos(client, token, verbose=cfg.verbose))
            imports_scanned = len(imports)
            videos_scanned = len(videos)
            failed = find_failed_imports(
                client,
                token,
                imports,
                videos,
                instance=cfg.instance,
                channel_id=cfg.channel_id,
                verbose=cfg.verbose,
            )
            if use_cache:
                write_cache_file(cache_path, cfg, failed)
                if cfg.verbose:
                    print(
                        f"[cache] saved {len(failed)} failed import(s) to {cache_path}",
                        file=sys.stderr,
                    )
        if args.retry and failed:
            mode = "dry-run" if args.dry_run else "retry"
            if cfg.verbose:
                print(f"[{mode}] {len(failed)} failed import(s)", file=sys.stderr)
            for idx, row in enumerate(failed):
                import_id = row.get("importId")
                if import_id is None:
                    print("[error] missing import id, skipping", file=sys.stderr)
                    retry_failed += 1
                    if args.fail_fast:
                        break
                    continue
                ok = retry_import(
                    client,
                    token,
                    int(import_id),
                    dry_run=bool(args.dry_run),
                    verbose=cfg.verbose,
                    quiet=bool(args.count_only),
                )
                if ok:
                    retried += 1
                    if not args.dry_run and (use_cache or cache_path.is_file()):
                        iid = int(import_id)
                        if remove_import_id_from_cache_file(
                            cache_path,
                            cfg,
                            iid,
                            verbose=cfg.verbose,
                        ):
                            failed = [
                                row for row in failed
                                if row.get("importId") is not None and int(row["importId"]) != iid
                            ]
                else:
                    retry_failed += 1
                    if args.fail_fast:
                        break
                if args.sleep > 0 and idx + 1 < len(failed):
                    time.sleep(args.sleep)

    if args.count_only:
        print(len(failed))
    elif args.json:
        payload = {
            "instance": cfg.instance,
            "channelId": cfg.channel_id,
            "importsScanned": imports_scanned,
            "videosScanned": videos_scanned,
            "fromCache": from_cache,
            "cacheFile": str(cache_path) if use_cache else None,
            "failedImportCount": len(failed),
            "failedImports": failed,
            "retried": retried if args.retry else 0,
            "retryFailed": retry_failed if args.retry else 0,
            "dryRun": bool(args.dry_run) if args.retry else False,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_human(
            failed,
            imports_scanned=imports_scanned,
            videos_scanned=videos_scanned,
            from_cache=from_cache,
            cache_file=cache_path if use_cache else None,
        )
        if args.retry and failed and not args.dry_run:
            print(f"Retried {retried} import(s); {retry_failed} failed.")

    if retry_failed:
        return 1
    return 1 if failed and not args.retry else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
