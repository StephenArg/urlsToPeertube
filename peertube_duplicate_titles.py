#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Find PeerTube videos that share the same title and optionally delete older copies.

For each duplicate title, keeps the newest video (by publishedAt, then createdAt)
and deletes the rest. Uses the same .env and OAuth flow as peertube_import.py.

Run with uv:
  uv run peertube_duplicate_titles.py
  uv run peertube_duplicate_titles.py --dry-run --delete
  uv run peertube_duplicate_titles.py --delete -v
  uv run peertube_duplicate_titles.py --json --dry-run --delete
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 100


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
            "User-Agent": "peertube-duplicate-titles/1.1",
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


def video_channel_id(video: dict[str, Any]) -> Optional[int]:
    channel = video.get("channel")
    if not isinstance(channel, dict):
        return None
    cid = channel.get("id")
    return int(cid) if cid is not None else None


def video_watch_url(instance: str, video: dict[str, Any]) -> str:
    short = video.get("shortUUID")
    if short:
        return f"{instance}/w/{short}"
    uuid = video.get("uuid")
    if uuid:
        return f"{instance}/videos/watch/{uuid}"
    vid = video.get("id")
    if vid is not None:
        return f"{instance}/videos/watch/{vid}"
    return instance


def summarize_video(instance: str, video: dict[str, Any]) -> dict[str, Any]:
    channel = video.get("channel") if isinstance(video.get("channel"), dict) else {}
    state = video.get("state") if isinstance(video.get("state"), dict) else {}
    return {
        "id": video.get("id"),
        "uuid": video.get("uuid"),
        "shortUUID": video.get("shortUUID"),
        "name": video.get("name") or "",
        "url": video_watch_url(instance, video),
        "channelId": channel.get("id"),
        "channelName": channel.get("name"),
        "createdAt": video.get("createdAt"),
        "publishedAt": video.get("publishedAt"),
        "state": state.get("label") or state.get("id"),
    }


def video_sort_key(video: dict[str, Any]) -> tuple[str, int]:
    """Older videos sort first; newest is last."""
    when = video.get("publishedAt") or video.get("createdAt") or ""
    vid = video.get("id")
    return when, int(vid) if vid is not None else 0


def split_keep_and_delete(videos: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ordered = sorted(videos, key=video_sort_key)
    return ordered[-1], ordered[:-1]


def delete_video(
    client: httpx.Client,
    token: str,
    video: dict[str, Any],
    *,
    dry_run: bool,
    verbose: bool,
) -> bool:
    video_ref = video.get("id") or video.get("uuid")
    if video_ref is None:
        print(f"[error] no id/uuid for {video.get('url')}", file=sys.stderr)
        return False

    if dry_run:
        print(f"[dry-run] would delete: {video['url']}  ({video.get('name')!r})")
        return True

    headers = {"Authorization": f"Bearer {token}"}
    r = client.delete(f"/videos/{video_ref}", headers=headers)
    if r.status_code == 204:
        if verbose:
            print(f"[deleted] {video['url']}")
        else:
            print(f"[deleted] {video['url']}  ({video.get('name')!r})")
        return True

    detail = ""
    try:
        detail = json.dumps(r.json(), ensure_ascii=False)
    except Exception:
        detail = (r.text or "").strip()
    print(
        f"[error] delete failed {video['url']} -> HTTP {r.status_code}: {detail}",
        file=sys.stderr,
    )
    return False


def plan_deletions(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for group in groups:
        keeper, to_delete = split_keep_and_delete(group["videos"])
        plans.append(
            {
                "title": group["title"],
                "keeper": keeper,
                "toDelete": to_delete,
            }
        )
    return plans


def run_deletions(
    client: httpx.Client,
    token: str,
    plans: list[dict[str, Any]],
    *,
    dry_run: bool,
    verbose: bool,
    fail_fast: bool,
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    for plan in plans:
        keeper = plan["keeper"]
        if verbose or dry_run:
            print(f"\n{plan['title']!r}: keep {keeper['url']}")
        for video in plan["toDelete"]:
            ok = delete_video(client, token, video, dry_run=dry_run, verbose=verbose)
            if ok:
                deleted += 1
            else:
                failed += 1
                if fail_fast:
                    return deleted, failed
    return deleted, failed


def title_key(title: str, *, ignore_case: bool) -> str:
    title = (title or "").strip()
    return title.casefold() if ignore_case else title


def find_duplicate_groups(
    videos: Iterable[dict[str, Any]],
    *,
    instance: str,
    channel_id: Optional[int],
    ignore_case: bool,
) -> list[dict[str, Any]]:
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for video in videos:
        if channel_id is not None and video_channel_id(video) != channel_id:
            continue
        name = video.get("name") or ""
        key = title_key(name, ignore_case=ignore_case)
        if not key:
            continue
        by_title[key].append(summarize_video(instance, video))

    groups: list[dict[str, Any]] = []
    for key in sorted(by_title, key=str.casefold):
        entries = by_title[key]
        if len(entries) < 2:
            continue
        display_title = entries[0]["name"]
        groups.append(
            {
                "title": display_title,
                "count": len(entries),
                "videos": sorted(entries, key=video_sort_key),
            }
        )

    groups.sort(key=lambda g: (-g["count"], str(g["title"]).casefold()))
    return groups


def print_human(
    groups: list[dict[str, Any]],
    *,
    total_scanned: int,
    plans: Optional[list[dict[str, Any]]] = None,
) -> None:
    if not groups:
        print(f"No duplicate titles found ({total_scanned} video(s) scanned).")
        return

    dup_video_count = sum(g["count"] for g in groups)
    print(
        f"Found {len(groups)} duplicate title(s) "
        f"({dup_video_count} videos across those groups, {total_scanned} scanned):\n"
    )
    for group in groups:
        print(f"  {group['title']!r}  ({group['count']} videos)")
        plan = None
        if plans:
            plan = next((p for p in plans if p["title"] == group["title"]), None)
        for video in group["videos"]:
            channel = video.get("channelName") or "?"
            state = video.get("state") or "?"
            when = video.get("publishedAt") or video.get("createdAt") or "?"
            suffix = ""
            if plan:
                vid = video.get("id")
                keeper_id = plan["keeper"].get("id")
                if vid is not None and vid == keeper_id:
                    suffix = "  [keep]"
                elif any(vid == d.get("id") for d in plan["toDelete"]):
                    suffix = "  [delete]"
            print(f"    - {video['url']}  [{channel}, {state}, {when}]{suffix}")
        print()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="peertube_duplicate_titles.py",
        description="Find duplicate PeerTube titles and optionally delete older copies.",
    )
    ap.add_argument(
        "--env",
        default=str(SCRIPT_DIR / ".env"),
        help="Path to .env file. Default: ./.env next to the script.",
    )
    ap.add_argument("--channel-id", type=int, default=None, help="Only include videos on this channel ID.")
    ap.add_argument(
        "--all-channels",
        action="store_true",
        help="Scan all of the user's channels (ignore PEERTUBE_CHANNEL_ID).",
    )
    ap.add_argument(
        "--ignore-case",
        action="store_true",
        help="Treat titles that differ only by letter case as duplicates.",
    )
    ap.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default from env or 30).")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed certs).",
    )
    ap.add_argument("--json", action="store_true", help="Print results as JSON.")
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Delete older duplicate(s), keeping the newest video in each group.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="With --delete, print what would be deleted without calling the API.",
    )
    ap.add_argument("--fail-fast", action="store_true", help="Stop on first failed delete.")
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

    with build_client(cfg) as client:
        token = get_access_token(client, username=cfg.username, password=cfg.password)
        videos = list(iter_user_videos(client, token, verbose=cfg.verbose))

        groups = find_duplicate_groups(
            videos,
            instance=cfg.instance,
            channel_id=cfg.channel_id,
            ignore_case=bool(args.ignore_case),
        )
        plans = plan_deletions(groups) if groups else []

        deleted = 0
        failed = 0
        if args.delete and plans:
            if cfg.verbose or args.dry_run:
                mode = "dry-run" if args.dry_run else "delete"
                print(f"[{mode}] processing {len(plans)} duplicate group(s)", file=sys.stderr)
            deleted, failed = run_deletions(
                client,
                token,
                plans,
                dry_run=bool(args.dry_run),
                verbose=cfg.verbose,
                fail_fast=bool(args.fail_fast),
            )
            if not args.dry_run and cfg.verbose:
                print(f"[done] deleted={deleted} failed={failed}", file=sys.stderr)

    if args.json:
        payload = {
            "instance": cfg.instance,
            "channelId": cfg.channel_id,
            "ignoreCase": bool(args.ignore_case),
            "scanned": len(videos),
            "duplicateTitleCount": len(groups),
            "groups": groups,
            "deletionPlans": plans,
            "deleted": deleted if args.delete else 0,
            "deleteFailed": failed if args.delete else 0,
            "dryRun": bool(args.dry_run) if args.delete else False,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_human(groups, total_scanned=len(videos), plans=plans if plans else None)
        if args.delete and plans and not args.dry_run:
            print(f"Deleted {deleted} older duplicate(s); {failed} failed.")

    if failed:
        return 1
    return 1 if groups and not args.delete else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
