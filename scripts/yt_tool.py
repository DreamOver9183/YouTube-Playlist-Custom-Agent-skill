"""
yt_tool.py — Background tool for YouTube Playlist Agent-Skill.

This script is designed to be executed by AI Agents, NOT the human user.
It provides simple, stateless CLI commands to interact with the YouTube API.

Features:
- Regex-based Playlist ID extraction from YouTube/YT Music URLs.
- Agent-oriented logging: stdout only emits JSON or plain text data results for
  the Agent to parse, while detailed debug/trace logs go to logs/yt_skill.log.
- Custom credentials path support (defaults to ~/.gemini/skills/yt-playlist-manager/credentials).

Commands:
1. fetch <playlist_id_or_url> --out <output.json>
2. diff <old_playlist.json> <new_playlist.json> --out <changes.json>
3. update <playlist_id_or_url> <changes.json>
"""

import argparse
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from scripts.cache_manager import PlaylistCache
from scripts.executor import compute_diff, estimate_quota
from scripts.optimizer import run_full_optimization
from scripts.schemas import EnrichedPlaylistItem, ExecutionResult, PositionChange
from scripts.youtube_api import YouTubeClient

# --- Configuration & Logging ---

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CREDENTIALS_DIR = Path.home() / ".gemini" / "skills" / "yt-playlist-manager" / "credentials"
DEFAULT_CREDENTIALS_PATH = DEFAULT_CREDENTIALS_DIR / "client_secret.json"

# Agent-friendly logging: File gets everything (DEBUG), stdout gets only CRITICAL errors if not caught.
logger = logging.getLogger("yt_tool")
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(LOG_DIR / "yt_skill.log", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(file_handler)

# Suppress stdout logging from imported modules so we don't pollute the JSON output
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def get_authenticated_client(credentials_path: Path | None = None) -> YouTubeClient:
    """Initialize and authenticate the YouTube client."""
    creds_path = credentials_path or DEFAULT_CREDENTIALS_PATH
    if not creds_path.is_file():
        # Print plain string to stdout for Agent to read easily
        print(f"ERROR: Credentials not found at {creds_path}")
        logger.error("Credentials not found at %s", creds_path)
        sys.exit(1)
        
    client = YouTubeClient(credentials_path=creds_path, token_path=creds_path.parent / "token.json")
    try:
        client.authenticate()
        logger.debug("Successfully authenticated via YouTubeClient.")
        return client
    except Exception as exc:
        print(f"ERROR: Authentication failed: {exc}")
        logger.exception("Authentication failed")
        sys.exit(1)


def extract_id(raw: str) -> str:
    """Extract Playlist ID from URL using regex, or return raw if it looks like an ID."""
    match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)
    return raw.strip()
def ensure_credentials() -> None:
    """Guard function: detect missing credentials before any command runs.

    If credentials are not found at the default path, this function prints
    an error JSON with code CREDENTIALS_MISSING and exits.
    """
    if DEFAULT_CREDENTIALS_PATH.is_file():
        logger.debug("Credentials found at %s", DEFAULT_CREDENTIALS_PATH)
        return

    logger.warning("Credentials not found at %s.", DEFAULT_CREDENTIALS_PATH)
    print(json.dumps({
        "status": "error",
        "code": "CREDENTIALS_MISSING",
        "message": "Google OAuth 憑證未設定。請提供憑證檔案的絕對路徑，並透過 setup_credentials 子指令設定。"
    }))
    sys.exit(1)


# --- Commands ---

def cmd_fetch(args: argparse.Namespace) -> None:
    print(json.dumps({"status": "started", "command": "fetch", "message": "開始獲取清單資料..."}))
    sys.stdout.flush()
    ensure_credentials()
    
    playlist_id = extract_id(args.playlist)
    logger.info("Fetching playlist ID: %s", playlist_id)
    
    cache = PlaylistCache()
    cached = cache.get(playlist_id)
    if cached:
        logger.info("Cache hit for %s", playlist_id)
        out_data = [item.model_dump(mode="json") for item in cached.items]
    else:
        logger.info("Cache miss for %s. Fetching from API...", playlist_id)
        yt_client = get_authenticated_client(args.credentials)
        items = yt_client.get_playlist_items(playlist_id)
        
        if not items:
            print("ERROR: Playlist is empty or not accessible.")
            logger.error("Playlist %s is empty or not accessible.", playlist_id)
            sys.exit(1)
            
        video_ids = [item.video_id for item in items]
        metadata_list = yt_client.get_videos_metadata(video_ids)
        metadata_map = {m.video_id: m for m in metadata_list}
        
        enriched = [
            EnrichedPlaylistItem.from_item_and_metadata(item, metadata_map.get(item.video_id))
            for item in items
        ]
        cache.set(playlist_id, enriched)
        out_data = [item.model_dump(mode="json") for item in enriched]
        logger.info("Successfully fetched and cached %d items.", len(enriched))

    out_path = Path(args.out)
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    # stdout for Agent
    result = {"status": "success", "item_count": len(out_data), "file": str(out_path)}
    print(json.dumps(result))


def cmd_diff(args: argparse.Namespace) -> None:
    print(json.dumps({"status": "started", "command": "diff", "message": "開始計算差異與配額..."}))
    sys.stdout.flush()
    try:
        old_data = json.loads(Path(args.old).read_text(encoding="utf-8"))
        new_data = json.loads(Path(args.new).read_text(encoding="utf-8"))
        
        old_items = [EnrichedPlaylistItem.model_validate(x) for x in old_data]
        new_items = [EnrichedPlaylistItem.model_validate(x) for x in new_data]
    except Exception as exc:
        print(f"ERROR: Failed to load or parse JSON: {exc}")
        sys.exit(1)

    changes = compute_diff(old_items, new_items)
    quota = estimate_quota(changes)
    
    out_data = [c.model_dump(mode="json") for c in changes]
    out_path = Path(args.out)
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    # stdout for Agent
    result = {
        "status": "success",
        "changes_count": len(changes),
        "estimated_quota": quota,
        "file": str(out_path)
    }
    print(json.dumps(result))


def cmd_update(args: argparse.Namespace) -> None:
    print(json.dumps({"status": "started", "command": "update", "message": "開始寫回播放清單..."}))
    sys.stdout.flush()
    ensure_credentials()
    
    playlist_id = extract_id(args.playlist)
    
    try:
        changes_data = json.loads(Path(args.changes).read_text(encoding="utf-8"))
        changes = [PositionChange.model_validate(x) for x in changes_data]
    except Exception as exc:
        print(f"ERROR: Failed to load changes JSON: {exc}")
        sys.exit(1)

    if not changes:
        print(json.dumps({"status": "success", "message": "No changes to apply."}))
        sys.exit(0)

    yt_client = get_authenticated_client(args.credentials)
    
    # Progress loading
    progress_file = LOG_DIR / f"progress_{playlist_id}.json"
    completed_ids = set()
    if progress_file.is_file():
        try:
            completed_ids = set(json.loads(progress_file.read_text(encoding="utf-8")))
            logger.info("Loaded progress: %d items already completed.", len(completed_ids))
        except Exception:
            pass

    remaining = [c for c in changes if c.playlist_item_id not in completed_ids]
    
    result = ExecutionResult(total_changes=len(changes))
    result.successful = len(changes) - len(remaining)
    result.quota_used = result.successful * 50

    interrupted = False
    for change in remaining:
        try:
            success = yt_client.update_item_position(
                playlist_item_id=change.playlist_item_id,
                playlist_id=change.playlist_id,
                video_id=change.resource_id,
                new_position=change.new_position,
            )
            if success:
                result.successful += 1
                result.quota_used += 50
                completed_ids.add(change.playlist_item_id)
                progress_file.write_text(json.dumps(list(completed_ids)), encoding="utf-8")
            else:
                result.failed += 1
                result.errors.append(f"Failed to update {change.video_id}")
        except KeyboardInterrupt:
            interrupted = True
            logger.warning("Update interrupted by user/Agent.")
            break
        except Exception as exc:
            result.failed += 1
            result.errors.append(str(exc))
            logger.error("API error for %s: %s", change.video_id, exc)

        time.sleep(0.3)

    if result.successful == len(changes) and not result.failed:
        if progress_file.is_file():
            progress_file.unlink()
        # Invalidate cache
        PlaylistCache().invalidate(playlist_id)
    
    out_result = {
        "status": "partial" if result.failed or interrupted else "success",
        "total": result.total_changes,
        "successful": result.successful,
        "failed": result.failed,
        "quota_used": result.quota_used,
        "interrupted": interrupted,
        "errors": result.errors
    }
    print(json.dumps(out_result))


def cmd_setup_credentials(args: argparse.Namespace) -> None:
    """Install and validate Google OAuth credentials from a specified path."""
    source = Path(args.path)
    if not source.is_file():
        print(json.dumps({
            "status": "error",
            "code": "FILE_NOT_FOUND",
            "message": f"找不到指定的憑證檔案：{source}"
        }))
        sys.exit(1)

    # Validate: must be a Google OAuth JSON (contains 'installed' or 'web' top-level key)
    try:
        with source.open(encoding="utf-8") as f:
            data = json.load(f)
        if "installed" not in data and "web" not in data:
            raise ValueError("Missing 'installed' or 'web' key")
    except Exception as exc:
        logger.error("Invalid OAuth JSON at %s: %s", source, exc)
        print(json.dumps({
            "status": "error",
            "code": "INVALID_JSON",
            "message": "所選檔案不是合法的 Google OAuth 2.0 憑證。請確認您下載的是『桌面應用程式』類型的 OAuth Client ID。"
        }))
        sys.exit(1)

    # Copy to secure default location
    try:
        # SEC: Enforce secure permissions for sensitive OAuth credentials
        # Create directory with 0o700, and use touch() + chmod(0o600) to prevent TOCTOU
        DEFAULT_CREDENTIALS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        DEFAULT_CREDENTIALS_PATH.touch(mode=0o600, exist_ok=True)
        DEFAULT_CREDENTIALS_PATH.chmod(0o600)
        # SEC: Use copyfile instead of copy2 to prevent copying insecure source file permissions
        shutil.copyfile(source, DEFAULT_CREDENTIALS_PATH)

        logger.info("Credentials installed from %s to %s", source, DEFAULT_CREDENTIALS_PATH)
        print(json.dumps({
            "status": "success",
            "message": "憑證設定成功。"
        }))
    except Exception as exc:
        logger.exception("Failed to copy credentials")
        print(json.dumps({
            "status": "error",
            "code": "COPY_FAILED",
            "message": f"複製憑證失敗：{exc}。請手動將檔案放置到 {DEFAULT_CREDENTIALS_PATH}"
        }))
        sys.exit(1)


def cmd_optimize(args: argparse.Namespace) -> None:
    """Run the full optimization pipeline for playlist reordering.

    Reads current.json, computes artist grouping + LIS anchors,
    generates the minimal set of drift-safe changes, and writes
    them to a local staging file. No API calls are made.
    """
    print(json.dumps({"status": "started", "command": "optimize", "message": "Starting local optimization..."})) 
    sys.stdout.flush()

    try:
        current_data = json.loads(Path(args.current).read_text(encoding="utf-8"))
        current_items = [EnrichedPlaylistItem.model_validate(x) for x in current_data]
    except Exception as exc:
        print(json.dumps({"status": "error", "code": "PARSE_ERROR", "message": f"Failed to load current playlist: {exc}"}))
        sys.exit(1)

    if not current_items:
        print(json.dumps({"status": "error", "code": "EMPTY_PLAYLIST", "message": "Playlist is empty."}))
        sys.exit(1)

    aliases_path = Path(args.aliases) if args.aliases else None
    group_order = args.group_order or "first_appearance"

    logger.info(
        "Running optimization: %d items, group_order=%s, aliases=%s",
        len(current_items), group_order, aliases_path,
    )

    target, changes, report, resolutions = run_full_optimization(
        current_items,
        aliases_path=aliases_path,
        group_order=group_order,
    )

    # Write target ordering (new.json)
    target_path = Path(args.target_out)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_data = [item.model_dump(mode="json") for item in target]
    target_path.write_text(json.dumps(target_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write optimized changes (staged, drift-safe order)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    changes_data = [c.model_dump(mode="json") for c in changes]
    out_path.write_text(json.dumps(changes_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # stdout for Agent
    result = {
        "status": "success",
        "total_items": report.total_items,
        "anchors": report.anchors,
        "need_to_move": report.need_to_move,
        "estimated_quota": report.estimated_quota,
        "quota_saved_vs_naive": report.quota_saved_vs_naive,
        "groups_found": report.groups_found,
        "group_details": report.group_details,
        "unresolved_count": report.unresolved_count,
        "target_file": str(target_path),
        "changes_file": str(out_path),
    }
    print(json.dumps(result))


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="YouTube Playlist Agent-Skill Background Tool")
    parser.add_argument("--credentials", type=Path, help="Path to client_secret.json (optional)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_setup = subparsers.add_parser("setup_credentials")
    p_setup.add_argument("path", help="Path to client_secret.json to install")
    p_setup.set_defaults(func=cmd_setup_credentials)

    p_fetch = subparsers.add_parser("fetch")
    p_fetch.add_argument("playlist", help="Playlist ID or URL")
    p_fetch.add_argument("--out", required=True, help="Output JSON file path")
    p_fetch.set_defaults(func=cmd_fetch)

    p_diff = subparsers.add_parser("diff")
    p_diff.add_argument("old", help="Old playlist JSON file")
    p_diff.add_argument("new", help="New playlist JSON file")
    p_diff.add_argument("--out", required=True, help="Changes JSON output path")
    p_diff.set_defaults(func=cmd_diff)

    p_update = subparsers.add_parser("update")
    p_update.add_argument("playlist", help="Playlist ID or URL")
    p_update.add_argument("changes", help="Changes JSON file to apply")
    p_update.set_defaults(func=cmd_update)

    p_optimize = subparsers.add_parser("optimize", help="Compute optimized reorder changes (local, 0 API units)")
    p_optimize.add_argument("current", help="Current playlist JSON file (from fetch)")
    p_optimize.add_argument("--target-out", required=True, help="Output path for target ordering JSON")
    p_optimize.add_argument("--out", required=True, help="Output path for optimized changes JSON")
    p_optimize.add_argument("--aliases", default=None, help="Path to artist_aliases.json (optional)")
    p_optimize.add_argument("--group-order", default="first_appearance",
                            choices=["first_appearance", "alphabetical", "count_desc"],
                            help="Group ordering strategy")
    p_optimize.set_defaults(func=cmd_optimize)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
