"""
yt_tool.py — Background tool for YouTube Playlist Agent-Skill.

This script is designed to be executed by AI Agents, NOT the human user.
It provides simple CLI commands to interact with the YouTube API.

Features:
- Regex-based Playlist ID extraction from YouTube/YT Music URLs.
- Agent-oriented logging: stdout only emits JSON or plain text data results for
  the Agent to parse, while detailed debug/trace logs go to logs/yt_skill.log.
- Custom credentials path support (defaults to ~/.gemini/skills/yt-playlist-manager/credentials,
  override with $YT_SKILL_HOME or --credentials).

Commands:
1. setup_credentials <path>
2. fetch <playlist_id_or_url> --out <output.json> [--refresh]
3. optimize <current.json> --target-out <new.json> --out <changes.json>
4. diff <old_playlist.json> <new_playlist.json> --out <changes.json>
5. update <playlist_id_or_url> <changes.json>

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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.cache_manager import PlaylistCache
from scripts.executor import estimate_quota
from scripts.optimizer import plan_reorder, run_full_optimization
from scripts.schemas import (
    EnrichedPlaylistItem,
    ExecutionResult,
    PositionChange,
    SortConfig,
    SortField,
    SortOrder,
    compute_change_set_fingerprint,
    compute_head_fingerprint,
)

if TYPE_CHECKING:  # pragma: no cover
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
WRITE_PAUSE_SECONDS = 0.3

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
    """Extract Playlist ID from URL using regex, or return raw if it looks like an ID."""
    match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)
    return raw.strip()


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
    creds_path = credentials_path_from(args)
    ensure_credentials(creds_path)

    playlist_id = extract_id(args.playlist)
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
    emit(
        {
            "status": "success",
            "item_count": len(items),
            "hidden_count": hidden_count,
            "hidden_note": (
                "私人／已刪除影片仍佔用位置，已保留為釘選項目且不會被移動。"
                if hidden_count
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
            "changes_count": len(changes),
            "anchors": report.anchors,
            "pinned_count": report.pinned_count,
            "estimated_quota": estimate_quota(changes),
            "quota_saved_vs_naive": report.quota_saved_vs_naive,
            "fingerprint": fingerprint,
            "file": str(out_path),
        }
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

    aliases_path = Path(args.aliases) if args.aliases else None
    group_order = args.group_order or "first_appearance"
    within_group_sort = _parse_within_group_sort(args.within_group_sort)

    logger.info(
        "Running optimization: %d items, group_order=%s, within_group_sort=%s, aliases=%s",
        len(current_items), group_order, args.within_group_sort, aliases_path,
    )

    try:
        target, changes, report, _ = run_full_optimization(
            current_items,
            aliases_path=aliases_path,
            group_order=group_order,
            within_group_sort=within_group_sort,
        )
    except ValueError as exc:
        fail("TARGET_MISMATCH", str(exc))

    target_path = Path(args.target_out)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in target], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

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
        },
    )

    emit(
        {
            "status": "success",
            "total_items": report.total_items,
            "anchors": report.anchors,
            "need_to_move": report.need_to_move,
            "pinned_count": report.pinned_count,
            "estimated_quota": report.estimated_quota,
            "quota_saved_vs_naive": report.quota_saved_vs_naive,
            "groups_found": report.groups_found,
            "group_details": report.group_details,
            "unresolved_count": report.unresolved_count,
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


def cmd_update(args: argparse.Namespace) -> None:
    from googleapiclient.errors import HttpError

    from scripts.youtube_api import classify_http_error

    emit({"status": "started", "command": "update", "message": "開始寫回播放清單..."})
    creds_path = credentials_path_from(args)
    ensure_credentials(creds_path)

    playlist_id = extract_id(args.playlist)
    change_set = read_change_set(Path(args.changes))
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
                            help="Sort inside each group, e.g. viewCount:desc / duration:asc")
    p_optimize.set_defaults(func=cmd_optimize)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
