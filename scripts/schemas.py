"""
schemas.py — Pydantic Schema 定義

所有資料模型的單一來源。分為兩類：
1. LLM 結構化輸出 Schema（PlaylistCommand 及其子模型）
2. 內部資料模型（PlaylistItem, VideoMetadata, PositionChange 等）
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# 1. LLM 結構化輸出 Schema
# ─────────────────────────────────────────────


class SortField(str, Enum):
    """可排序的欄位。"""
    VIEW_COUNT = "viewCount"
    PUBLISHED_AT = "publishedAt"
    DURATION = "duration"
    TITLE = "title"
    CHANNEL_TITLE = "channelTitle"
    ADDED_AT = "addedAt"


class SortOrder(str, Enum):
    """排序方向。"""
    ASC = "asc"
    DESC = "desc"


class ActionType(str, Enum):
    """支援的操作類型。"""
    SORT = "sort"
    FILTER = "filter"
    FILTER_THEN_SORT = "filter_then_sort"
    DELETE_ITEMS = "delete_items"


class SortConfig(BaseModel):
    """排序設定。"""
    field: SortField = Field(description="要排序的欄位")
    order: SortOrder = Field(default=SortOrder.DESC, description="排序方向：asc 升序 / desc 降序")


class FilterConfig(BaseModel):
    """篩選條件。所有欄位皆為可選，未填則不套用該條件。"""
    channel: Optional[str] = Field(default=None, description="頻道名稱篩選（包含比對）")
    duration_min_seconds: Optional[int] = Field(default=None, description="最短時長（秒）")
    duration_max_seconds: Optional[int] = Field(default=None, description="最長時長（秒）")
    published_after: Optional[str] = Field(default=None, description="發布日期下限（YYYY-MM-DD）")
    published_before: Optional[str] = Field(default=None, description="發布日期上限（YYYY-MM-DD）")
    title_contains: Optional[str] = Field(default=None, description="標題包含關鍵字")
    title_excludes: Optional[str] = Field(default=None, description="標題排除關鍵字")


class PlaylistCommand(BaseModel):
    """LLM 解析使用者自然語言後的結構化指令。"""
    action: ActionType = Field(description="操作類型")
    sort: Optional[SortConfig] = Field(default=None, description="排序設定（action 為 sort 或 filter_then_sort 時必填）")
    filter: Optional[FilterConfig] = Field(default=None, description="篩選條件（action 為 filter 或 filter_then_sort 時必填）")
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="LLM 對解析結果的信心分數，0.0-1.0"
    )


# ─────────────────────────────────────────────
# 2. 內部資料模型
# ─────────────────────────────────────────────


class PlaylistItemData(BaseModel):
    """YouTube Playlist Item 的資料模型。"""
    playlist_item_id: str = Field(description="playlistItems 的唯一 ID（用於 update/delete）")
    video_id: str = Field(description="影片的 video ID")
    position: int = Field(description="目前在 playlist 中的位置（0-indexed）")
    added_at: datetime = Field(description="加入 playlist 的時間")
    channel_title: str = Field(default="", description="影片頻道名稱")
    playlist_id: str = Field(default="", description="所屬 playlist ID")


class VideoMetadata(BaseModel):
    """影片的詳細 metadata（來自 videos.list）。"""
    video_id: str
    title: str = ""
    description: str = ""
    channel_title: str = ""
    published_at: Optional[datetime] = None
    duration_seconds: int = 0
    duration_raw: str = ""  # ISO 8601 原始值，如 PT4M30S
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    tags: list[str] = Field(default_factory=list)
    privacy_status: str = "public"


class EnrichedPlaylistItem(BaseModel):
    """合併 PlaylistItemData + VideoMetadata 的完整資料。"""
    playlist_item_id: str
    video_id: str
    position: int
    added_at: datetime
    playlist_id: str = ""

    # 來自 VideoMetadata
    title: str = ""
    channel_title: str = ""
    published_at: Optional[datetime] = None
    duration_seconds: int = 0
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    tags: list[str] = Field(default_factory=list)
    privacy_status: str = "public"

    @classmethod
    def from_item_and_metadata(
        cls,
        item: PlaylistItemData,
        metadata: VideoMetadata | None,
    ) -> "EnrichedPlaylistItem":
        """合併 playlist item 與 video metadata。"""
        base = {
            "playlist_item_id": item.playlist_item_id,
            "video_id": item.video_id,
            "position": item.position,
            "added_at": item.added_at,
            "playlist_id": item.playlist_id,
        }
        if metadata:
            base.update({
                "title": metadata.title,
                "channel_title": metadata.channel_title or item.channel_title,
                "published_at": metadata.published_at,
                "duration_seconds": metadata.duration_seconds,
                "view_count": metadata.view_count,
                "like_count": metadata.like_count,
                "comment_count": metadata.comment_count,
                "tags": metadata.tags,
                "privacy_status": metadata.privacy_status,
            })
        else:
            base["channel_title"] = item.channel_title
        return cls(**base)


class PositionChange(BaseModel):
    """描述一個 item 的位置變更。"""
    playlist_item_id: str
    video_id: str
    title: str = ""
    old_position: int
    new_position: int
    playlist_id: str = ""
    resource_id: str = ""  # videoId，update API 需要

    @property
    def is_changed(self) -> bool:
        return self.old_position != self.new_position


class CachedPlaylist(BaseModel):
    """快取的 playlist 快照。"""
    playlist_id: str
    items: list[EnrichedPlaylistItem]
    created_at: datetime
    item_count: int = 0
    etag: str = ""
    ttl_minutes: int = 30

    @property
    def is_expired(self) -> bool:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        created = self.created_at
        if created.tzinfo is None:
            from datetime import timezone as tz
            created = created.replace(tzinfo=tz.utc)
        elapsed = (now - created).total_seconds() / 60
        return elapsed > self.ttl_minutes


class ExecutionResult(BaseModel):
    """寫回操作的執行結果。"""
    total_changes: int = 0
    successful: int = 0
    failed: int = 0
    quota_used: int = 0
    errors: list[str] = Field(default_factory=list)
    interrupted: bool = False

    @property
    def all_succeeded(self) -> bool:
        return self.failed == 0 and not self.interrupted


# ─────────────────────────────────────────────
# 3. 工具函式
# ─────────────────────────────────────────────


def parse_iso8601_duration(duration_str: str) -> int:
    """
    解析 ISO 8601 duration 字串為秒數。
    例如：PT4M30S → 270, PT1H2M3S → 3723, PT45S → 45
    """
    if not duration_str:
        return 0
    match = re.match(
        r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$",
        duration_str,
    )
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds
