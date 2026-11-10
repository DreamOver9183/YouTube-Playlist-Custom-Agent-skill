"""
yt_tool.py — Background tool for the playlist Agent-Skill (YouTube and Spotify).

This script is designed to be executed by AI Agents, NOT the human user.
It provides simple CLI commands to interact with the YouTube API.

Features:
- Regex-based Playlist ID extraction from YouTube/YT Music URLs.
- Agent-oriented logging: stdout only emits JSON or plain text data results for
  the Agent to parse, while detailed debug/trace logs go to logs/yt_skill.log.
- Custom credentials path support (defaults to ~/.gemini/skills/yt-playlist-manager/credentials,
  override with $YT_SKILL_HOME or --credentials).

Commands (the platform is detected from the playlist URL / the fetched file):
1. setup_credentials <path>            (YouTube)
   setup_spotify <client_id>           (Spotify)
2. fetch <playlist_id_or_url> --out <output.json> [--refresh]
3. optimize <current.json> --target-out <new.json> --out <changes.json> [--group-by artist|album]
4. diff <old_playlist.json> <new_playlist.json> --out <changes.json>
5. update <playlist_id_or_url> <changes.json>
6. duplicates <current.json>
7. spotify-check [playlist_url]       (read-only Spotify smoke test)
8. review start <current.json> <proposed.json> [--mode order|label]  (drag-and-drop page)
   review wait <run_dir> / review cancel <run_dir>

Change-set files are versioned objects (see ``write_change_set``).  The moves
inside are **order-dependent**: they must be executed in ``execution_order`` and
never re-sorted, because every playlistItems.update re-indexes the playlist.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.cache_manager import PlaylistCache
from scripts.executor import estimate_quota
from scripts.optimizer import (
    GROUP_BY_CHOICES,
    apply_range_move,
    evaluate_grouping_benefit,
    find_duplicate_videos,
    group_by_structured_field,
    normalize_target,
    plan_range_reorder,
    plan_reorder,
    run_full_optimization,
)

# Ensure UTF-8 output on all platforms (especially Windows PowerShell/cmd)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from scripts.schemas import (
    EnrichedPlaylistItem,
    ExecutionResult,
    PositionChange,
    RangeMove,
    SortConfig,
    SortField,
    SortOrder,
    compute_change_set_fingerprint,
    compute_head_fingerprint,
)

if TYPE_CHECKING:  # pragma: no cover
    from scripts.spotify_api import SpotifyClient
    from scripts.youtube_api import YouTubeClient

# The Google client stack is imported lazily so that `optimize` and `diff` —
# the commands documented as "0 API units, pure local computation" — really do
# run without it.

# --- Configuration & Logging ---

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

#: Credentials live outside the repo.  $YT_SKILL_HOME lets non-Gemini agents
#: (Claude Code, Codex, Copilot) pick their own home without editing the code.
SKILL_HOME = Path(
    os.environ.get("YT_SKILL_HOME", Path.home() / ".gemini" / "skills" / "yt-playlist-manager")
)
DEFAULT_CREDENTIALS_DIR = SKILL_HOME / "credentials"
DEFAULT_CREDENTIALS_PATH = DEFAULT_CREDENTIALS_DIR / "client_secret.json"

#: Change-set file format version.  v1 (a bare JSON array) was produced by the
#: old tail-first planner and its positions are not safe to execute.
CHANGE_SET_VERSION = 2

#: Retry policy for transient API errors (rate limit / 5xx).
MAX_RETRIES = 4
RETRY_DELAYS = (2, 4, 8, 16)

#: Pause between write calls to stay friendly to the API.
WRITE_PAUSE_SECONDS = 0.5

# Agent-friendly logging: File gets everything (DEBUG), stdout gets only CRITICAL errors if not caught.
logger = logging.getLogger("yt_tool")
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(LOG_DIR / "yt_skill.log", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(file_handler)

# Suppress stdout logging from imported modules so we don't pollute the JSON output
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def emit(payload: dict[str, Any]) -> None:
    """Print one JSON object to stdout for the Agent to parse."""
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def fail(code: str, message: str, **extra: Any) -> None:
    """Emit a structured error and exit with status 1."""
    logger.error("%s: %s", code, message)
    emit({"status": "error", "code": code, "message": message, **extra})
    sys.exit(1)


def credentials_path_from(args: argparse.Namespace) -> Path:
    """Resolve which client_secret.json to use for this invocation."""
    explicit = getattr(args, "credentials", None)
    return Path(explicit) if explicit else DEFAULT_CREDENTIALS_PATH


def get_authenticated_client(credentials_path: Path) -> "YouTubeClient":
    """Initialize and authenticate the YouTube client."""
    from scripts.youtube_api import YouTubeClient

    client = YouTubeClient(
        credentials_path=credentials_path,
        token_path=credentials_path.parent / "token.json",
    )
    try:
        client.authenticate()
        logger.debug("Successfully authenticated via YouTubeClient.")
        return client
    except Exception as exc:
        fail("AUTH_FAILED", f"Google 帳號授權失敗：{exc}")
        raise  # unreachable; keeps type checkers happy


def extract_id(raw: str) -> str:
    """Extract Playlist ID from URL using regex, or return sanitized raw ID.

    Security: Always sanitize raw input to prevent path traversal vulnerabilities.
    """
    match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)
    safe_id = "".join(c for c in raw.strip() if c.isalnum() or c in "-_")
    return safe_id if safe_id else "unknown"


def ensure_credentials(credentials_path: Path) -> None:
    """Guard function: detect missing credentials before any command runs.

    If credentials are not found, print an error JSON with code
    CREDENTIALS_MISSING and exit so the Agent can ask the user for the path.
    """
    if credentials_path.is_file():
        logger.debug("Credentials found at %s", credentials_path)
        return

    fail(
        "CREDENTIALS_MISSING",
        "Google OAuth 憑證未設定。請提供憑證檔案的絕對路徑，並透過 setup_credentials 子指令設定。",
        expected_path=str(credentials_path),
    )


# --- Change-set I/O ---


def write_change_set(
    path: Path,
    playlist_id: str,
    current_items: list[EnrichedPlaylistItem],
    changes: list[PositionChange],
    report_extra: dict[str, Any] | None = None,
) -> str:
    """Write a versioned, order-dependent change set to disk.

    The snapshot the plan was computed against is embedded so ``update`` can
    detect a playlist that has changed underneath it *before* spending 50 units
    per wrong move.

    Returns:
        The change-set fingerprint (also used to key the resume file).
    """
    fingerprint = compute_change_set_fingerprint(playlist_id, changes)
    payload = {
        "version": CHANGE_SET_VERSION,
        "platform": "youtube",
        "playlist_id": playlist_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "execution": "sequential",
        "note": (
            "依 execution_order 由小到大逐筆執行；不可重新排序、跳過或平行執行。"
            "new_position 是送給 API 的位置，final_position 才是最終索引。"
        ),
        "source_snapshot": {
            "item_count": len(current_items),
            "head_fingerprint": compute_head_fingerprint(current_items),
            "item_ids": [item.playlist_item_id for item in current_items],
        },
        "report": report_extra or {},
        "changes": [c.model_dump(mode="json") for c in changes],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fingerprint


def read_change_set(path: Path) -> dict[str, Any]:
    """Read and validate a change-set file, rejecting the legacy format."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail("PARSE_ERROR", f"無法讀取變更檔 {path}：{exc}")

    if isinstance(raw, list):
        fail(
            "LEGACY_CHANGE_SET",
            "這是舊版（v1）變更檔，其位置是以尾端優先的靜態索引計算的，直接執行會打亂清單。"
            "請重新執行 optimize 或 diff 產生新的變更檔。",
        )

    if isinstance(raw, dict) and raw.get("version") == SPOTIFY_CHANGE_SET_VERSION:
        return read_spotify_change_set(raw)

    if not isinstance(raw, dict) or raw.get("version") != CHANGE_SET_VERSION:
        fail(
            "UNSUPPORTED_CHANGE_SET",
            f"不支援的變更檔版本：{raw.get('version') if isinstance(raw, dict) else type(raw).__name__}。"
            "請重新執行 optimize 或 diff。",
        )

    try:
        changes = [PositionChange.model_validate(x) for x in raw.get("changes", [])]
    except Exception as exc:
        fail("PARSE_ERROR", f"變更檔內容格式錯誤：{exc}")

    if any(c.execution_order < 0 for c in changes):
        fail(
            "LEGACY_CHANGE_SET",
            "變更檔缺少 execution_order，無法保證執行順序。請重新執行 optimize 或 diff。",
        )

    changes.sort(key=lambda c: c.execution_order)
    if [c.execution_order for c in changes] != list(range(len(changes))):
        fail("CORRUPT_CHANGE_SET", "execution_order 不連續，變更檔可能已被手動修改。")

    raw["changes"] = changes
    return raw


def replay_ids(item_ids: list[str], changes: list[PositionChange]) -> list[str]:
    """Apply moves to an ordered id list, mirroring playlistItems.update semantics."""
    live = list(item_ids)
    for change in changes:
        try:
            live.remove(change.playlist_item_id)
        except ValueError:
            logger.warning(
                "Replay: item %s not present in snapshot; skipping.",
                change.playlist_item_id,
            )
            continue
        live.insert(change.new_position, change.playlist_item_id)
    return live


# --- Resume state ---


def progress_path(playlist_id: str) -> Path:
    safe_id = "".join(c for c in playlist_id if c.isalnum() or c in "-_") or "unknown"
    return LOG_DIR / f"progress_{safe_id}.json"


def load_progress(playlist_id: str, fingerprint: str) -> int:
    """Return how many moves of *this* change set are already applied.

    The fingerprint binds the progress file to one specific plan.  A leftover
    file from an unrelated run must never cause moves to be skipped — that is
    how a "successful" run silently does nothing.
    """
    path = progress_path(playlist_id)
    if not path.is_file():
        return 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Unreadable progress file %s; starting from scratch.", path)
        return 0

    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        logger.info(
            "Progress file belongs to a different change set (%s != %s); ignoring it.",
            data.get("fingerprint") if isinstance(data, dict) else "<legacy>",
            fingerprint,
        )
        return 0

    completed = int(data.get("completed", 0))
    return max(0, completed)


def save_progress(playlist_id: str, fingerprint: str, completed: int) -> None:
    progress_path(playlist_id).write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "completed": completed,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def clear_progress(playlist_id: str) -> None:
    path = progress_path(playlist_id)
    if path.is_file():
        path.unlink()


# --- Commands ---


def cmd_fetch(args: argparse.Namespace) -> None:
    emit({"status": "started", "command": "fetch", "message": "開始獲取清單資料..."})
    platform, playlist_id = detect_platform(args.playlist)
    if platform == "spotify":
        cmd_fetch_spotify(args, playlist_id)
        return

    creds_path = credentials_path_from(args)
    ensure_credentials(creds_path)
    logger.info("Fetching playlist ID: %s (refresh=%s)", playlist_id, args.refresh)

    cache = PlaylistCache()
    cached = None if args.refresh else cache.get(playlist_id)

    if cached:
        logger.info("Cache hit for %s", playlist_id)
        items = cached.items
        source = "cache"
        fetched_at = cached.created_at.isoformat()
    else:
        logger.info("Cache miss for %s. Fetching from API...", playlist_id)
        yt_client = get_authenticated_client(creds_path)
        raw_items = yt_client.get_playlist_items(playlist_id)

        if not raw_items:
            fail("EMPTY_PLAYLIST", "播放清單是空的或無法存取。")

        video_ids = [item.video_id for item in raw_items]
        metadata_list = yt_client.get_videos_metadata(video_ids)
        metadata_map = {m.video_id: m for m in metadata_list}

        items = [
            EnrichedPlaylistItem.from_item_and_metadata(item, metadata_map.get(item.video_id))
            for item in raw_items
        ]
        cache.set(playlist_id, items)
        source = "api"
        fetched_at = datetime.now(timezone.utc).isoformat()
        logger.info("Successfully fetched and cached %d items.", len(items))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    hidden_count = sum(1 for item in items if not item.is_available)
    metadata_missing_count = sum(1 for item in items if not item.metadata_available)
    emit(
        {
            "status": "success",
            "platform": "youtube",
            "item_count": len(items),
            "hidden_count": hidden_count,
            "hidden_note": (
                "私人／已刪除影片仍佔用位置，已保留為釘選項目且不會被移動。"
                if hidden_count
                else ""
            ),
            "metadata_missing_count": metadata_missing_count,
            "metadata_missing_note": (
                "有影片可在清單中看到但 videos.list 未回傳其資料（常見於地區限制），"
                "這類影片仍可正常移動位置，但缺少標題／頻道名可用於藝人辨識，"
                "分組時多半會落入 unknown。"
                if metadata_missing_count
                else ""
            ),
            "source": source,
            "fetched_at": fetched_at,
            "head_fingerprint": compute_head_fingerprint(items),
            "file": str(out_path),
        }
    )


def _load_items(path: Path, label: str) -> list[EnrichedPlaylistItem]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [EnrichedPlaylistItem.model_validate(x) for x in data]
    except Exception as exc:
        fail("PARSE_ERROR", f"無法載入{label}（{path}）：{exc}")
        raise  # unreachable


def _playlist_id_of(items: list[EnrichedPlaylistItem], fallback: str = "") -> str:
    for item in items:
        if item.playlist_id:
            return item.playlist_id
    return fallback


def cmd_diff(args: argparse.Namespace) -> None:
    emit({"status": "started", "command": "diff", "message": "開始計算差異與配額..."})

    old_items = _load_items(Path(args.old), "原始清單")
    new_items = _load_items(Path(args.new), "目標清單")

    if old_items and old_items[0].platform == "spotify":
        out_path = Path(args.out)
        _moves, stats, fingerprint = _spotify_plan_and_write(old_items, new_items, out_path, {})
        emit(
            {
                "status": "success",
                "platform": "spotify",
                "changes_count": stats["calls"],
                "estimated_calls": stats["calls"],
                "items_moved": stats["items_moved"],
                "item_level_moves": stats["item_level_moves"],
                "pinned_count": stats["pinned_count"],
                "estimated_quota": 0,
                "fingerprint": fingerprint,
                "file": str(out_path),
            }
        )
        return

    try:
        changes, report = plan_reorder(old_items, new_items)
    except ValueError as exc:
        fail("TARGET_MISMATCH", str(exc))

    playlist_id = _playlist_id_of(old_items)
    out_path = Path(args.out)
    fingerprint = write_change_set(
        out_path,
        playlist_id,
        old_items,
        changes,
        {
            "anchors": report.anchors,
            "need_to_move": report.need_to_move,
            "pinned_count": report.pinned_count,
            "quota_saved_vs_naive": report.quota_saved_vs_naive,
        },
    )

    emit(
        {
            "status": "success",
            "platform": "youtube",
            "changes_count": len(changes),
            "anchors": report.anchors,
            "pinned_count": report.pinned_count,
            "estimated_quota": estimate_quota(changes),
            "quota_saved_vs_naive": report.quota_saved_vs_naive,
            "fingerprint": fingerprint,
            "file": str(out_path),
        }
    )


def _write_target(path: Path, target: list[EnrichedPlaylistItem], group_of: dict[str, str]) -> None:
    """Write the target order; each item also carries its ``group`` for the review page.

    The extra key is ignored when the file is loaded back as items (diff, update).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [dict(item.model_dump(mode="json"), group=group_of.get(item.playlist_item_id, ""))
             for item in target],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_within_group_sort(raw: str | None) -> SortConfig | None:
    """Parse ``--within-group-sort viewCount:desc`` into a SortConfig."""
    if not raw:
        return None

    field_raw, _, order_raw = raw.partition(":")
    try:
        field = SortField(field_raw.strip())
    except ValueError:
        fail(
            "INVALID_SORT_FIELD",
            f"不支援的排序欄位「{field_raw}」。可用值："
            + ", ".join(f.value for f in SortField),
        )

    order_text = (order_raw or "desc").strip().lower()
    try:
        order = SortOrder(order_text)
    except ValueError:
        fail("INVALID_SORT_ORDER", f"排序方向只能是 asc 或 desc，收到「{order_raw}」。")

    return SortConfig(field=field, order=order)


def cmd_optimize(args: argparse.Namespace) -> None:
    """Compute the grouped target ordering and the ordered move plan (0 API units)."""
    emit({"status": "started", "command": "optimize", "message": "開始本地最佳化計算..."})

    current_items = _load_items(Path(args.current), "目前清單")
    if not current_items:
        fail("EMPTY_PLAYLIST", "播放清單是空的。")

    platform = current_items[0].platform
    group_by = args.group_by or "artist"
    if group_by not in GROUP_BY_CHOICES[platform]:
        fail(
            "UNSUPPORTED_GROUP_BY",
            f"{platform} 不支援 --group-by {group_by}（可用：{', '.join(GROUP_BY_CHOICES[platform])}）。"
            "YouTube 沒有結構化的專輯資料，專輯聚集已於 v3.10.0 決定不做。",
        )
    if platform == "spotify":
        cmd_optimize_spotify(args, current_items)
        return

    aliases_path = Path(args.aliases) if args.aliases else None
    group_order = args.group_order or "first_appearance"
    within_group_sort = _parse_within_group_sort(args.within_group_sort)

    logger.info(
        "Running optimization: %d items, group_order=%s, within_group_sort=%s, aliases=%s",
        len(current_items), group_order, args.within_group_sort, aliases_path,
    )

    try:
        target, changes, report, resolutions = run_full_optimization(
            current_items,
            aliases_path=aliases_path,
            group_order=group_order,
            within_group_sort=within_group_sort,
        )
    except ValueError as exc:
        fail("TARGET_MISMATCH", str(exc))

    group_of = {
        item.playlist_item_id: ("" if res.method == "unavailable" else res.artist_key)
        for item, res in zip(current_items, resolutions)
    }
    target_path = Path(args.target_out)
    _write_target(target_path, target, group_of)

    playlist_id = _playlist_id_of(current_items)
    out_path = Path(args.out)
    fingerprint = write_change_set(
        out_path,
        playlist_id,
        current_items,
        changes,
        {
            "anchors": report.anchors,
            "need_to_move": report.need_to_move,
            "pinned_count": report.pinned_count,
            "quota_saved_vs_naive": report.quota_saved_vs_naive,
            "groups_found": report.groups_found,
            "channel_majority_overrides": [
                o.model_dump(mode="json") for o in report.channel_majority_overrides
            ],
        },
    )

    emit(
        {
            "status": "success",
            "platform": "youtube",
            "group_by": "artist",
            "total_items": report.total_items,
            "anchors": report.anchors,
            "need_to_move": report.need_to_move,
            "pinned_count": report.pinned_count,
            "estimated_quota": report.estimated_quota,
            "quota_saved_vs_naive": report.quota_saved_vs_naive,
            "groups_found": report.groups_found,
            "group_details": report.group_details,
            "unresolved_count": report.unresolved_count,
            "metadata_missing_count": report.metadata_missing_count,
            "unknown_ratio": report.unknown_ratio,
            "effective_grouping_ratio": report.effective_grouping_ratio,
            "orphan_group_count": report.orphan_group_count,
            "grouping_benefit": report.grouping_benefit,
            "grouping_warnings": report.grouping_warnings,
            "channel_majority_overrides": [
                o.model_dump(mode="json") for o in report.channel_majority_overrides
            ],
            "fingerprint": fingerprint,
            "target_file": str(target_path),
            "changes_file": str(out_path),
        }
    )


class _IdOnly:
    """Minimal shim so head fingerprints can be computed from bare ids."""

    __slots__ = ("playlist_item_id",)

    def __init__(self, playlist_item_id: str) -> None:
        self.playlist_item_id = playlist_item_id


def _verify_snapshot(
    yt_client: "YouTubeClient",
    playlist_id: str,
    snapshot_ids: list[str],
    applied: list[PositionChange],
) -> None:
    """Abort unless the remote playlist still matches what the plan assumes.

    Costs 1 quota unit.  On resume the expected state is the snapshot with the
    already-applied moves replayed on top of it.
    """
    from googleapiclient.errors import HttpError

    from scripts.youtube_api import classify_http_error

    if not snapshot_ids:
        logger.warning("Change set has no snapshot ids; skipping the staleness check.")
        return

    expected = replay_ids(snapshot_ids, applied)
    try:
        remote_head = yt_client.get_playlist_head(playlist_id)
    except HttpError as exc:
        code, _retryable, _fatal = classify_http_error(exc)
        fail(code, f"寫回前的清單驗證失敗：{exc}")

    if not remote_head:
        fail("EMPTY_PLAYLIST", "遠端播放清單是空的或無法存取。")

    expected_head = expected[: len(remote_head)]
    if remote_head != expected_head:
        fail(
            "STALE_SNAPSHOT",
            "遠端播放清單與此變更檔所依據的快照不一致（可能已被手動更動，或快取已過期）。"
            "請重新執行 fetch --refresh 後重新計算，再寫回。",
            expected_head_fingerprint=compute_head_fingerprint(
                [_IdOnly(i) for i in expected_head]
            ),
            remote_head_fingerprint=compute_head_fingerprint(
                [_IdOnly(i) for i in remote_head]
            ),
        )


def cmd_duplicates(args: argparse.Namespace) -> None:
    """List items that point at the exact same video (0 API units, read-only).

    Deliberately one layer deep: same ``video_id`` and nothing else.  A second
    "probably the same song" tier keyed on title similarity was specified and
    then dropped, because the only time it was measured it was wrong — it
    flagged ``aLIEz`` / ``aLIEz (Remastered)`` and ``sh0ut`` / ``sh0ut
    (Remastered)`` as duplicates by stripping parenthesised text, and those are
    different versions the owner collected on purpose.  Identical ``video_id``
    cannot produce that class of false positive.

    Reports only; nothing is removed.  ``YouTubeAPIClient.delete_item`` stays
    unwired on purpose — deletion is irreversible and costs 50 units a call, so
    it belongs behind the same explicit-consent step as ``update``, if it is
    ever wanted at all.
    """
    emit({"status": "started", "command": "duplicates", "message": "掃描重複影片（0 API units）..."})

    items = _load_items(Path(args.current), "目前清單")
    if not items:
        fail("EMPTY_PLAYLIST", "播放清單是空的。")

    groups = find_duplicate_videos(items)
    redundant = sum(len(g["removable"]) for g in groups)

    emit(
        {
            "status": "success",
            "total_items": len(items),
            "distinct_videos": len({item.video_id for item in items}),
            "duplicate_groups": len(groups),
            "redundant_items": redundant,
            "groups": groups,
            "note": (
                "只比對 video_id 完全相同的項目，不會誤判 Remastered／Live／"
                "Acoustic 等刻意收藏的不同版本。本指令只列出，不會刪除任何東西。"
            ),
        }
    )


def cmd_update(args: argparse.Namespace) -> None:
    from googleapiclient.errors import HttpError

    from scripts.youtube_api import classify_http_error

    emit({"status": "started", "command": "update", "message": "開始寫回播放清單..."})
    platform, playlist_id = detect_platform(args.playlist)
    change_set = read_change_set(Path(args.changes))
    planned_platform = change_set.get("platform") or "youtube"
    if planned_platform != platform:
        fail(
            "MISMATCHED_PLAYLIST",
            f"變更檔是 {planned_platform} 的，但指令指定的是 {platform} 播放清單。",
        )
    if platform == "spotify":
        cmd_update_spotify(args, playlist_id, change_set)
        return

    creds_path = credentials_path_from(args)
    ensure_credentials(creds_path)
    changes: list[PositionChange] = change_set["changes"]

    planned_playlist_id = change_set.get("playlist_id") or ""
    if planned_playlist_id and planned_playlist_id != playlist_id:
        fail(
            "MISMATCHED_PLAYLIST",
            f"變更檔是為播放清單 {planned_playlist_id} 計算的，但指令指定了 {playlist_id}。",
        )

    if not changes:
        emit({"status": "success", "message": "No changes to apply.", "quota_used": 0})
        return

    fingerprint = change_set.get("fingerprint") or compute_change_set_fingerprint(
        playlist_id, changes
    )
    start_index = load_progress(playlist_id, fingerprint)
    if start_index >= len(changes):
        clear_progress(playlist_id)
        PlaylistCache().invalidate(playlist_id)
        emit(
            {
                "status": "success",
                "message": "這份變更集先前已全部套用完成。",
                "total": len(changes),
                "successful": len(changes),
                "quota_used": 0,
            }
        )
        return

    yt_client = get_authenticated_client(creds_path)

    snapshot_ids = list(change_set.get("source_snapshot", {}).get("item_ids", []))
    quota_used = 1  # the verification read below
    if not args.skip_verify:
        _verify_snapshot(yt_client, playlist_id, snapshot_ids, changes[:start_index])
    else:
        quota_used = 0
        logger.warning("Snapshot verification skipped by --skip-verify.")

    result = ExecutionResult(total_changes=len(changes))
    result.successful = start_index
    if start_index:
        logger.info("Resuming from move %d/%d.", start_index + 1, len(changes))

    interrupted = False
    aborted_code = ""
    aborted_message = ""
    applied = start_index

    for index in range(start_index, len(changes)):
        change = changes[index]
        attempt = 0

        while True:
            try:
                yt_client.update_item_position(
                    playlist_item_id=change.playlist_item_id,
                    playlist_id=change.playlist_id or playlist_id,
                    video_id=change.resource_id or change.video_id,
                    new_position=change.new_position,
                )
                quota_used += 50
                applied = index + 1
                result.successful += 1
                save_progress(playlist_id, fingerprint, applied)
                break

            except KeyboardInterrupt:
                interrupted = True
                logger.warning("Update interrupted by user/Agent.")
                break

            except HttpError as exc:
                code, retryable, fatal = classify_http_error(exc)
                if retryable and attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.warning(
                        "%s on move %d; retrying in %ds (attempt %d/%d).",
                        code, index, delay, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue

                if not fatal:
                    quota_used += 50  # the failed write still counted against us
                result.failed += 1
                aborted_code = code
                aborted_message = str(exc)
                result.errors.append(f"{code}: {change.video_id} — {exc}")
                break

            except Exception as exc:  # noqa: BLE001 — surface anything unexpected
                result.failed += 1
                aborted_code = "UNEXPECTED_ERROR"
                aborted_message = str(exc)
                result.errors.append(f"UNEXPECTED_ERROR: {change.video_id} — {exc}")
                logger.exception("Unexpected error on move %d", index)
                break

        if interrupted or aborted_code:
            # The remaining positions were computed assuming this move landed,
            # so continuing would scramble the playlist.  Stop here; the resume
            # file lets the Agent pick up from exactly this point.
            break

        time.sleep(WRITE_PAUSE_SECONDS)

    result.quota_used = quota_used
    completed = applied >= len(changes)

    if completed:
        clear_progress(playlist_id)
    if applied > start_index or completed:
        # Any successful write makes the cached snapshot stale.
        PlaylistCache().invalidate(playlist_id)

    payload: dict[str, Any] = {
        "status": "success" if completed else "partial",
        "total": result.total_changes,
        "successful": result.successful,
        "failed": result.failed,
        "quota_used": result.quota_used,
        "interrupted": interrupted,
        "resume_from": None if completed else applied,
        "errors": result.errors,
    }
    if aborted_code:
        payload["code"] = aborted_code
        payload["message"] = (
            f"第 {applied + 1}/{len(changes)} 筆移動失敗（{aborted_code}）：{aborted_message}。"
            "後續移動的位置是以這一筆已完成為前提計算的，因此已停止。"
            "請重新執行 fetch --refresh 後重新計算變更集。"
        )
    elif not completed:
        payload["message"] = (
            f"已完成 {applied}/{len(changes)} 筆，進度已存檔，可再次執行同一指令續傳。"
        )

    _record_run_result(Path(args.changes), payload)
    emit(payload)
    if not completed:
        sys.exit(1)


def cmd_setup_credentials(args: argparse.Namespace) -> None:
    """Install and validate Google OAuth credentials from a specified path."""
    source = Path(args.path)
    if not source.is_file():
        fail("FILE_NOT_FOUND", f"找不到指定的憑證檔案：{source}")

    # Validate: must be a Google OAuth JSON (contains 'installed' or 'web' top-level key)
    try:
        with source.open(encoding="utf-8") as f:
            data = json.load(f)
        if "installed" not in data and "web" not in data:
            raise ValueError("Missing 'installed' or 'web' key")
    except Exception as exc:
        logger.error("Invalid OAuth JSON at %s: %s", source, exc)
        fail(
            "INVALID_JSON",
            "所選檔案不是合法的 Google OAuth 2.0 憑證。請確認您下載的是『桌面應用程式』類型的 OAuth Client ID。",
        )

    # Copy to secure default location
    try:
        # SECURITY: Ensure the directory and file have secure permissions
        # to prevent unauthorized access to OAuth credentials.
        # Use shutil.copyfile to avoid copying insecure source file permissions.
        DEFAULT_CREDENTIALS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        DEFAULT_CREDENTIALS_PATH.touch(mode=0o600, exist_ok=True)
        DEFAULT_CREDENTIALS_PATH.chmod(0o600)
        shutil.copyfile(source, DEFAULT_CREDENTIALS_PATH)
        logger.info("Credentials installed from %s to %s", source, DEFAULT_CREDENTIALS_PATH)
        emit({"status": "success", "message": "憑證設定成功。", "path": str(DEFAULT_CREDENTIALS_PATH)})
    except Exception as exc:
        logger.exception("Failed to copy credentials")
        fail(
            "COPY_FAILED",
            f"複製憑證失敗：{exc}。請手動將檔案放置到 {DEFAULT_CREDENTIALS_PATH}",
        )


# --- Spotify ---

#: Spotify change sets are block moves (``RangeMove``), not YouTube's
#: remove-then-insert position updates, so they carry their own version.
SPOTIFY_CHANGE_SET_VERSION = 3

#: Spotify reads are cheap (no unit quota), so post-write verification re-reads
#: the whole list; it retries a few times because Spotify can take seconds to
#: make a new snapshot visible.
POST_VERIFY_DELAYS = (0, 2, 5, 10)


def spotify_config_path() -> Path:
    return DEFAULT_CREDENTIALS_DIR / "spotify.json"


def spotify_token_path() -> Path:
    return DEFAULT_CREDENTIALS_DIR / "spotify_token.json"


def spotify_store_key(playlist_id: str) -> str:
    """Cache / progress key; the prefix keeps it apart from YouTube ids."""
    return f"spotify_{playlist_id}"


def detect_platform(raw: str) -> tuple[str, str]:
    """Return ``(platform, playlist_id)`` for a URL, URI or bare id.

    Anything that is not recognisably Spotify is treated as YouTube, which is
    what every pre-v4 command line already passes.
    """
    from scripts.spotify_api import extract_spotify_playlist_id

    spotify_id = extract_spotify_playlist_id(raw)
    if spotify_id:
        return "spotify", spotify_id
    return "youtube", extract_id(raw)


def get_spotify_client() -> "SpotifyClient":
    from scripts.spotify_api import SpotifyClient, SpotifyError

    config_path = spotify_config_path()
    try:
        client_id = json.loads(config_path.read_text(encoding="utf-8"))["client_id"]
    except (OSError, ValueError, KeyError):
        fail(
            "SPOTIFY_NOT_CONFIGURED",
            "尚未設定 Spotify Client ID。請照 docs/SPOTIFY_SETUP.md 在 Spotify 開發者後台建立 App，"
            "再執行 setup_spotify <client_id>。",
            expected_path=str(config_path),
        )
    client = SpotifyClient(client_id=client_id, token_path=spotify_token_path())
    try:
        client.authenticate()
    except SpotifyError as exc:
        fail(exc.code, f"Spotify 授權失敗：{exc}")
    return client


def _fail_spotify(exc: Exception, context: str) -> None:
    code = getattr(exc, "code", "SPOTIFY_ERROR")
    fail(code, f"{context}：{exc}")


def cmd_fetch_spotify(args: argparse.Namespace, playlist_id: str) -> None:
    from scripts.spotify_api import SpotifyError, entries_to_items

    store_key = spotify_store_key(playlist_id)
    cache = PlaylistCache()
    cached = None if args.refresh else cache.get(store_key)
    api_calls = 0

    if cached:
        items, source, snapshot_id = cached.items, "cache", cached.etag
        fetched_at = cached.created_at.isoformat()
    else:
        client = get_spotify_client()
        try:
            snapshot_id = client.get_snapshot_id(playlist_id)
            entries = client.get_playlist_entries(playlist_id)
        except SpotifyError as exc:
            _fail_spotify(exc, "讀取 Spotify 播放清單失敗")
        if not entries:
            fail("EMPTY_PLAYLIST", "播放清單是空的或無法存取。")
        items = entries_to_items(entries, playlist_id)
        cache.set(store_key, items, etag=snapshot_id)
        source, fetched_at, api_calls = "api", datetime.now(timezone.utc).isoformat(), client.calls

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    missing = sum(1 for item in items if not item.metadata_available)
    emit(
        {
            "status": "success",
            "platform": "spotify",
            "item_count": len(items),
            "hidden_count": 0,
            "metadata_missing_count": missing,
            "metadata_missing_note": (
                "有項目沒有曲目資料（通常是無法播放或已下架），仍可正常移動位置，"
                "分組時會落入 unknown。" if missing else ""
            ),
            "source": source,
            "fetched_at": fetched_at,
            "snapshot_id": snapshot_id,
            "api_calls": api_calls,
            "file": str(out_path),
        }
    )


def _range_fingerprint(playlist_id: str, moves: list[RangeMove]) -> str:
    import hashlib

    lines = [f"{m.range_start}:{m.insert_before}:{m.range_length}" for m in moves]
    text = f"spotify:{playlist_id}\n" + "\n".join(lines)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_spotify_change_set(
    path: Path,
    playlist_id: str,
    current_items: list[EnrichedPlaylistItem],
    target_items: list[EnrichedPlaylistItem],
    moves: list[RangeMove],
    report_extra: dict[str, Any],
) -> str:
    from scripts.spotify_api import uri_of_key

    fingerprint = _range_fingerprint(playlist_id, moves)
    payload = {
        "version": SPOTIFY_CHANGE_SET_VERSION,
        "platform": "spotify",
        "playlist_id": playlist_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "execution": "sequential",
        "note": (
            "依 execution_order 由小到大逐筆執行；每一筆是一次區段搬移。不可重新排序、跳過或平行執行。"
            "range_start / insert_before 是送出當下的索引，final_position 才是最終索引。"
        ),
        "source_snapshot": {
            "item_count": len(current_items),
            "item_keys": [item.playlist_item_id for item in current_items],
            "uris": [uri_of_key(item.playlist_item_id) for item in current_items],
        },
        "target_uris": [uri_of_key(item.playlist_item_id) for item in target_items],
        "report": report_extra,
        "ops": [m.model_dump(mode="json") for m in moves],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fingerprint


def _spotify_plan_and_write(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
    out_path: Path,
    report_extra: dict[str, Any],
) -> tuple[list[RangeMove], dict[str, int], str]:
    try:
        moves, stats = plan_range_reorder(current, target)
    except ValueError as exc:
        fail("TARGET_MISMATCH", str(exc))
    fingerprint = write_spotify_change_set(
        out_path,
        _playlist_id_of(current),
        current,
        normalize_target(current, target),
        moves,
        {**stats, **report_extra},
    )
    return moves, stats, fingerprint


def cmd_optimize_spotify(args: argparse.Namespace, items: list[EnrichedPlaylistItem]) -> None:
    group_by = args.group_by or "artist"
    within_group_sort = _parse_within_group_sort(args.within_group_sort)
    target, groups, resolutions = group_by_structured_field(
        items, group_by, args.group_order or "first_appearance", within_group_sort
    )
    benefit = evaluate_grouping_benefit(groups, resolutions)

    group_of = {
        item.playlist_item_id: ("" if res.method == "unavailable" else res.artist_key)
        for item, res in zip(items, resolutions)
    }
    target_path = Path(args.target_out)
    _write_target(target_path, target, group_of)
    out_path = Path(args.out)
    _moves, stats, fingerprint = _spotify_plan_and_write(
        items, target, out_path, {"group_by": group_by, "groups_found": list(groups)}
    )
    emit(
        {
            "status": "success",
            "platform": "spotify",
            "group_by": group_by,
            "total_items": stats["total_items"],
            "estimated_calls": stats["calls"],
            "items_moved": stats["items_moved"],
            "item_level_moves": stats["item_level_moves"],
            "estimated_quota": 0,
            "pinned_count": stats["pinned_count"],
            "groups_found": list(groups),
            "group_details": {k: len(v) for k, v in groups.items()},
            "unresolved_count": sum(1 for r in resolutions if r.method == "unknown"),
            "unknown_ratio": benefit.unknown_ratio,
            "effective_grouping_ratio": benefit.effective_grouping_ratio,
            "orphan_group_count": benefit.orphan_group_count,
            "grouping_benefit": benefit.verdict,
            "grouping_warnings": benefit.warnings,
            "aliases_ignored": bool(args.aliases),
            "fingerprint": fingerprint,
            "target_file": str(target_path),
            "changes_file": str(out_path),
        }
    )


def read_spotify_change_set(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        moves = [RangeMove.model_validate(x) for x in raw.get("ops", [])]
    except Exception as exc:
        fail("PARSE_ERROR", f"變更檔內容格式錯誤：{exc}")
    moves.sort(key=lambda m: m.execution_order)
    if [m.execution_order for m in moves] != list(range(len(moves))):
        fail("CORRUPT_CHANGE_SET", "execution_order 不連續，變更檔可能已被手動修改。")
    if raw.get("fingerprint") != _range_fingerprint(raw.get("playlist_id", ""), moves):
        fail("CORRUPT_CHANGE_SET", "變更檔指紋不符，可能已被手動修改。請重新執行 optimize 或 diff。")
    snapshot = raw.get("source_snapshot") or {}
    keys = list(snapshot.get("item_keys") or [])
    if not keys or len(keys) != len(raw.get("target_uris") or []):
        fail("CORRUPT_CHANGE_SET", "變更檔缺少快照或目標順序。請重新執行 optimize 或 diff。")
    raw["ops"] = moves
    return raw


def _replay_states(keys: list[str], moves: list[RangeMove]) -> list[list[str]]:
    """``states[k]`` is the uri order after the first *k* moves."""
    from scripts.spotify_api import uri_of_key

    states = [[uri_of_key(k) for k in keys]]
    live = list(keys)
    for move in moves:
        live = apply_range_move(live, move.range_start, move.insert_before, move.range_length)
        states.append([uri_of_key(k) for k in live])
    return states


def cmd_update_spotify(args: argparse.Namespace, playlist_id: str, change_set: dict[str, Any]) -> None:
    from scripts.spotify_api import SpotifyError

    planned = change_set.get("playlist_id") or ""
    if planned and planned != playlist_id:
        fail("MISMATCHED_PLAYLIST", f"變更檔是為播放清單 {planned} 計算的，但指令指定了 {playlist_id}。")

    moves: list[RangeMove] = change_set["ops"]
    store_key = spotify_store_key(playlist_id)
    if not moves:
        emit({"status": "success", "platform": "spotify", "message": "No changes to apply.", "api_calls": 0})
        return

    fingerprint = change_set["fingerprint"]
    states = _replay_states(change_set["source_snapshot"]["item_keys"], moves)
    client = get_spotify_client()

    # Where is the remote right now?  Spotify reads are free of unit quota, so
    # compare the whole list (YouTube can only afford the first page).
    try:
        snapshot_id = client.get_snapshot_id(playlist_id)
        remote = client.get_playlist_uris(playlist_id)
    except SpotifyError as exc:
        _fail_spotify(exc, "寫回前的清單驗證失敗")

    progress = load_progress(store_key, fingerprint)
    candidates = [progress] + [k for k in range(len(states)) if k != progress]
    start = next((k for k in candidates if k < len(states) and states[k] == remote), None)
    if start is None:
        fail(
            "STALE_SNAPSHOT",
            "遠端播放清單與此變更檔所依據的快照不一致（可能已在 Spotify 上被更動）。"
            "請重新執行 fetch --refresh 後重新計算，再寫回。",
            api_calls=client.calls,
        )
    if start:
        logger.info("Spotify resume: remote matches the state after %d/%d moves.", start, len(moves))

    applied = start
    interrupted = False
    aborted_code = aborted_message = ""
    for index in range(start, len(moves)):
        move = moves[index]
        attempt = 0
        while True:
            try:
                snapshot_id = client.reorder(
                    playlist_id, move.range_start, move.insert_before, move.range_length, snapshot_id
                ) or snapshot_id
                applied = index + 1
                save_progress(store_key, fingerprint, applied)
                break
            except KeyboardInterrupt:
                interrupted = True
                break
            except SpotifyError as exc:
                if exc.ambiguous and attempt < MAX_RETRIES:
                    # A reorder is not idempotent: look before retrying.
                    time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                    attempt += 1
                    try:
                        remote = client.get_playlist_uris(playlist_id)
                        snapshot_id = client.get_snapshot_id(playlist_id)
                    except SpotifyError:
                        continue
                    if remote == states[index + 1]:
                        applied = index + 1
                        save_progress(store_key, fingerprint, applied)
                        break
                    if remote == states[index]:
                        continue
                    aborted_code, aborted_message = "STALE_SNAPSHOT", "寫入結果不確定，且遠端順序已與預期不符。"
                    break
                aborted_code, aborted_message = exc.code, str(exc)
                break
        if interrupted or aborted_code:
            break
        time.sleep(WRITE_PAUSE_SECONDS)

    completed = applied >= len(moves)
    verified = False
    if completed:
        clear_progress(store_key)
        for delay in POST_VERIFY_DELAYS:
            time.sleep(delay)
            try:
                if client.get_playlist_uris(playlist_id) == change_set["target_uris"]:
                    verified = True
                    break
            except SpotifyError:
                continue
    if applied > start or completed:
        PlaylistCache().invalidate(store_key)

    payload: dict[str, Any] = {
        "status": "success" if completed and verified else ("error" if completed else "partial"),
        "platform": "spotify",
        "total": len(moves),
        "successful": applied,
        "resumed_from": start,
        "api_calls": client.calls,
        "quota_used": 0,
        "interrupted": interrupted,
        "resume_from": None if completed else applied,
        "verified": verified,
    }
    if completed and not verified:
        payload["code"] = "POST_VERIFY_MISMATCH"
        payload["message"] = (
            "所有搬移都已送出，但重新讀取的順序與目標不符（可能有人同時在 Spotify 上更動）。"
            "請 fetch --refresh 後重新計算。"
        )
    elif aborted_code:
        payload["code"] = aborted_code
        payload["message"] = (
            f"第 {applied + 1}/{len(moves)} 筆搬移失敗（{aborted_code}）：{aborted_message}。"
            "清單目前只是部分重排、項目沒有增減。再次執行同一指令會從遠端實際狀態續傳；"
            "若要回到原順序，fetch --refresh 後對原本的 current.json 執行 diff。"
        )
    elif interrupted:
        payload["message"] = f"已完成 {applied}/{len(moves)} 筆，再次執行同一指令即可續傳。"
    _record_run_result(Path(args.changes), payload)
    emit(payload)
    if payload["status"] != "success":
        sys.exit(1)


def cmd_setup_spotify(args: argparse.Namespace) -> None:
    from scripts.spotify_api import CLIENT_ID_PATTERN, REDIRECT_URI, _write_private_json

    client_id = args.client_id.strip().lower()
    if not CLIENT_ID_PATTERN.match(client_id):
        fail(
            "INVALID_CLIENT_ID",
            "這不是有效的 Spotify Client ID（應為 32 個英數字元，在 App 的 Settings → Basic Information）。"
            "注意不要貼成 Client Secret。",
        )
    path = spotify_config_path()
    try:
        _write_private_json(path, {"client_id": client_id, "redirect_uri": REDIRECT_URI})
    except OSError as exc:
        fail("COPY_FAILED", f"無法寫入 {path}：{exc}")
    emit(
        {
            "status": "success",
            "message": "Spotify Client ID 已設定。第一次讀取清單時會開啟瀏覽器請你按「同意」。",
            "path": str(path),
            "redirect_uri": REDIRECT_URI,
        }
    )


def cmd_spotify_check(args: argparse.Namespace) -> None:
    """Read-only smoke test: sign-in works, and (optionally) the playlist is reorderable."""
    from scripts.spotify_api import SpotifyError

    emit({"status": "started", "command": "spotify-check", "message": "檢查 Spotify 連線（唯讀）..."})
    client = get_spotify_client()
    report: dict[str, Any] = {"status": "success", "scopes": client._token.get("scope", "")}
    try:
        me = client.me()
        report["user"] = {"id": me.get("id", ""), "display_name": me.get("display_name", "")}
        if args.playlist:
            platform, playlist_id = detect_platform(args.playlist)
            if platform != "spotify":
                fail("NOT_SPOTIFY", "spotify-check 只接受 Spotify 播放清單網址或 URI。")
            meta = client.get_playlist_meta(playlist_id)
            first_page = client._request(
                "GET", f"/playlists/{playlist_id}/items", params={"limit": 1}
            )
            owner = (meta.get("owner") or {}).get("id", "")
            report["playlist"] = {
                "id": playlist_id,
                "name": meta.get("name", ""),
                "snapshot_id": meta.get("snapshot_id", ""),
                "total_items": first_page.get("total"),
                "owner_is_you": owner == report["user"]["id"],
                "collaborative": bool(meta.get("collaborative")),
                "can_reorder": owner == report["user"]["id"] or bool(meta.get("collaborative")),
            }
    except SpotifyError as exc:
        _fail_spotify(exc, "Spotify 連線檢查失敗")
    report["api_calls"] = client.calls
    emit(report)


# --- Review page ---

#: Each review gets its own directory, so nothing is overwritten by the next
#: run (docs/BACKLOG.md §5).  Override with --run-root (the e2e suite does).
RUNS_ROOT = Path(__file__).resolve().parent.parent / "runs"
REVIEW_WAIT_DEFAULT = 540  # stays under the 10-minute foreground limit of Agent hosts
SERVER_START_TIMEOUT = 15


def _detach_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        return {"creationflags": flags}
    return {"start_new_session": True}


def cmd_review_start(args: argparse.Namespace) -> None:
    """Create a run directory, start the review server, open the browser."""
    import secrets
    import webbrowser

    from scripts import review_server as rs

    current_path, proposed_path = Path(args.current), Path(args.proposed)
    current = _load_items(current_path, "目前清單")
    proposed = _load_items(proposed_path, "建議順序")
    if not current:
        fail("EMPTY_PLAYLIST", "播放清單是空的。")
    if sorted(i.playlist_item_id for i in current) != sorted(i.playlist_item_id for i in proposed):
        fail("TARGET_MISMATCH", "建議順序不是目前清單的重排（有項目遺失或多出來）。")

    platform = current[0].platform
    safe_id = "".join(c for c in _playlist_id_of(current, "unknown") if c.isalnum() or c in "-_")[:40]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.run_root or RUNS_ROOT) / f"{platform}_{safe_id}_{stamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = run_dir.with_name(f"{platform}_{safe_id}_{stamp}-{suffix}")
    run_dir.mkdir(parents=True)

    shutil.copyfile(current_path, run_dir / rs.SNAPSHOT_FILE)
    shutil.copyfile(proposed_path, run_dir / rs.PROPOSED_FILE)
    token = secrets.token_urlsafe(18)
    (run_dir / rs.STATE_FILE).write_text(
        json.dumps({"token": token, "mode": args.mode, "created_at": datetime.now(timezone.utc).isoformat(),
                    "current_file": str(current_path), "proposed_file": str(proposed_path)}, indent=2),
        encoding="utf-8",
    )

    log_handle = (run_dir / "server.log").open("w", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-m", "scripts.review_server", str(run_dir)],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdin=subprocess.DEVNULL, stdout=log_handle, stderr=log_handle, close_fds=True,
        **_detach_kwargs(),
    )
    log_handle.close()

    server_file = run_dir / rs.SERVER_FILE
    deadline = time.time() + SERVER_START_TIMEOUT
    while not server_file.is_file():
        if time.time() > deadline:
            fail("REVIEW_START_FAILED", "檢查頁伺服器沒有啟動。", run_dir=str(run_dir),
                 server_log=(run_dir / "server.log").read_text(encoding="utf-8")[-2000:])
        time.sleep(0.1)
    port = json.loads(server_file.read_text(encoding="utf-8"))["port"]
    url = f"http://127.0.0.1:{port}/?t={token}"

    opened = False
    if not args.no_browser:
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 — no browser is a normal outcome, not an error
            opened = False

    emit(
        {
            "status": "started",
            "mode": args.mode,
            "platform": platform,
            "item_count": len(current),
            "run_dir": str(run_dir),
            "url": url,
            "browser_opened": opened,
            "next": f"review wait {run_dir}",
            "note": (
                "已在瀏覽器開啟檢查頁。" if opened else
                "沒有自動開啟瀏覽器：請使用者手動打開 url；無法使用瀏覽器時，改在聊天室列出前 10 筆移動與總數。"
            ),
        }
    )


def _review_summary(run_dir: Path) -> dict[str, Any]:
    """Turn a saved review into a change set + the one-line cost the Agent reports."""
    from scripts import review_server as rs

    saved = json.loads((run_dir / rs.SAVED_FILE).read_text(encoding="utf-8"))
    if saved.get("mode") == "label":
        return {"status": "saved", "mode": "label", "run_dir": str(run_dir),
                "labels_file": str(run_dir / rs.LABELS_FILE), **saved}

    snapshot = _load_items(run_dir / rs.SNAPSHOT_FILE, "快照")
    edited = _load_items(run_dir / rs.EDITED_FILE, "保存的順序")
    proposed = _load_items(run_dir / rs.PROPOSED_FILE, "建議順序")
    changes_path = run_dir / "changes.json"
    edits, relabels = saved.get("edits", 0), saved.get("relabels", 0)
    touched = f"你在檢查頁改了 {edits} 處" if edits else "你沒有修改建議"

    if snapshot[0].platform == "spotify":
        _moves, stats, fingerprint = _spotify_plan_and_write(snapshot, edited, changes_path, {"source": "review"})
        _p, proposed_stats = plan_range_reorder(snapshot, proposed)
        cost = {"estimated_calls": stats["calls"], "items_moved": stats["items_moved"], "estimated_quota": 0,
                "proposed_calls": proposed_stats["calls"]}
        line = (f"{touched}。寫回要搬 {stats['items_moved']} 首、呼叫 Spotify API {stats['calls']} 次"
                f"（Spotify 沒有每日配額），要寫回嗎？")
        nothing = stats["calls"] == 0
    else:
        try:
            changes, report = plan_reorder(snapshot, edited)
            proposed_changes, _r = plan_reorder(snapshot, proposed)
        except ValueError as exc:
            fail("TARGET_MISMATCH", str(exc))
        fingerprint = write_change_set(
            changes_path, _playlist_id_of(snapshot), snapshot, changes,
            {"source": "review", "anchors": report.anchors, "need_to_move": report.need_to_move,
             "pinned_count": report.pinned_count},
        )
        quota = 1 + 50 * len(changes) if changes else 0
        cost = {"need_to_move": len(changes), "estimated_quota": quota, "proposed_moves": len(proposed_changes)}
        line = f"{touched}。寫回要搬 {len(changes)} 首、約 {quota} units（每日上限 10,000），要寫回嗎？"
        nothing = not changes

    if nothing:
        line = f"{touched}。保存的順序和目前清單一樣，不需要寫回。"
    return {
        "status": "saved", "mode": "order", "run_dir": str(run_dir), "edits": edits, "relabels": relabels,
        "repinned": saved.get("repinned", False), **cost, "nothing_to_do": nothing,
        "changes_file": str(changes_path), "fingerprint": fingerprint, "summary_line": line,
        "next": "使用者明確回覆同意後：update <playlist_url> " + str(changes_path),
    }


def wait_for_review(run_dir: Path, timeout: float) -> dict[str, Any]:
    from scripts import review_server as rs

    deadline = time.time() + max(0.0, timeout)
    while True:
        if (run_dir / rs.SAVED_FILE).is_file():
            return _review_summary(run_dir)
        if (run_dir / rs.CANCELLED_FILE).is_file():
            return {"status": "cancelled", "run_dir": str(run_dir),
                    "message": "使用者在檢查頁按了取消，不寫回。"}
        server_file = run_dir / rs.SERVER_FILE
        if server_file.is_file():
            info = json.loads(server_file.read_text(encoding="utf-8"))
            if info.get("stopped_at"):
                return {"status": "expired", "run_dir": str(run_dir),
                        "message": "檢查頁閒置太久已關閉，沒有保存。需要的話重新執行 review start。"}
        if time.time() >= deadline:
            return {"status": "waiting", "run_dir": str(run_dir),
                    "message": "使用者還在檢查頁上，再執行一次 review wait 繼續等。"}
        time.sleep(0.5)


def cmd_review_wait(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    if not (run_dir / "review_state.json").is_file():
        fail("RUN_NOT_FOUND", f"找不到檢查頁的執行目錄：{run_dir}")
    emit(wait_for_review(run_dir, args.timeout))


def cmd_review_cancel(args: argparse.Namespace) -> None:
    """Close a review from the Agent side (the user abandoned it in chat)."""
    import urllib.request

    run_dir = Path(args.run_dir)
    try:
        state = json.loads((run_dir / "review_state.json").read_text(encoding="utf-8"))
        port = json.loads((run_dir / "server.json").read_text(encoding="utf-8"))["port"]
    except (OSError, ValueError, KeyError):
        fail("RUN_NOT_FOUND", f"找不到執行中的檢查頁：{run_dir}")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/cancel?t={state['token']}", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except OSError:
        pass  # already closed
    emit({"status": "cancelled", "run_dir": str(run_dir)})


def _record_run_result(changes_path: Path, payload: dict[str, Any]) -> None:
    """When the change set lives in a review run directory, keep the outcome next to it."""
    run_dir = changes_path.resolve().parent
    if (run_dir / "snapshot.json").is_file() and (run_dir / "review_state.json").is_file():
        (run_dir / "update_result.json").write_text(
            json.dumps({**payload, "finished_at": datetime.now(timezone.utc).isoformat()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# --- Main ---


def main():
    parser = argparse.ArgumentParser(description="YouTube Playlist Agent-Skill Background Tool")
    parser.add_argument("--credentials", type=Path, help="Path to client_secret.json (optional)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        # Also accept --credentials *after* the subcommand, which is what most
        # people (and Agents) type first.  SUPPRESS keeps the subparser from
        # overwriting a value already given before the subcommand.
        sub.add_argument("--credentials", type=Path, default=argparse.SUPPRESS,
                         help="Path to client_secret.json (optional)")

    p_setup_spotify = subparsers.add_parser("setup_spotify")
    p_setup_spotify.add_argument("client_id", help="Client ID of your Spotify Dashboard app")
    p_setup_spotify.set_defaults(func=cmd_setup_spotify)

    p_check = subparsers.add_parser("spotify-check", help="Read-only Spotify connection check")
    p_check.add_argument("playlist", nargs="?", default="", help="Spotify playlist URL / URI (optional)")
    p_check.set_defaults(func=cmd_spotify_check)

    p_review = subparsers.add_parser("review", help="Drag-and-drop review page in the browser")
    review_sub = p_review.add_subparsers(dest="review_command", required=True)
    p_rstart = review_sub.add_parser("start", help="Create a run directory and open the review page")
    p_rstart.add_argument("current", help="Current playlist JSON (from fetch)")
    p_rstart.add_argument("proposed", help="Proposed order JSON (from optimize --target-out, or written by the Agent)")
    p_rstart.add_argument("--mode", choices=["order", "label"], default="order",
                          help="order: check/adjust the new order; label: assign groups (ground truth)")
    p_rstart.add_argument("--no-browser", action="store_true", help="Do not open a browser (print the URL only)")
    p_rstart.add_argument("--run-root", default=None, help="Where run directories go (default: <repo>/runs)")
    p_rstart.set_defaults(func=cmd_review_start)
    p_rwait = review_sub.add_parser("wait", help="Wait for the user to save or cancel")
    p_rwait.add_argument("run_dir")
    p_rwait.add_argument("--timeout", type=float, default=REVIEW_WAIT_DEFAULT,
                         help="Seconds to wait before returning status=waiting (run again to keep waiting)")
    p_rwait.set_defaults(func=cmd_review_wait)
    p_rcancel = review_sub.add_parser("cancel", help="Close an open review page without saving")
    p_rcancel.add_argument("run_dir")
    p_rcancel.set_defaults(func=cmd_review_cancel)

    p_setup = subparsers.add_parser("setup_credentials")
    p_setup.add_argument("path", help="Path to client_secret.json to install")
    p_setup.set_defaults(func=cmd_setup_credentials)

    p_fetch = subparsers.add_parser("fetch")
    p_fetch.add_argument("playlist", help="Playlist ID or URL")
    p_fetch.add_argument("--out", required=True, help="Output JSON file path")
    p_fetch.add_argument("--refresh", action="store_true",
                         help="Bypass the local cache and read from the API")
    add_common(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_diff = subparsers.add_parser("diff")
    p_diff.add_argument("old", help="Old playlist JSON file")
    p_diff.add_argument("new", help="New playlist JSON file")
    p_diff.add_argument("--out", required=True, help="Changes JSON output path")
    p_diff.set_defaults(func=cmd_diff)

    p_dupes = subparsers.add_parser("duplicates")
    p_dupes.add_argument("current", help="Current playlist JSON file (from fetch)")
    p_dupes.set_defaults(func=cmd_duplicates)

    p_update = subparsers.add_parser("update")
    p_update.add_argument("playlist", help="Playlist ID or URL")
    p_update.add_argument("changes", help="Changes JSON file to apply")
    p_update.add_argument("--skip-verify", action="store_true",
                          help="Skip the 1-unit staleness check (not recommended)")
    add_common(p_update)
    p_update.set_defaults(func=cmd_update)

    p_optimize = subparsers.add_parser("optimize", help="Compute optimized reorder changes (local, 0 API units)")
    p_optimize.add_argument("current", help="Current playlist JSON file (from fetch)")
    p_optimize.add_argument("--target-out", required=True, help="Output path for target ordering JSON")
    p_optimize.add_argument("--out", required=True, help="Output path for optimized changes JSON")
    p_optimize.add_argument("--aliases", default=None, help="Path to artist_aliases.json (optional)")
    p_optimize.add_argument("--group-order", default="first_appearance",
                            choices=["first_appearance", "alphabetical", "count_desc"],
                            help="Group ordering strategy")
    p_optimize.add_argument("--within-group-sort", default=None,
                            help="Sort inside each group, e.g. viewCount:desc / trackNumber:asc")
    p_optimize.add_argument("--group-by", default="artist", choices=["artist", "album"],
                            help="Grouping key; album is Spotify-only")
    p_optimize.set_defaults(func=cmd_optimize)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
