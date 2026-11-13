"""
optimizer.py — Playlist Reorder Optimization Engine

Minimizes YouTube API quota consumption for large-scale playlist
reordering operations by computing the smallest set of position
updates required.

Core components:
1. Grouping on the artist the weighted formula in ``scripts/resolver.py`` picks
   (platform field, Agent label, title pattern, channel; see that module)
2. LIS (Longest Increasing Subsequence) anchor algorithm, constrained by
   pinned (unavailable) items that must keep their original position
3. Move planning against a simulated live list — the position sent to the
   API is computed at execution time, not taken from the target index
4. Spotify: grouping on the platform's structured artist / album ids, and a
   block-move planner (one call per contiguous run, ``S − LIS(runs)`` calls)

All computation is local (0 API units).
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import Counter, defaultdict
from typing import NamedTuple, Optional

from scripts import resolver
from scripts.executor import apply_sort
from scripts.resolver import PINNED, UNKNOWN, Resolution
from scripts.schemas import (
    EnrichedPlaylistItem,
    OptimizationReport,
    PositionChange,
    RangeMove,
    SortConfig,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

#: Share of unresolved items above which grouping is reported as low-benefit.
#: Calibrated on the 11 collected playlist samples (tests/dev/ytm_samples/):
#: exactly one list — a K-pop list whose videos sit on multi-artist label
#: channels (HYBE LABELS, SMTOWN, JYP) — is above it, at 39%.  The other ten
#: range from 0% to 4.7%, so 20% separates the one genuinely broken case with
#: 15.3pp of clearance below it and 19pp above, and no borderline call.
_UNKNOWN_RATIO_GATE: float = 0.20

#: Share of movable items that actually end up grouped with at least one other
#: item, below which grouping is reported as low-benefit.  Same 11 samples:
#: this catches the three lists where reordering is close to pointless — 12.9%
#: (one Chinese list, most of it singletons), 15.9% (a Japanese list where 40
#: groups hold 44 songs, so only 4 items move for 201 units — a wasted run) and
#: 23.2% (a film-score list).  The nearest list *not* caught sits at 45.5%, so
#: the threshold has 6.8pp of clearance below it and 15.5pp above.
#:
#: Re-run tests/dev/grouping_gate_bench.py --check after changing either gate:
#: the unit tests use synthetic fixtures and can only prove the arithmetic, not
#: that the threshold sits in the right place.
_EFFECTIVE_GROUPING_GATE: float = 0.30


# ─────────────────────────────────────────────
# Grouping
# ─────────────────────────────────────────────


def pinned_item_ids(items: list[EnrichedPlaylistItem]) -> frozenset[str]:
    """Return the ids of items that must never be moved.

    Private / deleted videos still occupy a slot in the playlist's position
    space, but the API cannot move them (and the user cannot see them).  They
    are pinned to their original index so that every other position stays exact.
    """
    return frozenset(
        item.playlist_item_id for item in items if not item.is_available
    )


def weave_pinned(
    movable_order: list[EnrichedPlaylistItem],
    original: list[EnrichedPlaylistItem],
) -> list[EnrichedPlaylistItem]:
    """Rebuild a full ordering, putting pinned items back at their original index.

    Args:
        movable_order: The desired order of the movable (available) items.
        original: The original full playlist, used to locate pinned items.

    Returns:
        A list of ``len(original)`` items where every pinned item sits at the
        index it had in *original*, and the movable items fill the remaining
        slots in the given order.
    """
    pinned = [(idx, item) for idx, item in enumerate(original) if not item.is_available]
    if not pinned:
        return list(movable_order)

    total = len(movable_order) + len(pinned)
    result: list[EnrichedPlaylistItem | None] = [None] * total
    for idx, item in pinned:
        if idx >= total:
            # Should not happen for a well-formed playlist; fail loudly rather
            # than silently shifting everything by one.
            raise ValueError(
                f"Pinned item {item.playlist_item_id} has index {idx} outside "
                f"the playlist length {total}."
            )
        result[idx] = item

    stream = iter(movable_order)
    for slot in range(total):
        if result[slot] is None:
            result[slot] = next(stream)

    return [item for item in result if item is not None]


def order_groups(
    items: list[EnrichedPlaylistItem],
    groups: dict[str, list[int]],
    group_order: str = "first_appearance",
    within_group_sort: SortConfig | None = None,
) -> list[EnrichedPlaylistItem]:
    """``stable_sort(i ↦ (rank_g(g(i)), rank_w(i)))``, with pinned items woven back in.

    ``rank_g`` defaults to each group's first appearance and ``rank_w`` to the
    original position — the order that moves the fewest items.  ``alphabetical``
    / ``count_desc`` and ``within_group_sort`` only apply when the user asked.
    """
    if group_order == "alphabetical":
        ordered_keys = sorted(groups)
    elif group_order == "count_desc":
        ordered_keys = sorted(groups, key=lambda k: len(groups[k]), reverse=True)
    else:
        ordered_keys = sorted(groups, key=lambda k: min(groups[k]))

    movable_order: list[EnrichedPlaylistItem] = []
    for key in ordered_keys:
        group_items = [items[idx] for idx in groups[key]]
        if within_group_sort is not None:
            group_items = apply_sort(group_items, within_group_sort)
        movable_order.extend(group_items)
    return weave_pinned(movable_order, items)


def group_by_artist(
    items: list[EnrichedPlaylistItem],
    labels: dict[str, dict] | None = None,
    aliases: dict[str, list[str]] | None = None,
    user: dict[str, str] | None = None,
    group_order: str = "first_appearance",
    within_group_sort: SortConfig | None = None,
) -> tuple[list[EnrichedPlaylistItem], dict[str, list[int]], list[Resolution]]:
    """Group by the artist :func:`scripts.resolver.resolve` picks for each item.

    ``labels`` are the Agent's answers, ``user`` the corrections made on the
    review page (both keyed by ``playlist_item_id``); ``aliases`` is
    ``{canonical: [variants]}``.  Groups are keyed by display name; items the
    formula cannot place share the ``unknown`` group.  Pinned (unavailable)
    items never take part and keep their original index.

    Returns ``(target, groups, resolutions)`` with ``resolutions`` in input order.
    """
    resolutions = resolver.resolve(items, labels=labels, aliases=aliases, user=user)
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, res in enumerate(resolutions):
        if res.key != PINNED:
            groups[UNKNOWN if res.key == UNKNOWN else res.name].append(idx)
    target = order_groups(items, groups, group_order, within_group_sort)
    logger.info(
        "Grouped %d items into %d artist groups (order=%s, pinned=%d).",
        len(items), len(groups), group_order, sum(1 for r in resolutions if r.key == PINNED),
    )
    return target, dict(groups), resolutions


# ─────────────────────────────────────────────
# LIS Anchor Algorithm
# ─────────────────────────────────────────────


def _lis_segment(
    seq: list[int],
    lo: int,
    hi: int,
    lower_bound: int,
    upper_bound: int | None,
) -> set[int]:
    """Longest increasing subsequence of ``seq[lo:hi]``, restricted to values
    strictly between *lower_bound* and *upper_bound*.

    Patience sorting with parent links, O(N log N).  The bounds keep the result
    compatible with the pinned items that delimit this segment: an anchor may
    only sit between two pinned items if its original position also lies
    between theirs, otherwise the anchor set would not be a valid increasing
    subsequence and the move plan would be unsatisfiable.

    Returns:
        The set of indices into *seq* that belong to the LIS.
    """
    candidates = [
        i
        for i in range(lo, hi)
        if seq[i] > lower_bound and (upper_bound is None or seq[i] < upper_bound)
    ]
    if not candidates:
        return set()

    tails: list[int] = []           # Smallest tail values
    tail_positions: list[int] = []  # Indices into seq for each tail
    parent: dict[int, int] = {}     # For backtracking

    for i in candidates:
        pos = bisect_left(tails, seq[i])
        if pos == len(tails):
            tails.append(seq[i])
            tail_positions.append(i)
        else:
            tails[pos] = seq[i]
            tail_positions[pos] = i

        parent[i] = tail_positions[pos - 1] if pos > 0 else -1

    lis_indices: set[int] = set()
    cur = tail_positions[-1]
    while cur != -1:
        lis_indices.add(cur)
        cur = parent[cur]

    return lis_indices


def compute_lis_anchors(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
    pinned_ids: frozenset[str] | None = None,
) -> frozenset[str]:
    """Find the items that do not need an API call to reach the target order.

    The anchor set must be an *increasing subsequence of original positions*
    when read in target order — that is the invariant the move planner relies
    on.  The longest such subsequence gives the provably minimal number of
    moves (``N - LIS``).

    Pinned items (private / deleted videos) are always anchors, and they split
    the sequence into segments: each segment's LIS is bounded by the pinned
    positions on either side so the combined set stays increasing.

    Args:
        current: Items in their current (original) order.
        target: Items in their desired (target) order.
        pinned_ids: Ids that must be anchors.  Defaults to the unavailable
            items found in *current*.

    Returns:
        A frozenset of playlist_item_ids that should NOT be moved (anchors).
    """
    if not current or not target:
        return frozenset()

    if pinned_ids is None:
        pinned_ids = pinned_item_ids(current)

    # Map playlist_item_id → original position
    original_pos: dict[str, int] = {
        item.playlist_item_id: idx for idx, item in enumerate(current)
    }

    # Build sequence: for each item in target order, its original position
    seq: list[int] = []
    target_ids: list[str] = []
    for item in target:
        pos = original_pos.get(item.playlist_item_id)
        if pos is not None:
            seq.append(pos)
            target_ids.append(item.playlist_item_id)

    if not seq:
        return frozenset()

    anchor_indices: set[int] = set()
    segment_start = 0
    lower_bound = -1

    for idx, item_id in enumerate(target_ids):
        if item_id not in pinned_ids:
            continue
        anchor_indices.add(idx)
        anchor_indices |= _lis_segment(
            seq, segment_start, idx, lower_bound, seq[idx]
        )
        segment_start = idx + 1
        lower_bound = seq[idx]

    anchor_indices |= _lis_segment(
        seq, segment_start, len(seq), lower_bound, None
    )

    anchor_ids = frozenset(target_ids[i] for i in anchor_indices)

    logger.info(
        "LIS computation: %d items total, %d anchors (%d pinned), %d need to move.",
        len(seq),
        len(anchor_ids),
        len(pinned_ids),
        len(seq) - len(anchor_ids),
    )
    return anchor_ids


# ─────────────────────────────────────────────
# Move Planning (drift-exact)
# ─────────────────────────────────────────────


def build_move_plan(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
    anchors: frozenset[str],
) -> list[PositionChange]:
    """Generate the minimal, ordered list of moves that turns *current* into
    *target*, with every position computed against a simulated live list.

    ``playlistItems.update`` removes the item and re-inserts it at ``position``,
    so every call re-indexes everything after it.  A position taken from the
    target array is therefore only valid for the very first call.  This planner
    replays the moves locally: it walks the target **head-first** (ascending),
    skips anchors, and sends each item to the slot immediately after its already
    settled predecessor.

    Because anchors form an increasing subsequence of original positions, the
    predecessor of every moved item is either an anchor or an item that has
    already been placed, which makes the plan exact.

    Args:
        current: Items in original order.
        target: Items in desired order (same id set as *current*).
        anchors: Set of playlist_item_ids that must NOT be moved.

    Returns:
        Ordered list of PositionChange records.  **Execute them in this exact
        order** — they are not independent, and must not be re-sorted.
    """
    original_pos: dict[str, int] = {
        item.playlist_item_id: idx for idx, item in enumerate(current)
    }

    # Sanity check: the anchors must be increasing in original position when
    # read in target order, otherwise no valid plan exists.
    last_anchor_pos = -1
    for item in target:
        item_id = item.playlist_item_id
        if item_id not in anchors:
            continue
        pos = original_pos.get(item_id)
        if pos is None:
            continue
        if pos <= last_anchor_pos:
            raise ValueError(
                "Anchor set is not an increasing subsequence "
                f"(item {item_id} at original position {pos} follows {last_anchor_pos})."
            )
        last_anchor_pos = pos

    live: list[str] = [item.playlist_item_id for item in current]
    target_ids: list[str] = [
        item.playlist_item_id
        for item in target
        if item.playlist_item_id in original_pos
    ]
    placeable = [item for item in target if item.playlist_item_id in original_pos]

    changes: list[PositionChange] = []
    for slot, item in enumerate(placeable):
        item_id = item.playlist_item_id
        if item_id in anchors:
            continue

        live.remove(item_id)
        api_position = 0 if slot == 0 else live.index(target_ids[slot - 1]) + 1
        live.insert(api_position, item_id)

        changes.append(
            PositionChange(
                playlist_item_id=item_id,
                video_id=item.video_id,
                title=item.title,
                old_position=original_pos[item_id],
                new_position=api_position,
                final_position=slot,
                execution_order=len(changes),
                playlist_id=item.playlist_id,
                resource_id=item.video_id,
            )
        )

    logger.info(
        "Generated %d ordered moves (head-first, positions simulated).",
        len(changes),
    )
    return changes


def normalize_target(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
) -> list[EnrichedPlaylistItem]:
    """Validate a caller-supplied target order and pin unavailable items back.

    ``diff`` accepts a hand-written ``new.json``; this is the guard that stops a
    malformed one from being written back at 50 units per wrong move.

    Args:
        current: The playlist as fetched.
        target: The desired order.

    Returns:
        A target of the same length as *current* with every pinned item at its
        original index.

    Raises:
        ValueError: If *target* is not a permutation of *current* (reordering
            cannot add or drop items — that is a separate operation).
    """
    current_ids = [item.playlist_item_id for item in current]
    target_ids = [item.playlist_item_id for item in target]

    if sorted(current_ids) != sorted(target_ids):
        missing = set(current_ids) - set(target_ids)
        extra = set(target_ids) - set(current_ids)
        raise ValueError(
            "Target ordering must be a permutation of the current playlist "
            f"({len(current_ids)} items). Missing: {len(missing)}, unknown: {len(extra)}. "
            "Reordering cannot add or remove items — re-run fetch if the "
            "playlist changed."
        )

    pinned = pinned_item_ids(current)
    if not pinned:
        return list(target)

    movable_order = [
        item for item in target if item.playlist_item_id not in pinned
    ]
    normalized = weave_pinned(movable_order, current)

    if [i.playlist_item_id for i in normalized] != target_ids:
        logger.warning(
            "Target moved %d unavailable item(s); they were pinned back to "
            "their original positions.",
            len(pinned),
        )
    return normalized


# ─────────────────────────────────────────────
# Public API: Full Optimization Pipeline
# ─────────────────────────────────────────────


def plan_reorder(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
) -> tuple[list[PositionChange], OptimizationReport]:
    """Single entry point for turning a desired order into API calls.

    Used by both the ``optimize`` path (artist grouping) and the ``diff`` path
    (custom ordering), so both get the same LIS savings and the same exactness
    guarantee.

    Args:
        current: Items in original order.
        target: Items in desired order.

    Returns:
        A tuple of (ordered_changes, report).
    """
    normalized = normalize_target(current, target)
    pinned = pinned_item_ids(current)
    anchors = compute_lis_anchors(current, normalized, pinned)
    changes = build_move_plan(current, normalized, anchors)

    # Naive baseline: one update call for every item that is not already
    # sitting at its final index.
    old_positions = {item.playlist_item_id: idx for idx, item in enumerate(current)}
    naive_count = sum(
        1
        for final_pos, item in enumerate(normalized)
        if old_positions.get(item.playlist_item_id, final_pos) != final_pos
    )

    report = OptimizationReport(
        total_items=len(current),
        anchors=len(anchors),
        need_to_move=len(changes),
        estimated_quota=len(changes) * 50,
        quota_saved_vs_naive=(naive_count - len(changes)) * 50,
        pinned_count=len(pinned),
    )

    return changes, report


def find_duplicate_videos(
    items: list[EnrichedPlaylistItem],
) -> list[dict]:
    """Group items that point at the exact same video (0 API units).

    One layer only: identical ``video_id``.  A second "probably the same song"
    tier keyed on title similarity was specified and dropped — the one time it
    was measured it flagged ``aLIEz`` against ``aLIEz (Remastered)`` and
    ``sh0ut`` against ``sh0ut (Remastered)``, because normalisation stripped the
    parenthesised part.  Those are different versions someone collected on
    purpose.  Identical ``video_id`` cannot produce that class of false
    positive, and a duplicate report the user has to second-guess is worth less
    than a short one they can trust.

    ``video_id`` legitimately repeats in a playlist, which is exactly what this
    finds; ``playlist_item_id`` stays the primary key throughout.

    Returns:
        One entry per repeated video, ordered by the position of the copy being
        kept.  ``keep`` is the earliest occurrence in playlist order and
        ``removable`` the rest.  Nothing is deleted — see cmd_duplicates.
    """
    by_video: dict[str, list[EnrichedPlaylistItem]] = defaultdict(list)
    for item in items:
        by_video[item.video_id].append(item)

    groups: list[dict] = []
    for video_id, occurrences in by_video.items():
        if len(occurrences) < 2:
            continue
        ordered = sorted(occurrences, key=lambda i: i.position)
        keep, extras = ordered[0], ordered[1:]
        groups.append(
            {
                "video_id": video_id,
                "title": keep.title,
                "occurrences": len(ordered),
                "keep": {
                    "playlist_item_id": keep.playlist_item_id,
                    "position": keep.position,
                },
                "removable": [
                    {
                        "playlist_item_id": extra.playlist_item_id,
                        "position": extra.position,
                    }
                    for extra in extras
                ],
            }
        )

    groups.sort(key=lambda g: g["keep"]["position"])
    return groups


class GroupingBenefit(NamedTuple):
    """How much a caller actually gains by grouping this particular playlist."""

    unknown_ratio: float
    effective_grouping_ratio: float
    orphan_group_count: int
    verdict: str
    warnings: list[str]


def evaluate_grouping_benefit(
    groups: dict[str, list[int]],
    resolutions: list[Resolution],
) -> GroupingBenefit:
    """Judge whether grouping this playlist is worth the quota.

    Many real playlists simply do not group: every song is by a different
    artist, or the videos sit on label channels that name no artist at all.
    Reordering them costs 50 units per move and the user sees no difference —
    one sampled list spent 201 units to move 4 of 44 items.  AGENT_SOP.md
    already carried the right knowledge ("group count close to item count means
    this list is not suitable for automatic grouping") but hung it on
    ``unresolved_count``, which is **0** on exactly that list.  So the gate
    could never open.  This computes the numbers instead of asking the agent to
    eyeball ``groups_found``.

    Two deliberate choices in the arithmetic:

    - Pinned (unavailable) items are outside ``groups`` already and stay out of
      the denominator.  They never take part in grouping, so counting them
      would penalise a playlist for videos nobody can reorder anyway.
    - The ``unknown`` group does **not** count towards
      ``effective_grouping_ratio``.  Dumping every unrecognised video into one
      bucket is not grouping, it is a bin — and counting it inverts the verdict
      on the worst case: the K-pop sample scores 77% with ``unknown`` included
      and 38% without.  This is the same bias that retired
      ``group_count_ratio`` as a metric.

    Returns:
        A :class:`GroupingBenefit`.  ``verdict`` is ``"low"`` when either gate
        trips, and ``warnings`` then carries one human-readable line per reason
        with the real numbers already substituted.  It is advisory: callers
        warn and confirm, they do not refuse to run.
    """
    movable = sum(len(indices) for indices in groups.values())
    if not movable:
        return GroupingBenefit(0.0, 0.0, 0, "ok", [])

    # Count unresolved items by resolution rather than by group name.
    unknown_indices = {idx for idx, res in enumerate(resolutions) if res.key == UNKNOWN}
    unknown_count = sum(
        1 for indices in groups.values() for idx in indices if idx in unknown_indices
    )

    grouped = 0
    orphan_group_count = 0
    for indices in groups.values():
        if len(indices) == 1:
            orphan_group_count += 1
            continue
        if any(idx in unknown_indices for idx in indices):
            continue
        grouped += len(indices)

    unknown_ratio = unknown_count / movable
    effective_grouping_ratio = grouped / movable

    warnings: list[str] = []
    if unknown_ratio >= _UNKNOWN_RATIO_GATE:
        warnings.append(
            f"{unknown_count}/{movable} 部影片（{unknown_ratio:.0%}）辨識不出藝人，"
            "會被歸進同一個 unknown 群。這通常代表影片掛在多藝人的廠牌頻道下，"
            "分群結果對使用者沒有意義。"
        )
    if effective_grouping_ratio < _EFFECTIVE_GROUPING_GATE:
        warnings.append(
            f"只有 {grouped}/{movable} 部影片（{effective_grouping_ratio:.0%}）真的和別的"
            f"影片聚在一起，其餘 {orphan_group_count} 群都只有一首。重排後畫面幾乎不會改變，"
            "但仍會照筆數消耗配額。"
        )

    return GroupingBenefit(
        unknown_ratio=round(unknown_ratio, 4),
        effective_grouping_ratio=round(effective_grouping_ratio, 4),
        orphan_group_count=orphan_group_count,
        verdict="low" if warnings else "ok",
        warnings=warnings,
    )


def run_full_optimization(
    items: list[EnrichedPlaylistItem],
    labels: dict[str, dict] | None = None,
    aliases: dict[str, list[str]] | None = None,
    user: dict[str, str] | None = None,
    group_order: str = "first_appearance",
    within_group_sort: SortConfig | None = None,
) -> tuple[list[EnrichedPlaylistItem], list[PositionChange], OptimizationReport, list[Resolution]]:
    """Complete pipeline: resolve → group → target → LIS → ordered moves.

    Returns ``(target_order, ordered_changes, report, resolutions)``.
    """
    target, groups, resolutions = group_by_artist(
        items, labels, aliases, user, group_order, within_group_sort
    )
    changes, report = plan_reorder(items, target)

    report.groups_found = list(groups.keys())
    report.group_details = {k: len(v) for k, v in groups.items()}
    report.unresolved_count = sum(1 for r in resolutions if r.key == UNKNOWN)
    report.uncertain_count = sum(1 for r in resolutions if r.uncertain)
    report.metadata_missing_count = sum(1 for item in items if not item.metadata_available)

    benefit = evaluate_grouping_benefit(groups, resolutions)
    report.unknown_ratio = benefit.unknown_ratio
    report.effective_grouping_ratio = benefit.effective_grouping_ratio
    report.orphan_group_count = benefit.orphan_group_count
    report.grouping_benefit = benefit.verdict
    report.grouping_warnings = benefit.warnings

    return target, changes, report, resolutions


# ─────────────────────────────────────────────
# Spotify: structured grouping + block moves
# ─────────────────────────────────────────────

#: ``--group-by`` values each platform supports.  YouTube has no structured
#: album (album clustering there was dropped in v3.10.0), so it is artist-only.
GROUP_BY_CHOICES: dict[str, tuple[str, ...]] = {
    "youtube": ("artist",),
    "spotify": ("artist", "album"),
}


def group_by_album(
    items: list[EnrichedPlaylistItem],
    group_order: str = "first_appearance",
    within_group_sort: SortConfig | None = None,
) -> tuple[list[EnrichedPlaylistItem], dict[str, list[int]], list[Resolution]]:
    """Spotify ``--group-by album``: group on the album id the platform gives us.

    Two albums that share a name stay apart (the label gets a short id suffix on
    a real collision).  Items without an album (local files, episodes) land in
    ``unknown``.  Same return shape as :func:`group_by_artist`.
    """
    def album_key(item: EnrichedPlaylistItem) -> tuple[str, str] | None:
        if item.is_available and item.album and (item.album.id or item.album.name):
            return item.album.id or f"name:{item.album.name}", item.album.name or item.album.id
        return None

    keyed = [album_key(item) for item in items]
    names_by_key = dict(entry for entry in reversed(keyed) if entry)
    name_counts = Counter(names_by_key.values())
    label_of = {
        key: name if name_counts[name] == 1 else f"{name} ({key.rsplit(':', 1)[-1][:6]})"
        for key, name in names_by_key.items()
    }

    groups: dict[str, list[int]] = defaultdict(list)
    resolutions: list[Resolution] = []
    for idx, (item, entry) in enumerate(zip(items, keyed)):
        if not item.is_available:
            resolutions.append(Resolution(PINNED, PINNED, 0.0, ""))
        elif entry is None:
            resolutions.append(Resolution(UNKNOWN, UNKNOWN, 0.0, ""))
            groups[UNKNOWN].append(idx)
        else:
            resolutions.append(Resolution(entry[0], label_of[entry[0]], 1.0, "P"))
            groups[label_of[entry[0]]].append(idx)
    return order_groups(items, groups, group_order, within_group_sort), dict(groups), resolutions


def apply_range_move(
    live: list[str], range_start: int, insert_before: int, range_length: int
) -> list[str]:
    """Replay one Spotify reorder call on an id list.

    Spotify counts ``insert_before`` in the list as it was **before** the move,
    so a target past the block shifts left by ``range_length`` once the block is
    lifted out.
    """
    block = live[range_start:range_start + range_length]
    rest = live[:range_start] + live[range_start + range_length:]
    slot = insert_before if insert_before <= range_start else insert_before - range_length
    return rest[:slot] + block + rest[slot:]


def build_range_plan(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
) -> list[RangeMove]:
    """Plan Spotify block moves: one call per contiguous run, ``S − LIS(runs)`` calls.

    A *run* is a maximal stretch of the current list whose items are also
    consecutive, in the same order, in the target.  Runs never need to be
    split, so each one moves as a block.  The runs that already appear in
    target order (an LIS over runs) stay put; every other run is moved, in
    target order, to sit right after its target predecessor.  Unavailable items
    form their own runs and are forced anchors, exactly like the YouTube planner.

    Since every item-level LIS extends to a run-level one, the call count is
    never above the item-level ``N − LIS`` that YouTube needs.

    Raises:
        ValueError: If *target* is not a permutation of *current*.
        RuntimeError: If replaying the plan does not reproduce *target* — this
            would be a planner bug and must never reach the API.
    """
    normalized = normalize_target(current, target)
    cur_keys = [item.playlist_item_id for item in current]
    tgt_keys = [item.playlist_item_id for item in normalized]
    pinned = pinned_item_ids(current)
    pos_t = {key: idx for idx, key in enumerate(tgt_keys)}
    cur_index = {key: idx for idx, key in enumerate(cur_keys)}
    title_of = {item.playlist_item_id: item.title for item in current}

    runs: list[list[str]] = []
    for key in cur_keys:
        if (
            runs
            and key not in pinned
            and runs[-1][-1] not in pinned
            and pos_t[key] == pos_t[runs[-1][-1]] + 1
        ):
            runs[-1].append(key)
        else:
            runs.append([key])

    runs_in_target = sorted(runs, key=lambda run: pos_t[run[0]])
    seq = [cur_index[run[0]] for run in runs_in_target]

    anchors: set[int] = set()
    segment_start, lower_bound = 0, -1
    for idx, run in enumerate(runs_in_target):
        if run[0] not in pinned:
            continue
        anchors.add(idx)
        anchors |= _lis_segment(seq, segment_start, idx, lower_bound, seq[idx])
        segment_start, lower_bound = idx + 1, seq[idx]
    anchors |= _lis_segment(seq, segment_start, len(seq), lower_bound, None)

    live = list(cur_keys)
    moves: list[RangeMove] = []
    for idx, run in enumerate(runs_in_target):
        if idx in anchors:
            continue
        first = run[0]
        start = live.index(first)
        target_slot = pos_t[first]
        insert_before = 0 if target_slot == 0 else live.index(tgt_keys[target_slot - 1]) + 1
        if insert_before == start:
            continue  # already sitting right after its predecessor
        moves.append(
            RangeMove(
                execution_order=len(moves),
                range_start=start,
                insert_before=insert_before,
                range_length=len(run),
                item_keys=list(run),
                first_title=title_of.get(first, ""),
                old_position=cur_index[first],
                final_position=target_slot,
            )
        )
        live = apply_range_move(live, start, insert_before, len(run))

    if live != tgt_keys:
        raise RuntimeError("Range plan does not reproduce the target order; refusing to emit it.")

    logger.info(
        "Range plan: %d items, %d runs, %d run anchors, %d calls.",
        len(cur_keys), len(runs), len(anchors), len(moves),
    )
    return moves


def plan_range_reorder(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
) -> tuple[list[RangeMove], dict[str, int]]:
    """Spotify counterpart of :func:`plan_reorder`: block moves plus cost figures."""
    normalized = normalize_target(current, target)
    moves = build_range_plan(current, normalized)
    pinned = pinned_item_ids(current)
    item_anchors = compute_lis_anchors(current, normalized, pinned)
    stats = {
        "total_items": len(current),
        "calls": len(moves),
        "items_moved": sum(move.range_length for move in moves),
        "item_level_moves": len(current) - len(item_anchors),
        "pinned_count": len(pinned),
    }
    return moves, stats
