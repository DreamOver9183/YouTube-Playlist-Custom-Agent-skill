"""
executor.py — Sort / Filter Engine + Diff Calculator

Pure computation module (no API calls, no side effects).  Operates on
``EnrichedPlaylistItem`` lists and produces ``PositionChange`` diffs
that the API layer can apply.
"""

from __future__ import annotations

import locale
import logging
from datetime import datetime, timezone
from typing import Callable

from scripts.schemas import (
    EnrichedPlaylistItem,
    FilterConfig,
    PositionChange,
    SortConfig,
    SortField,
    SortOrder,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────


def apply_filter(
    items: list[EnrichedPlaylistItem],
    filter_config: FilterConfig,
) -> list[EnrichedPlaylistItem]:
    """Return a new list containing only items that match *every* active filter.

    Each non-``None`` field in *filter_config* is treated as an AND condition.
    Items must satisfy **all** active conditions to be included.

    Args:
        items: The full list of enriched playlist items.
        filter_config: Filter criteria; only non-None fields are applied.

    Returns:
        A (possibly empty) list of items that pass all active filters.
    """
    predicates: list[Callable[[EnrichedPlaylistItem], bool]] = []

    if filter_config.channel is not None:
        _ch = filter_config.channel.lower()
        predicates.append(lambda item, ch=_ch: ch in item.channel_title.lower())

    if filter_config.duration_min_seconds is not None:
        _dmin = filter_config.duration_min_seconds
        predicates.append(lambda item, dmin=_dmin: item.duration_seconds >= dmin)

    if filter_config.duration_max_seconds is not None:
        _dmax = filter_config.duration_max_seconds
        predicates.append(lambda item, dmax=_dmax: item.duration_seconds <= dmax)

    if filter_config.published_after is not None:
        _after = _parse_date_boundary(filter_config.published_after)
        predicates.append(
            lambda item, after=_after: (
                item.published_at is not None
                and _ensure_aware(item.published_at) >= after
            )
        )

    if filter_config.published_before is not None:
        _before = _parse_date_boundary(filter_config.published_before)
        predicates.append(
            lambda item, before=_before: (
                item.published_at is not None
                and _ensure_aware(item.published_at) <= before
            )
        )

    if filter_config.title_contains is not None:
        _tc = filter_config.title_contains.lower()
        predicates.append(lambda item, tc=_tc: tc in item.title.lower())

    if filter_config.title_excludes is not None:
        _te = filter_config.title_excludes.lower()
        predicates.append(lambda item, te=_te: te not in item.title.lower())

    if not predicates:
        logger.debug("No active filter predicates; returning all items.")
        return list(items)

    filtered = [item for item in items if all(p(item) for p in predicates)]
    logger.info(
        "Filter reduced %d items to %d items.", len(items), len(filtered)
    )
    return filtered


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


def compute_diff(
    old_items: list[EnrichedPlaylistItem],
    new_items: list[EnrichedPlaylistItem],
) -> list[PositionChange]:
    """Compute position changes between two orderings of the same items.

    Only items whose position **actually changed** are included in the
    returned list.

    Args:
        old_items: The current ordering (positions taken from list index).
        new_items: The desired ordering (positions taken from list index).

    Returns:
        A list of ``PositionChange`` records for items that moved.
    """
    # Build a lookup from playlist_item_id → old position.
    old_positions: dict[str, int] = {
        item.playlist_item_id: idx for idx, item in enumerate(old_items)
    }

    changes: list[PositionChange] = []
    for new_pos, item in enumerate(new_items):
        old_pos = old_positions.get(item.playlist_item_id)
        if old_pos is None:
            # Item exists in new list but not in old — treat as an add at
            # the end.  This shouldn't normally happen in a pure
            # re-order scenario, but handle gracefully.
            logger.warning(
                "Item %s (%s) found in new list but not in old; "
                "treating old_position as %d.",
                item.playlist_item_id,
                item.title,
                new_pos,
            )
            old_pos = new_pos

        if old_pos != new_pos:
            changes.append(
                PositionChange(
                    playlist_item_id=item.playlist_item_id,
                    video_id=item.video_id,
                    title=item.title,
                    old_position=old_pos,
                    new_position=new_pos,
                    playlist_id=item.playlist_id,
                    resource_id=item.video_id,
                )
            )

    logger.info(
        "Computed diff: %d of %d items changed position.",
        len(changes),
        len(new_items),
    )
    return changes


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


def _parse_date_boundary(date_str: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` date string into a timezone-aware datetime.

    The returned datetime is set to midnight UTC on the given date.

    Args:
        date_str: Date in ``YYYY-MM-DD`` format.

    Returns:
        A timezone-aware ``datetime`` at midnight UTC.

    Raises:
        ValueError: If the string is not a valid date.
    """
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)


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
