"""
cache_manager.py — Local Playlist Cache Manager

Manages JSON file cache for YouTube playlist snapshots to reduce API
quota consumption.  Each playlist is stored as a separate JSON file
using Pydantic's serialization for round-trip fidelity.

Public API:
  PlaylistCache — main cache class with get / set / invalidate / is_valid
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from scripts.schemas import CachedPlaylist, EnrichedPlaylistItem

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR: Path = Path(__file__).resolve().parent / "cache"
_DEFAULT_TTL_MINUTES: int = 30


class PlaylistCache:
    """Filesystem-backed cache for playlist snapshots.

    Each playlist is serialized as a single JSON file under the cache
    directory.  Entries expire after ``ttl_minutes`` (default 30).

    Args:
        cache_dir: Directory for cache files.  Defaults to
            ``scripts/cache/`` next to this module.
        ttl_minutes: Time-to-live in minutes for each cache entry.

    Example::

        cache = PlaylistCache()
        cache.set("PLxxxxxx", enriched_items, etag="abc123")
        snapshot = cache.get("PLxxxxxx")
        if snapshot:
            print(f"Cached {snapshot.item_count} items")
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_minutes: int = _DEFAULT_TTL_MINUTES,
    ) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
        self._ttl_minutes = max(1, ttl_minutes)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "PlaylistCache initialized (dir=%s, ttl=%d min)",
            self._cache_dir,
            self._ttl_minutes,
        )

    # ─────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────

    def get(self, playlist_id: str) -> CachedPlaylist | None:
        """Retrieve a cached playlist snapshot.

        Returns ``None`` if the cache file does not exist, cannot be
        parsed, or has expired past the configured TTL.

        Args:
            playlist_id: YouTube playlist ID (e.g. ``"PLxxxxxxxx"``).

        Returns:
            A ``CachedPlaylist`` instance, or None.
        """
        path = self._cache_path(playlist_id)
        if not path.is_file():
            logger.debug("Cache miss (no file): %s", playlist_id)
            return None

        try:
            raw = path.read_text(encoding="utf-8")
            cached = CachedPlaylist.model_validate_json(raw)
        except Exception as exc:
            logger.warning(
                "Cache read/parse error for %s: %s — treating as miss",
                playlist_id,
                exc,
            )
            # Corrupt cache file; remove it to avoid repeated errors
            self._delete_file(path)
            return None

        # Override the stored TTL with the instance's configured TTL
        # so runtime changes to ttl_minutes take effect immediately
        cached.ttl_minutes = self._ttl_minutes

        if cached.is_expired:
            logger.debug(
                "Cache expired for %s (created_at=%s, ttl=%d min)",
                playlist_id,
                cached.created_at.isoformat(),
                self._ttl_minutes,
            )
            self._delete_file(path)
            return None

        logger.debug(
            "Cache hit: %s (%d items, etag=%s)",
            playlist_id,
            cached.item_count,
            cached.etag or "<none>",
        )
        return cached

    def set(
        self,
        playlist_id: str,
        items: list[EnrichedPlaylistItem],
        etag: str = "",
    ) -> None:
        """Store a playlist snapshot in the cache.

        Creates a ``CachedPlaylist`` model and writes it as JSON.
        Overwrites any existing cache for the same playlist.

        Args:
            playlist_id: YouTube playlist ID.
            items: List of enriched playlist items to cache.
            etag: Optional ETag from the YouTube API response for
                conditional requests.
        """
        snapshot = CachedPlaylist(
            playlist_id=playlist_id,
            items=items,
            created_at=datetime.now(timezone.utc),
            item_count=len(items),
            etag=etag,
            ttl_minutes=self._ttl_minutes,
        )

        path = self._cache_path(playlist_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            json_str = snapshot.model_dump_json(indent=2)
            path.write_text(json_str + "\n", encoding="utf-8")
            logger.info(
                "Cached playlist %s (%d items, etag=%s)",
                playlist_id,
                len(items),
                etag or "<none>",
            )
        except OSError as exc:
            logger.error("Failed to write cache for %s: %s", playlist_id, exc)

    def invalidate(self, playlist_id: str) -> None:
        """Delete the cache file for a playlist.

        No-op if the file does not exist.

        Args:
            playlist_id: YouTube playlist ID to invalidate.
        """
        path = self._cache_path(playlist_id)
        if self._delete_file(path):
            logger.info("Invalidated cache for %s", playlist_id)
        else:
            logger.debug("No cache to invalidate for %s", playlist_id)

    def is_valid(self, playlist_id: str) -> bool:
        """Check whether a valid (non-expired) cache exists for a playlist.

        This is a lightweight check that reads and validates the cache
        file without returning the full data.

        Args:
            playlist_id: YouTube playlist ID.

        Returns:
            True if a valid, non-expired cache entry exists.
        """
        return self.get(playlist_id) is not None

    # ─────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────

    def _cache_path(self, playlist_id: str) -> Path:
        """Compute the cache file path for a playlist.

        Args:
            playlist_id: YouTube playlist ID.

        Returns:
            Path like ``<cache_dir>/playlist_<playlist_id>.json``.
        """
        # Sanitize playlist_id to prevent path traversal
        safe_id = "".join(c for c in playlist_id if c.isalnum() or c in "-_")
        if not safe_id:
            safe_id = "unknown"
        return self._cache_dir / f"playlist_{safe_id}.json"

    @staticmethod
    def _delete_file(path: Path) -> bool:
        """Delete a file if it exists.

        Args:
            path: File to delete.

        Returns:
            True if the file was deleted, False otherwise.
        """
        try:
            if path.is_file():
                path.unlink()
                return True
        except OSError as exc:
            logger.warning("Failed to delete cache file %s: %s", path, exc)
        return False
