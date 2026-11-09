"""
executor.py — within-group sort keys and quota estimate

Pure computation module (no API calls, no side effects).  The reorder
planner itself lives in ``optimizer.plan_reorder``.
"""

from __future__ import annotations

import locale
import logging
from datetime import datetime, timezone
from typing import Callable

from scripts.schemas import (
    EnrichedPlaylistItem,
    PositionChange,
    SortConfig,
    SortField,
    SortOrder,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────


def apply_sort(
    items: list[EnrichedPlaylistItem],
    sort_config: SortConfig,
) -> list[EnrichedPlaylistItem]:
    """Return a new list sorted according to *sort_config*.

    Args:
        items: The list of enriched playlist items to sort.
        sort_config: Specifies the field and direction.

    Returns:
        A new list sorted in the requested order.

    Raises:
        ValueError: If an unsupported sort field is provided.
    """
    reverse = sort_config.order == SortOrder.DESC
    key_func = _sort_key_factory(sort_config.field)

    sorted_items = sorted(items, key=key_func, reverse=reverse)
    logger.info(
        "Sorted %d items by %s (%s).",
        len(sorted_items),
        sort_config.field.value,
        sort_config.order.value,
    )
    return sorted_items


def estimate_quota(changes: list[PositionChange]) -> int:
    """Estimate the YouTube API quota cost for a set of position changes.

    Each ``playlistItems.update`` call costs **50 quota units**.

    Args:
        changes: The list of position changes to apply.

    Returns:
        Total estimated quota units.
    """
    cost = len(changes) * 50
    logger.debug("Estimated quota cost: %d units for %d changes.", cost, len(changes))
    return cost


# ─────────────────────────────────────────────
# Internal Helpers
# ─────────────────────────────────────────────


def _sort_key_factory(
    field: SortField,
) -> Callable[[EnrichedPlaylistItem], object]:
    """Return a sort-key function for the given ``SortField``.

    Args:
        field: The ``SortField`` enum member to sort by.

    Returns:
        A callable suitable for ``sorted(..., key=...)``.

    Raises:
        ValueError: If the field is not recognised.
    """
    match field:
        case SortField.VIEW_COUNT:
            return lambda item: item.view_count
        case SortField.PUBLISHED_AT:
            return lambda item: (
                _ensure_aware(item.published_at)
                if item.published_at is not None
                else datetime.min.replace(tzinfo=timezone.utc)
            )
        case SortField.DURATION:
            return lambda item: item.duration_seconds
        case SortField.TITLE:
            return _locale_title_key
        case SortField.CHANNEL_TITLE:
            return lambda item: item.channel_title.lower()
        case SortField.ADDED_AT:
            return lambda item: _ensure_aware(item.added_at)
        case SortField.TRACK_NUMBER:
            return lambda item: (item.disc_number, item.track_number)
        case _:
            raise ValueError(f"Unsupported sort field: {field!r}")


def _locale_title_key(item: EnrichedPlaylistItem) -> str:
    """Generate a locale-aware sort key for a title string.

    Falls back to simple case-folding if the locale collation module
    is unavailable or misconfigured.

    Args:
        item: The playlist item whose title to convert.

    Returns:
        A transformed string usable as a sort key.
    """
    try:
        return locale.strxfrm(item.title)
    except Exception:
        return item.title.casefold()


def _ensure_aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware, assuming UTC if naïve.

    Args:
        dt: A datetime that may or may not have tzinfo.

    Returns:
        A timezone-aware datetime (original if already aware).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
