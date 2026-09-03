"""
schemas.py — Pydantic Schema 定義

所有資料模型的單一來源。分為兩類：
1. LLM 結構化輸出 Schema（PlaylistCommand 及其子模型）
2. 內部資料模型（PlaylistItem, VideoMetadata, PositionChange 等）
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

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
    is_available: bool = Field(
        default=True,
        description=(
            "影片是否可存取。私人／已刪除影片為 False；這類項目仍佔用播放清單的"
            "位置空間，必須保留才能算出正確的寫回位置（見 optimizer 的釘選機制）。"
        ),
    )


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
    is_available: bool = True
    metadata_available: bool = Field(
        default=True,
        description=(
            "videos.list 是否有回傳這支影片的 metadata。playlistItems 可見但 "
            "videos.list 未回傳的情況（例如地區限制）會是 False：此時仍可正常"
            "移動位置，但 title/channel_title 是空的，藝人辨識會落回 unknown，"
            "分群品質會受影響。與 is_available（私人／已刪除，釘選不移動）是"
            "兩件獨立的事。"
        ),
    )

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
            "is_available": item.is_available,
            "metadata_available": metadata is not None,
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
    """描述一個 item 的位置變更（單一次 playlistItems.update 呼叫）。

    重要：``new_position`` 是「送給 API 的位置」，不是該影片在最終清單中的索引。
    ``playlistItems.update`` 的語意是「先移除、再插入到 position」，每一次呼叫都會
    讓其餘項目重新編號，因此位置必須依執行順序在模擬盤面上即時算出。
    最終索引請改看 ``final_position``（僅供預覽表顯示）。

    這些變更**順序相依**：必須依 ``execution_order`` 由小到大逐筆執行，
    不可重新排序、不可跳過、不可平行執行。
    """
    playlist_item_id: str
    video_id: str
    title: str = ""
    old_position: int = Field(description="變更前在原清單中的索引（供預覽表顯示）")
    new_position: int = Field(description="送給 playlistItems.update 的 position 參數")
    final_position: int = Field(
        default=-1,
        description="套用完整變更集後，該影片在最終清單中的索引（供預覽表顯示）",
    )
    execution_order: int = Field(
        default=-1,
        description="執行序（0-based）。必須依此順序逐筆送出，否則位置會漂移。",
    )
    playlist_id: str = ""
    resource_id: str = ""  # videoId，update API 需要

    @property
    def api_position(self) -> int:
        """送給 API 的位置（``new_position`` 的語意別名）。"""
        return self.new_position


class CachedPlaylist(BaseModel):
    """快取的 playlist 快照。"""
    playlist_id: str
    items: list[EnrichedPlaylistItem]
    created_at: datetime
    item_count: int = 0
    etag: str = ""
    ttl_minutes: int = 30

    @property
    def head_fingerprint(self) -> str:
        """快照頭部指紋，用於寫回前偵測清單是否已在遠端被改動。"""
        return compute_head_fingerprint(self.items)

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


class ArtistResolution(BaseModel):
    """單一影片的藝人辨識結果。"""
    artist_key: str = Field(description="標準化後的藝人名稱（作為分組 key）")
    confidence: float = Field(ge=0.0, le=1.0, description="辨識信心分數")
    method: str = Field(description="辨識方法：bracket_prefix | dash_separator | channel | fuzzy | unknown")
    raw_candidate: str = Field(default="", description="原始候選字串（debug 用）")
    counterpart: str = Field(
        default="",
        description=(
            "標題分隔符另一側的字串（例如「曲名 - 藝人」的藝人側）。"
            "用來偵測反向標題與版本標記，避免把曲名當成藝人。"
        ),
    )
    channel_override_candidate: str = Field(
        default="",
        description=(
            "灰色地帶信心度（見 optimizer._GRAY_ZONE_CONFIDENCE）的 title 層猜測與 "
            "channel 層衝突、且既有逃生閥都沒接住時，這裡暫存 channel 層原本會給出的 "
            "藝人 key。resolve_artist() 逐支影片判斷時看不到其他影片，真正是否要"
            "覆蓋回這個 key，交給有整份清單視野的 group_by_artist() 依頻道多數決決定"
            "（見 _apply_channel_majority_override）。非灰色地帶（例如 bracket_prefix "
            "0.95）永遠是空字串，不會被二次審查。"
        ),
    )


class ChannelMajorityOverride(BaseModel):
    """一筆被『頻道多數決』機制修正過的藝人辨識結果。

    只有 title 層灰色地帶信心度（0.80–0.90，目前即 dash_separator）的猜測，
    才可能出現在這裡；bracket_prefix（0.95）等高信心度來源永遠不會被覆蓋。
    刻意攤開成獨立列表而不是靜默套用，讓 Phase 3 的人工預覽表能看到、覆核。
    """
    video_id: str
    title: str
    channel_title: str
    from_artist_key: str = Field(description="覆蓋前：title 層灰色地帶的猜測")
    to_artist_key: str = Field(description="覆蓋後：channel 層多數決採用的藝人 key")
    corroborating_count: int = Field(
        description="同頻道內，透過 channel 層獨立辨識出同一個藝人的其他影片數量"
    )


class OptimizationReport(BaseModel):
    """optimize 指令的最佳化結果報告。"""
    total_items: int = Field(description="播放清單總影片數")
    anchors: int = Field(description="錨點數量（不需移動的影片）")
    need_to_move: int = Field(description="需要移動的影片數量")
    estimated_quota: int = Field(description="預估配額消耗（units）")
    quota_saved_vs_naive: int = Field(description="相較 Naive 方法節省的配額")
    groups_found: list[str] = Field(default_factory=list, description="識別出的藝人群組列表")
    unresolved_count: int = Field(default=0, description="無法自動辨識的影片數量")
    group_details: dict[str, int] = Field(default_factory=dict, description="每個群組的影片數量")
    pinned_count: int = Field(
        default=0,
        description="被釘在原位置、不參與重排的影片數量（私人／已刪除影片）",
    )
    metadata_missing_count: int = Field(
        default=0,
        description=(
            "videos.list 未回傳 metadata 的影片數量（例如地區限制）。這些影片"
            "仍會正常參與重排，但沒有標題／頻道名可用於辨識，通常會落入 "
            "unknown 群組——`unresolved_count` 不會告訴你原因是「辨識失敗」"
            "還是「根本沒有資料可辨識」，這個欄位補上這個區別。"
        ),
    )
    unknown_ratio: float = Field(
        default=0.0,
        description=(
            "辨識不出藝人的影片佔可移動影片的比例（0.0–1.0）。分母不含被釘住的"
            "不可用影片——它們從來不參與分群。"
        ),
    )
    effective_grouping_ratio: float = Field(
        default=0.0,
        description=(
            "真正被聚集起來的影片佔可移動影片的比例（0.0–1.0）：屬於「兩首以上"
            "同群」的影片數 ÷ 可移動影片數。**`unknown` 群不計入分子**——把辨識"
            "失敗的影片全丟進同一桶不是聚集，是垃圾桶；把它算進來會讓最該被攔下"
            "的清單看起來聚集良好（K-pop 那份含 unknown 是 77%，排除後是 38%）。"
        ),
    )
    orphan_group_count: int = Field(
        default=0,
        description="只有一首歌的群組數量（孤兒群）。接近群組總數時代表這份清單不適合自動分群。",
    )
    grouping_benefit: Literal["ok", "low"] = Field(
        default="ok",
        description=(
            "分群效益判定。`low` 代表這份清單重排後使用者大概感受不到差別，"
            "**但這是警告不是拒絕執行**：Phase 3 要先把 `grouping_warnings` "
            "告知使用者並取得確認，不要靜默照跑。判定在 `optimize` 階段完成，"
            "本來就是 0 API units，所以警告一定發生在使用者付出配額之前。"
        ),
    )
    grouping_warnings: list[str] = Field(
        default_factory=list,
        description="`grouping_benefit` 為 `low` 時的具體原因，每一條都已帶入實際數字，可直接轉述給使用者。",
    )
    channel_majority_overrides: list[ChannelMajorityOverride] = Field(
        default_factory=list,
        description=(
            "被『頻道多數決』機制修正過的藝人辨識結果列表。詳見 "
            "ChannelMajorityOverride；Phase 3 預覽時應告知使用者這幾支影片的"
            "分群結果是被頻道多數決覆蓋過的，而不是靜默套用。"
        ),
    )


# ─────────────────────────────────────────────
# 3. 工具函式
# ─────────────────────────────────────────────


#: 寫回前用來比對遠端清單是否被改動的取樣長度（一次 playlistItems.list = 1 unit）。
HEAD_FINGERPRINT_SIZE: int = 50


def compute_head_fingerprint(items: list) -> str:
    """計算清單頭部指紋（前 ``HEAD_FINGERPRINT_SIZE`` 筆的 playlist_item_id 順序）。

    用途：``update`` 在寫回前只需 1 unit 讀取第一頁，即可判斷這份變更集所依據的
    快照是否仍然成立。順序不同 → 遠端已被改動 → 中止並要求重新 fetch。

    Args:
        items: 具有 ``playlist_item_id`` 屬性的項目列表（依目前順序）。

    Returns:
        16 進位字串（sha256 前 16 碼）。空清單回傳空字串。
    """
    if not items:
        return ""
    head = [getattr(it, "playlist_item_id", "") for it in items[:HEAD_FINGERPRINT_SIZE]]
    digest = hashlib.sha256("\n".join(head).encode("utf-8")).hexdigest()
    return digest[:16]


def compute_change_set_fingerprint(playlist_id: str, changes: list) -> str:
    """計算變更集指紋，用於斷點續傳時確認「續的是同一份任務」。

    變更集是順序相依的，因此指紋必須涵蓋順序與每一筆的目標位置；只要任一項不同，
    就代表這是一份新的任務，不可沿用舊進度。

    Args:
        playlist_id: 播放清單 ID。
        changes: ``PositionChange`` 列表（依執行順序）。

    Returns:
        16 進位字串（sha256 前 16 碼）。
    """
    parts = [playlist_id]
    for c in changes:
        parts.append(f"{c.playlist_item_id}:{c.new_position}")
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


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
