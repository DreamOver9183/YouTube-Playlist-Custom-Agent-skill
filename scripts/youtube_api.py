"""
youtube_api.py — YouTube Data API v3 Client

Wraps the YouTube Data API v3 for playlist management operations:
- OAuth 2.0 authentication with token caching
- Playlist item retrieval with pagination
- Batch video metadata fetching
- Playlist item position updates and deletion
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from scripts.schemas import PlaylistItemData, VideoMetadata, parse_iso8601_duration

logger = logging.getLogger(__name__)

# OAuth 2.0 scopes — force-ssl gives full read/write access over HTTPS.
_SCOPES: list[str] = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Defaults relative to the project root.
_DEFAULT_CREDENTIALS_PATH = Path("scripts/credentials/client_secret.json")
_DEFAULT_TOKEN_PATH = Path("scripts/credentials/token.json")


def extract_playlist_id(url_or_id: str) -> str:
    """Extract a YouTube playlist ID from a URL or return the raw ID.

    Handles these URL patterns:
        - https://www.youtube.com/playlist?list=PLxxxxxx
        - https://youtube.com/watch?v=xxx&list=PLxxxxxx
        - https://www.youtube.com/embed/videoseries?list=PLxxxxxx

    Args:
        url_or_id: A YouTube playlist URL or a bare playlist ID.

    Returns:
        The extracted playlist ID string.

    Raises:
        ValueError: If a URL is detected but contains no ``list`` parameter.
    """
    url_or_id = url_or_id.strip()

    # Quick heuristic: bare IDs typically start with PL, UU, LL, FL, OL, etc.
    if not url_or_id.startswith(("http://", "https://", "www.")):
        return url_or_id

    parsed = urlparse(url_or_id)
    query_params = parse_qs(parsed.query)
    playlist_ids = query_params.get("list")
    if playlist_ids:
        return playlist_ids[0]

    raise ValueError(
        f"Could not extract playlist ID from URL: {url_or_id}"
    )


class YouTubeClient:
    """Thin wrapper around the YouTube Data API v3 for playlist ops.

    Attributes:
        credentials_path: Path to the OAuth client-secret JSON file.
        token_path: Path to the cached OAuth token JSON file.
    """

    def __init__(
        self,
        credentials_path: Path | None = None,
        token_path: Path | None = None,
    ) -> None:
        self.credentials_path: Path = credentials_path or _DEFAULT_CREDENTIALS_PATH
        self.token_path: Path = token_path or _DEFAULT_TOKEN_PATH
        self._youtube = None  # Will be set by authenticate()
        self._creds: Credentials | None = None

    # ── Authentication ───────────────────────────────────────────────

    def authenticate(self) -> None:
        """Perform OAuth 2.0 authentication and build the YouTube service.

        Token lifecycle:
        1. If *token_path* exists and the token is still valid → reuse it.
        2. If the token is expired but refreshable → refresh automatically.
        3. Otherwise, launch the OAuth consent flow via a local server.

        Raises:
            FileNotFoundError: If *credentials_path* does not exist and no
                cached token is available.
        """
        creds: Credentials | None = None

        # 1. Try to load a cached token.
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_path), _SCOPES
                )
                logger.info("Loaded cached token from %s", self.token_path)
            except Exception:
                logger.warning(
                    "Failed to load cached token; will re-authenticate.",
                    exc_info=True,
                )
                creds = None

        # 2. Refresh if expired.
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Refreshed expired token successfully.")
            except RefreshError:
                logger.warning(
                    "Token refresh failed; will re-authenticate.",
                    exc_info=True,
                )
                creds = None

        # 3. Full consent flow if we still don't have valid creds.
        if not creds or not creds.valid:
            if not self.credentials_path.exists():
                raise FileNotFoundError(
                    f"Client secret file not found: {self.credentials_path}. "
                    "Download it from the Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), _SCOPES
            )
            import json
            import sys
            print(json.dumps({
                "status": "waiting_for_user",
                "message": "正在開啟瀏覽器進行 Google 帳號授權，請在瀏覽器中完成登入..."
            }))
            sys.stdout.flush()
            creds = flow.run_local_server(port=0)
            logger.info("Completed OAuth consent flow.")

        # Persist the token for next run.
        # SEC: Enforce secure permissions for sensitive OAuth token
        self.token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_path.touch(mode=0o600, exist_ok=True)
        self.token_path.chmod(0o600)

        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Saved token to %s", self.token_path)

        self._creds = creds
        self._youtube = build("youtube", "v3", credentials=creds)
        logger.info("YouTube API service built successfully.")

    # ── Playlist Items ───────────────────────────────────────────────

    def get_playlist_items(self, playlist_id: str) -> list[PlaylistItemData]:
        """Retrieve every item in a playlist, handling pagination.

        Args:
            playlist_id: The YouTube playlist ID (e.g. ``PLxxxxxxxx``).

        Returns:
            A list of ``PlaylistItemData`` models, filtered to exclude
            unavailable / deleted videos.

        Raises:
            RuntimeError: If the API client has not been authenticated.
            HttpError: On unrecoverable API errors.
        """
        self._ensure_authenticated()

        items: list[PlaylistItemData] = []
        page_token: str | None = None

        while True:
            request = self._youtube.playlistItems().list(
                part="snippet,contentDetails,status",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            )
            try:
                response: dict = request.execute()
            except HttpError as exc:
                logger.error(
                    "API error fetching playlist items (playlist=%s): %s",
                    playlist_id,
                    exc,
                )
                raise

            for raw_item in response.get("items", []):
                item = self._parse_playlist_item(raw_item)
                if item is not None:
                    items.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        logger.info(
            "Fetched %d items from playlist %s", len(items), playlist_id
        )
        return items

    # ── Video Metadata ───────────────────────────────────────────────

    def get_videos_metadata(
        self, video_ids: list[str]
    ) -> list[VideoMetadata]:
        """Fetch detailed metadata for a list of video IDs.

        The YouTube API accepts up to 50 IDs per request, so this method
        batches automatically.

        Args:
            video_ids: One or more YouTube video IDs.

        Returns:
            A list of ``VideoMetadata`` models (order may differ from input).

        Raises:
            RuntimeError: If the API client has not been authenticated.
        """
        self._ensure_authenticated()

        if not video_ids:
            return []

        results: list[VideoMetadata] = []

        for start in range(0, len(video_ids), 50):
            batch = video_ids[start : start + 50]
            request = self._youtube.videos().list(
                part="snippet,contentDetails,statistics,status",
                id=",".join(batch),
            )
            try:
                response: dict = request.execute()
            except HttpError as exc:
                logger.error(
                    "API error fetching video metadata for batch starting "
                    "at index %d: %s",
                    start,
                    exc,
                )
                raise

            for raw_video in response.get("items", []):
                meta = self._parse_video_metadata(raw_video)
                results.append(meta)

        logger.info(
            "Fetched metadata for %d / %d videos",
            len(results),
            len(video_ids),
        )
        return results

    # ── Mutations ────────────────────────────────────────────────────

    def update_item_position(
        self,
        playlist_item_id: str,
        playlist_id: str,
        video_id: str,
        new_position: int,
    ) -> bool:
        """Move a playlist item to a new position.

        Args:
            playlist_item_id: The playlist-item resource ID.
            playlist_id: The parent playlist ID.
            video_id: The video's ID (required by the API body).
            new_position: Zero-based target position.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        self._ensure_authenticated()

        body: dict = {
            "id": playlist_item_id,
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
                "position": new_position,
            },
        }

        try:
            self._youtube.playlistItems().update(
                part="snippet", body=body
            ).execute()
            logger.debug(
                "Moved item %s to position %d in playlist %s",
                playlist_item_id,
                new_position,
                playlist_id,
            )
            return True
        except HttpError as exc:
            logger.error(
                "Failed to move item %s to position %d: %s",
                playlist_item_id,
                new_position,
                exc,
            )
            return False

    def delete_item(self, playlist_item_id: str) -> bool:
        """Delete a single item from a playlist.

        Args:
            playlist_item_id: The playlist-item resource ID to delete.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        self._ensure_authenticated()

        try:
            self._youtube.playlistItems().delete(
                id=playlist_item_id
            ).execute()
            logger.debug("Deleted playlist item %s", playlist_item_id)
            return True
        except HttpError as exc:
            logger.error(
                "Failed to delete playlist item %s: %s",
                playlist_item_id,
                exc,
            )
            return False

    # ── Internal Helpers ─────────────────────────────────────────────

    def _ensure_authenticated(self) -> None:
        """Raise if ``authenticate()`` hasn't been called yet."""
        if self._youtube is None:
            raise RuntimeError(
                "YouTube client is not authenticated. "
                "Call authenticate() before making API requests."
            )

    @staticmethod
    def _parse_playlist_item(raw: dict) -> PlaylistItemData | None:
        """Map a raw API playlist-item resource to ``PlaylistItemData``.

        Returns ``None`` for unavailable videos (deleted, private, etc.).
        """
        snippet: dict = raw.get("snippet", {})
        status: dict = raw.get("status", {})
        resource_id: dict = snippet.get("resourceId", {})

        video_id: str = resource_id.get("videoId", "")
        if not video_id:
            logger.debug(
                "Skipping playlist item %s — empty videoId", raw.get("id")
            )
            return None

        # Skip unavailable / private / deleted videos.
        privacy_status: str = status.get("privacyStatus", "public")
        if privacy_status in ("private", "privacyStatusUnspecified"):
            logger.debug(
                "Skipping unavailable video %s (status=%s)",
                video_id,
                privacy_status,
            )
            return None

        published_at_str: str = snippet.get("publishedAt", "")
        try:
            added_at = _parse_datetime(published_at_str)
        except (ValueError, TypeError):
            from datetime import datetime, timezone
            added_at = datetime.now(timezone.utc)
            logger.warning(
                "Unparseable publishedAt '%s' for item %s; defaulting to now.",
                published_at_str,
                raw.get("id"),
            )

        return PlaylistItemData(
            playlist_item_id=raw["id"],
            video_id=video_id,
            position=snippet.get("position", 0),
            added_at=added_at,
            channel_title=snippet.get("videoOwnerChannelTitle", ""),
            playlist_id=snippet.get("playlistId", ""),
        )

    @staticmethod
    def _parse_video_metadata(raw: dict) -> VideoMetadata:
        """Map a raw API video resource to ``VideoMetadata``."""
        snippet: dict = raw.get("snippet", {})
        content: dict = raw.get("contentDetails", {})
        stats: dict = raw.get("statistics", {})
        status: dict = raw.get("status", {})

        duration_raw: str = content.get("duration", "")
        duration_seconds: int = parse_iso8601_duration(duration_raw)

        published_at_str: str = snippet.get("publishedAt", "")
        try:
            published_at = _parse_datetime(published_at_str)
        except (ValueError, TypeError):
            published_at = None

        return VideoMetadata(
            video_id=raw.get("id", ""),
            title=snippet.get("title", ""),
            description=snippet.get("description", ""),
            channel_title=snippet.get("channelTitle", ""),
            published_at=published_at,
            duration_seconds=duration_seconds,
            duration_raw=duration_raw,
            view_count=_safe_int(stats.get("viewCount")),
            like_count=_safe_int(stats.get("likeCount")),
            comment_count=_safe_int(stats.get("commentCount")),
            tags=snippet.get("tags", []),
            privacy_status=status.get("privacyStatus", "public"),
        )


# ── Module-level Utilities ───────────────────────────────────────────


def _parse_datetime(value: str) -> "datetime":
    """Parse an ISO-8601 datetime string from the YouTube API.

    YouTube returns timestamps like ``2024-01-15T10:30:00Z``.

    Args:
        value: An ISO-8601 datetime string.

    Returns:
        A timezone-aware ``datetime`` object.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    from datetime import datetime, timezone

    if not value:
        raise ValueError("Empty datetime string")

    # Python 3.11+ handles the trailing 'Z' via fromisoformat.
    # For robustness, replace 'Z' with '+00:00' as well.
    normalised = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalised)


def _safe_int(value: str | int | None) -> int:
    """Convert a value to int, defaulting to 0 on failure."""
    if value is None:
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0
