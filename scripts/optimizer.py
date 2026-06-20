"""
optimizer.py — Playlist Reorder Optimization Engine

Minimizes YouTube API quota consumption for large-scale playlist
reordering operations by computing the smallest set of position
updates required.

Core components:
1. Three-layer artist identification engine (regex → channel → fuzzy)
2. LIS (Longest Increasing Subsequence) anchor algorithm
3. Drift-safe change ordering (tail-first)

All computation is local (0 API units).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import Optional

from scripts.schemas import (
    ArtistResolution,
    EnrichedPlaylistItem,
    OptimizationReport,
    PositionChange,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Layer 1: Title regex patterns (priority order, highest confidence first)
_TITLE_PATTERNS: list[tuple[str, str, float]] = [
    # Pattern 1: Full-width bracket prefix  【Artist】Song
    (r"^[【\[]([^\]】]+)[】\]]\s*(.+)", "bracket_prefix", 0.95),
    # Pattern 2: Standard ARTIST - SONG (strip trailing metadata)
    (r"^(.+?)\s*[-–—]\s*(.+?)(?:\s*[\(\[].+?[\)\]])*$", "dash_separator", 0.80),
    # Pattern 3: Bracket suffix with metadata  Song (Artist Ver./Official)
    (
        r"^(.+?)\s*\(([^)]+?)\s*(?:ver|version|official|mv|video)\.?\)",
        "bracket_suffix_meta",
        0.70,
    ),
]

# Noise patterns to strip from titles before regex parsing
_NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:official\s+)?(?:music\s+video|m/?v|lyric(?:s)?\s+video|"
        r"performance\s+ver(?:sion)?\.?|live|remix|edit|cover|instrumental|"
        r"karaoke|teaser|trailer|short(?:s)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:hd|4k|1080p|720p|remastered?)\b", re.IGNORECASE),
    re.compile(r"\(feat\.?\s+[^)]+\)", re.IGNORECASE),
    re.compile(r"\[feat\.?\s+[^\]]+\]", re.IGNORECASE),
]

# Layer 2: Distributor/label channel blacklist (these cannot be used as artist keys)
_DISTRIBUTOR_CHANNELS: set[str] = {
    "1thek", "big hit labels", "smtown", "jyp entertainment",
    "yg entertainment", "hybe labels", "stone music entertainment",
    "ultra music", "warner music taiwan", "warner music japan",
    "sony music", "universal music", "avex", "various artists",
    "vevo", "topic",
}

# Channel name suffix noise to strip during normalization
_CHANNEL_SUFFIX_PATTERN = re.compile(
    r"\s*(?:official|channel|music|records?|entertainment|ent\.?|"
    r"label|vevo|topic)\s*$",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────
# Layer 1: Title Regex Parsing
# ─────────────────────────────────────────────


def _clean_title(title: str) -> str:
    """Remove noise suffixes/tags from a video title."""
    cleaned = title
    for pattern in _NOISE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    # Collapse whitespace and strip
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # Remove trailing punctuation noise
    cleaned = re.sub(r"[\s\-–—:,;]+$", "", cleaned).strip()
    return cleaned


def _parse_title_for_artist(title: str) -> ArtistResolution | None:
    """Attempt to extract artist name from video title using regex patterns.

    Returns an ArtistResolution if a pattern matches, otherwise None.
    """
    cleaned = _clean_title(title)

    for pattern_str, method, confidence in _TITLE_PATTERNS:
        match = re.match(pattern_str, cleaned, re.IGNORECASE)
        if match:
            if method == "bracket_prefix":
                # Group 1 is artist, Group 2 is song
                artist_raw = match.group(1).strip()
            elif method == "dash_separator":
                # Group 1 is artist, Group 2 is song
                artist_raw = match.group(1).strip()
            elif method == "bracket_suffix_meta":
                # Group 1 is song, Group 2 is artist (reversed)
                artist_raw = match.group(2).strip()
            else:
                continue

            if not artist_raw or len(artist_raw) > 80:
                continue

            # Remove parenthetical sub-annotations from artist
            # e.g. "BTS (방탄소년단)" -> "BTS"
            artist_clean = re.sub(r"\s*[\(\[][^)\]]*[\)\]]$", "", artist_raw).strip()
            if not artist_clean:
                artist_clean = artist_raw

            return ArtistResolution(
                artist_key=_normalize_name(artist_clean),
                confidence=confidence,
                method=method,
                raw_candidate=artist_raw,
            )

    return None


# ─────────────────────────────────────────────
# Layer 2: Channel Title Normalization
# ─────────────────────────────────────────────


def _normalize_name(name: str) -> str:
    """Normalize an artist/channel name for comparison.

    Steps:
    1. Unicode NFKC normalization (full/half-width, compatibility chars)
    2. Remove official/label suffixes
    3. Lowercase + strip
    """
    name = unicodedata.normalize("NFKC", name)
    name = _CHANNEL_SUFFIX_PATTERN.sub("", name)
    return name.strip().lower()


def _is_distributor_channel(channel_title: str) -> bool:
    """Check if a channel is a known distributor/label (not a direct artist)."""
    normalized = _normalize_name(channel_title)
    # Exact match or ends with a blacklisted term
    if normalized in _DISTRIBUTOR_CHANNELS:
        return True
    # Also check if the channel name ends with " - topic" (auto-generated)
    if normalized.endswith(" - topic"):
        return True
    return False


def _resolve_channel(channel_title: str) -> ArtistResolution | None:
    """Attempt to use channel_title as artist identifier.

    Returns None if the channel is a known distributor/label.
    """
    if not channel_title or _is_distributor_channel(channel_title):
        return None

    return ArtistResolution(
        artist_key=_normalize_name(channel_title),
        confidence=0.75,
        method="channel",
        raw_candidate=channel_title,
    )


# ─────────────────────────────────────────────
# Layer 3: Fuzzy Matching
# ─────────────────────────────────────────────


def _load_aliases(aliases_path: Path | None) -> dict[str, list[str]]:
    """Load artist alias mapping from JSON file.

    Expected format:
    {
        "BTS": ["Bangtan Boys", "방탄소년단"],
        "BLACKPINK": ["블랙핑크", "BP"]
    }
    """
    if aliases_path is None or not aliases_path.is_file():
        return {}

    try:
        raw = json.loads(aliases_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            logger.warning("Aliases file is not a dict: %s", aliases_path)
            return {}
        return raw
    except Exception as exc:
        logger.warning("Failed to load aliases from %s: %s", aliases_path, exc)
        return {}


def _build_alias_lookup(aliases: dict[str, list[str]]) -> dict[str, str]:
    """Build a normalized alias → canonical name lookup table.

    Returns a dict where each normalized alias maps to the canonical artist name.
    """
    lookup: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        canonical_norm = _normalize_name(canonical)
        lookup[canonical_norm] = canonical
        for alias in alias_list:
            lookup[_normalize_name(alias)] = canonical
    return lookup


def _fuzzy_match_artist(
    candidate: str,
    alias_lookup: dict[str, str],
    threshold: float = 85.0,
) -> ArtistResolution | None:
    """Use rapidfuzz WRatio to match a candidate against known artist aliases.

    Returns None if no match exceeds the threshold.
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        logger.warning("rapidfuzz not installed; skipping fuzzy matching.")
        return None

    if not alias_lookup:
        return None

    normalized = _normalize_name(candidate)
    known_names = list(alias_lookup.keys())

    result = process.extractOne(
        normalized,
        known_names,
        scorer=fuzz.WRatio,
        score_cutoff=threshold,
    )

    if result:
        matched_name, score, _idx = result
        canonical = alias_lookup[matched_name]
        return ArtistResolution(
            artist_key=_normalize_name(canonical),
            confidence=score / 100.0,
            method="fuzzy",
            raw_candidate=candidate,
        )

    return None


# ─────────────────────────────────────────────
# Three-Layer Resolution Engine
# ─────────────────────────────────────────────


def resolve_artist(
    item: EnrichedPlaylistItem,
    alias_lookup: dict[str, str],
) -> ArtistResolution:
    """Resolve the artist for a single playlist item using the three-layer pipeline.

    Priority:
    1. Title regex parsing (highest confidence)
    2. Channel title normalization (cross-validation)
    3. Fuzzy matching against known aliases (fallback)

    If all layers fail, returns an 'unknown' resolution.
    """
    title_result = _parse_title_for_artist(item.title)
    channel_result = _resolve_channel(item.channel_title)

    # Layer 1 + Layer 2 cross-validation
    if title_result and channel_result:
        if title_result.artist_key == channel_result.artist_key:
            # Cross-confirmed: boost confidence
            return ArtistResolution(
                artist_key=title_result.artist_key,
                confidence=min(1.0, title_result.confidence + 0.15),
                method=f"{title_result.method}+channel",
                raw_candidate=title_result.raw_candidate,
            )
        # Prefer higher confidence
        if title_result.confidence >= 0.80:
            return title_result
        return channel_result

    # Layer 1 only (sufficient if high confidence)
    if title_result and title_result.confidence >= 0.80:
        return title_result

    # Layer 2 only
    if channel_result:
        return channel_result

    # Layer 1 with lower confidence (still try it)
    if title_result:
        # Attempt fuzzy validation of the title-extracted artist
        fuzzy_result = _fuzzy_match_artist(
            title_result.raw_candidate, alias_lookup
        )
        if fuzzy_result:
            return fuzzy_result
        return title_result

    # Layer 3: Fuzzy matching on channel_title as last resort
    if item.channel_title and not _is_distributor_channel(item.channel_title):
        fuzzy_result = _fuzzy_match_artist(item.channel_title, alias_lookup)
        if fuzzy_result:
            return fuzzy_result

    # All layers failed
    return ArtistResolution(
        artist_key="unknown",
        confidence=0.0,
        method="unknown",
        raw_candidate=item.channel_title or item.title,
    )


# ─────────────────────────────────────────────
# Grouping
# ─────────────────────────────────────────────


def group_by_artist(
    items: list[EnrichedPlaylistItem],
    aliases_path: Path | None = None,
    group_order: str = "first_appearance",
) -> tuple[list[EnrichedPlaylistItem], dict[str, list[int]], list[ArtistResolution]]:
    """Group playlist items by resolved artist and build target ordering.

    Args:
        items: Current playlist items in their original order.
        aliases_path: Optional path to artist_aliases.json.
        group_order: How to order groups. One of:
            - "first_appearance": Groups ordered by their earliest item position.
            - "alphabetical": Groups ordered alphabetically by artist key.
            - "count_desc": Groups ordered by item count (largest first).

    Returns:
        A tuple of:
        - target: The reordered list (grouped by artist).
        - groups: Dict mapping artist_key → list of original indices.
        - resolutions: ArtistResolution for each item (same order as input).
    """
    aliases = _load_aliases(aliases_path)
    alias_lookup = _build_alias_lookup(aliases)

    resolutions: list[ArtistResolution] = []
    groups: dict[str, list[int]] = defaultdict(list)

    for idx, item in enumerate(items):
        resolution = resolve_artist(item, alias_lookup)
        resolutions.append(resolution)
        groups[resolution.artist_key].append(idx)

    # Determine group ordering
    if group_order == "alphabetical":
        ordered_keys = sorted(groups.keys())
    elif group_order == "count_desc":
        ordered_keys = sorted(groups.keys(), key=lambda k: len(groups[k]), reverse=True)
    else:  # first_appearance (default)
        ordered_keys = sorted(groups.keys(), key=lambda k: min(groups[k]))

    # Build target ordering
    target: list[EnrichedPlaylistItem] = []
    for key in ordered_keys:
        for idx in groups[key]:
            target.append(items[idx])

    logger.info(
        "Grouped %d items into %d artist groups (order=%s).",
        len(items),
        len(groups),
        group_order,
    )
    return target, dict(groups), resolutions


# ─────────────────────────────────────────────
# LIS Anchor Algorithm
# ─────────────────────────────────────────────


def compute_lis_anchors(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
) -> frozenset[str]:
    """Find the longest increasing subsequence of original positions in the
    target ordering. Items in the LIS do not need to be moved.

    Args:
        current: Items in their current (original) order.
        target: Items in their desired (target) order.

    Returns:
        A frozenset of playlist_item_ids that should NOT be moved (anchors).
    """
    if not current or not target:
        return frozenset()

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

    # Compute LIS using patience sorting (O(N log N))
    # We need to recover the actual LIS elements, not just the length
    n = len(seq)
    tails: list[int] = []           # Smallest tail values
    tail_positions: list[int] = []  # Indices into seq for each tail
    parent: list[int] = [-1] * n    # For backtracking

    for i in range(n):
        pos = bisect_left(tails, seq[i])
        if pos == len(tails):
            tails.append(seq[i])
            tail_positions.append(i)
        else:
            tails[pos] = seq[i]
            tail_positions[pos] = i

        if pos > 0:
            parent[i] = tail_positions[pos - 1]

    # Backtrack to find actual LIS indices
    lis_indices: set[int] = set()
    if tails:
        cur = tail_positions[-1]
        while cur != -1:
            lis_indices.add(cur)
            cur = parent[cur]

    anchor_ids = frozenset(target_ids[i] for i in lis_indices)

    logger.info(
        "LIS computation: %d items total, %d anchors (LIS length), %d need to move.",
        len(seq),
        len(anchor_ids),
        len(seq) - len(anchor_ids),
    )
    return anchor_ids


# ─────────────────────────────────────────────
# Optimized Change Generation
# ─────────────────────────────────────────────


def build_optimized_changes(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
    anchors: frozenset[str],
) -> list[PositionChange]:
    """Generate the minimal set of PositionChange records, excluding anchors,
    sorted by target position descending (tail-first) for drift-safe execution.

    Args:
        current: Items in original order.
        target: Items in desired order.
        anchors: Set of playlist_item_ids that should NOT be moved.

    Returns:
        List of PositionChange records, sorted by new_position descending.
    """
    old_positions: dict[str, int] = {
        item.playlist_item_id: idx for idx, item in enumerate(current)
    }

    changes: list[PositionChange] = []
    for new_pos, item in enumerate(target):
        if item.playlist_item_id in anchors:
            continue

        old_pos = old_positions.get(item.playlist_item_id, new_pos)
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

    # Sort by new_position DESCENDING for drift-safe execution (tail-first)
    changes.sort(key=lambda c: c.new_position, reverse=True)

    logger.info(
        "Generated %d optimized changes (sorted tail-first for drift-safe execution).",
        len(changes),
    )
    return changes


# ─────────────────────────────────────────────
# Public API: Full Optimization Pipeline
# ─────────────────────────────────────────────


def optimize_reorder(
    current: list[EnrichedPlaylistItem],
    target: list[EnrichedPlaylistItem],
) -> tuple[list[PositionChange], OptimizationReport]:
    """Run the full optimization pipeline: LIS anchors + minimal changes.

    Args:
        current: Items in original order.
        target: Items in desired order (e.g. output of group_by_artist).

    Returns:
        A tuple of (optimized_changes, report).
    """
    anchors = compute_lis_anchors(current, target)
    changes = build_optimized_changes(current, target, anchors)

    # Naive baseline: count all items whose position changed
    old_positions = {item.playlist_item_id: idx for idx, item in enumerate(current)}
    naive_count = sum(
        1 for new_pos, item in enumerate(target)
        if old_positions.get(item.playlist_item_id, new_pos) != new_pos
    )

    report = OptimizationReport(
        total_items=len(current),
        anchors=len(anchors),
        need_to_move=len(changes),
        estimated_quota=len(changes) * 50,
        quota_saved_vs_naive=(naive_count - len(changes)) * 50,
    )

    return changes, report


def run_full_optimization(
    items: list[EnrichedPlaylistItem],
    aliases_path: Path | None = None,
    group_order: str = "first_appearance",
) -> tuple[list[EnrichedPlaylistItem], list[PositionChange], OptimizationReport, list[ArtistResolution]]:
    """Complete pipeline: group → target → LIS → changes.

    Args:
        items: Current playlist items.
        aliases_path: Optional path to artist_aliases.json.
        group_order: Group ordering strategy.

    Returns:
        A tuple of (target_order, optimized_changes, report, resolutions).
    """
    target, groups, resolutions = group_by_artist(items, aliases_path, group_order)
    changes, report = optimize_reorder(items, target)

    # Enrich report with group info
    report.groups_found = list(groups.keys())
    report.group_details = {k: len(v) for k, v in groups.items()}
    report.unresolved_count = sum(
        1 for r in resolutions if r.method == "unknown"
    )

    return target, changes, report, resolutions
