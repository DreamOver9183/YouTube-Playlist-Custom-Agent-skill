"""
optimizer.py — Playlist Reorder Optimization Engine

Minimizes YouTube API quota consumption for large-scale playlist
reordering operations by computing the smallest set of position
updates required.

Core components:
1. Three-layer artist identification engine (regex → channel → fuzzy)
2. LIS (Longest Increasing Subsequence) anchor algorithm, constrained by
   pinned (unavailable) items that must keep their original position
3. Move planning against a simulated live list — the position sent to the
   API is computed at execution time, not taken from the target index

All computation is local (0 API units).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from scripts.executor import apply_sort
from scripts.schemas import (
    ArtistResolution,
    ChannelMajorityOverride,
    EnrichedPlaylistItem,
    OptimizationReport,
    PositionChange,
    SortConfig,
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

#: Separator debris left behind after a suffix is removed.  Without this,
#: "Alan Walker - Topic" normalises to "alan walker -", which never matches the
#: artist's own channel "alan walker" and silently splits the group in two.
_TRAILING_SEPARATORS = re.compile(r"[\s\-–—:_·/|,]+$")

#: Empty brackets left behind by noise removal, e.g. "Numb (Official Video)"
#: → "Numb ()".
_EMPTY_BRACKETS = re.compile(r"[\(\[【]\s*[\)\]】]")

#: Words that make up a version/edition annotation rather than a song title.
#: If everything on the other side of the dash is one of these, the dash is not
#: an "artist - song" separator ("Valhalla Calling - Duet Version").
_VERSION_MARKER_WORDS: frozenset[str] = frozenset({
    "official", "ver", "version", "remix", "edit", "live", "acoustic",
    "instrumental", "karaoke", "cover", "remaster", "remastered", "extended",
    "radio", "duet", "mix", "audio", "video", "mv", "lyric", "lyrics",
    "performance", "visualizer", "hd", "4k", "8k", "1080p", "720p", "full",
})

#: Confidence band where a title-layer win over a disagreeing channel gets a
#: second look from the channel's own majority (see
#: _apply_channel_majority_override). Bounded below by 0.80 — the minimum
#: confidence that ever beats a channel result at all, currently produced only
#: by dash_separator — and above by bracket_prefix's 0.95 explicit 【Artist】
#: marker, which is never reconsidered regardless of channel evidence: it is a
#: deliberate, human-authored signal, not a syntactic guess that happened to
#: clear a threshold.
_GRAY_ZONE_CONFIDENCE: tuple[float, float] = (0.80, 0.90)

#: Minimum number of *other* same-channel items independently resolved to one
#: artist (via the channel layer) before a gray-zone title guess is overridden.
#: Calibrated against two real playlists (248 items total): false-positive-prone
#: label/compilation channels (RHINO, VEVO handles, Atlantic Records, Kontor.TV,
#: ...) never accumulate >=2 items under one artist_key there — each of their
#: videos is a different real artist — while a genuine single-artist channel
#: with a title-hijacked outlier (e.g. Hiroyuki SAWANO's "Mio Mare (2V-Alk
#: Version)") does. No case in that data needed a threshold of 1 to be rescued,
#: so 2 is the smallest threshold the evidence supports.
_MAJORITY_MIN_CORROBORATION: int = 2


# ─────────────────────────────────────────────
# Layer 1: Title Regex Parsing
# ─────────────────────────────────────────────


def _clean_title(title: str) -> str:
    """Remove noise suffixes/tags from a video title."""
    cleaned = title
    for pattern in _NOISE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    # Stripping the noise words out of "(Official Music Video)" leaves "()",
    # which used to survive all the way into the group name ("numb ()").
    cleaned = _EMPTY_BRACKETS.sub(" ", cleaned)
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
                counterpart = match.group(2).strip()
            elif method == "dash_separator":
                # Group 1 is artist, Group 2 is song
                artist_raw = match.group(1).strip()
                counterpart = match.group(2).strip()
            elif method == "bracket_suffix_meta":
                # Group 1 is song, Group 2 is artist (reversed)
                artist_raw = match.group(2).strip()
                counterpart = match.group(1).strip()
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
                counterpart=counterpart,
            )

    return None


#: Latin/digits plus kana, CJK ideographs and Hangul — artist names in this
#: domain are routinely Korean or Japanese, and dropping those characters would
#: silently disable every token comparison below for them.
_TOKEN_PATTERN = re.compile(r"[0-9a-z぀-ヿ㐀-鿿가-힣]+")


def _tokens(name: str) -> list[str]:
    """Split a name into comparable word tokens (CJK / Hangul kept whole)."""
    return _TOKEN_PATTERN.findall(name.lower())


def _loose(name: str) -> str:
    """Collapse a name to letters/digits only, so 'ImagineDragons' == 'Imagine Dragons'."""
    return "".join(_tokens(name))


def _matched_token_run(haystack: str, needle: str) -> str:
    """Return *haystack*'s word run that spells *needle*, or "" if there is none.

    Token-level (not substring) on purpose: "Sia" must not match "Siamese Dream".
    A run of *haystack* words may also be glued together to match, because VEVO
    channels drop the spaces ("LadyGagaVEVO" vs "Lady Gaga, Bruno Mars") — and
    the run is returned in the readable, spaced spelling.
    """
    outer, inner = _tokens(haystack), _tokens(needle)
    if not inner or not outer:
        return ""
    if len(inner) <= len(outer):
        for start in range(len(outer) - len(inner) + 1):
            if outer[start : start + len(inner)] == inner:
                return " ".join(inner)

    glued = "".join(inner)
    if len(glued) < 5:  # too short to be distinctive; avoids "sia" ⊂ "siamese"
        return ""
    for start in range(len(outer)):
        run: list[str] = []
        for word in outer[start:]:
            run.append(word)
            joined = "".join(run)
            if joined == glued:
                return " ".join(run)
            if len(joined) >= len(glued):
                break
    return ""


def _contains_token_run(haystack: str, needle: str) -> bool:
    """True if *needle*'s words appear consecutively inside *haystack*'s words."""
    return bool(_matched_token_run(haystack, needle))


def _is_version_annotation(text: str) -> bool:
    """True if *text* is purely a version/edition marker, not a song name."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return bool(words) and all(word in _VERSION_MARKER_WORDS for word in words)


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
    # Loop so "Artist Official Channel" collapses fully, and drop the separator
    # each suffix leaves behind ("Alan Walker - Topic" → "alan walker", not
    # "alan walker -", which would never match the artist's own channel).
    for _ in range(3):
        shortened = _TRAILING_SEPARATORS.sub("", _CHANNEL_SUFFIX_PATTERN.sub("", name))
        if shortened == name:
            break
        name = shortened
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
        title_key = title_result.artist_key
        channel_key = channel_result.artist_key

        if _loose(title_key) == _loose(channel_key):
            # Cross-confirmed.  Keep whichever spelling is more readable so
            # "ImagineDragonsVEVO" and "Imagine Dragons - Topic" land on one key.
            best = max((title_key, channel_key), key=lambda k: (k.count(" "), len(k)))
            return ArtistResolution(
                artist_key=best,
                confidence=min(1.0, title_result.confidence + 0.15),
                method=f"{title_result.method}+channel",
                raw_candidate=title_result.raw_candidate,
                counterpart=title_result.counterpart,
            )

        # The channel is the artist's own upload identity, so it outranks a
        # title guess whenever the title tells us the guess is not an artist:
        #
        #   "One More Light [...] - Linkin Park"  → reversed "song - artist"
        #   "Valhalla Calling - Duet Version"     → the other side is a version
        #   "Alan Walker & Torine - Hello World"  → collaboration, main act owns it
        reversed_title = _loose(title_result.counterpart) == _loose(channel_key)
        version_tail = _is_version_annotation(title_result.counterpart)
        collaboration = _matched_token_run(title_key, channel_key)
        if reversed_title or version_tail or collaboration:
            reason = (
                "reversed_title" if reversed_title
                else "version_tail" if version_tail
                else "collaboration"
            )
            # The title spells the artist with spaces even when the channel does
            # not ("LadyGagaVEVO" → "lady gaga"), so prefer that spelling.
            return ArtistResolution(
                artist_key=collaboration if collaboration and not reversed_title else channel_key,
                confidence=min(1.0, channel_result.confidence + 0.15),
                method=f"channel+{reason}",
                raw_candidate=channel_result.raw_candidate,
                counterpart=title_result.raw_candidate,
            )

        # Otherwise the title still wins when it is confident — a label channel
        # ("RHINO") must not swallow the real artist named in the title.
        if title_result.confidence >= 0.80:
            gray_lo, gray_hi = _GRAY_ZONE_CONFIDENCE
            if gray_lo <= title_result.confidence < gray_hi:
                # Borderline confidence (today: dash_separator only). This
                # function only sees one item at a time, so it cannot tell a
                # spurious hyphen-in-a-version-code guess ("VV-Alk" on
                # Hiroyuki SAWANO's channel) apart from a genuine "Artist -
                # Song" title. Flag the channel's reading as a fallback
                # candidate; group_by_artist() has visibility across the
                # whole playlist and decides via channel majority (see
                # _apply_channel_majority_override). A high-confidence
                # explicit marker like bracket_prefix (0.95) never reaches
                # this branch's gray zone and is never second-guessed.
                return title_result.model_copy(
                    update={"channel_override_candidate": channel_result.artist_key}
                )
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


def _apply_channel_majority_override(
    items: list[EnrichedPlaylistItem],
    resolutions: list[ArtistResolution],
) -> list[ChannelMajorityOverride]:
    """Second-guess gray-zone title wins against the channel's own majority.

    ``resolve_artist`` flags a gray-zone title win (see _GRAY_ZONE_CONFIDENCE)
    with ``channel_override_candidate`` but cannot decide alone — it only sees
    one item at a time. Here, with every item's resolution in hand, that
    flagged guess is overridden back to the channel's reading only if
    ``_MAJORITY_MIN_CORROBORATION`` or more *other* items from the exact same
    channel independently resolved to that same artist via the channel layer.
    Label/compilation channels never accumulate that majority under one key
    (each of their videos is a different real artist), so they are left
    untouched; a single artist's own channel does, which is exactly the gap
    this closes.

    Mutates ``resolutions`` in place (by index) and returns the overrides
    applied, so callers can surface them in the Phase 3 preview instead of
    letting the correction happen silently.
    """
    channel_majority: dict[str, Counter[str]] = defaultdict(Counter)
    for item, resolution in zip(items, resolutions):
        if resolution.method == "channel" or resolution.method.endswith("+channel"):
            channel_majority[item.channel_title][resolution.artist_key] += 1

    overrides: list[ChannelMajorityOverride] = []
    for idx, (item, resolution) in enumerate(zip(items, resolutions)):
        candidate = resolution.channel_override_candidate
        if not candidate:
            continue
        corroborating = channel_majority.get(item.channel_title, {}).get(candidate, 0)
        if corroborating < _MAJORITY_MIN_CORROBORATION:
            continue
        overrides.append(
            ChannelMajorityOverride(
                video_id=item.video_id,
                title=item.title,
                channel_title=item.channel_title,
                from_artist_key=resolution.artist_key,
                to_artist_key=candidate,
                corroborating_count=corroborating,
            )
        )
        resolutions[idx] = resolution.model_copy(
            update={
                "artist_key": candidate,
                "method": f"{resolution.method}+channel_majority_override",
            }
        )

    if overrides:
        logger.info(
            "Channel-majority override reclassified %d item(s): %s",
            len(overrides),
            ", ".join(f"{o.title!r} -> {o.to_artist_key!r}" for o in overrides),
        )

    return overrides


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


def group_by_artist(
    items: list[EnrichedPlaylistItem],
    aliases_path: Path | None = None,
    group_order: str = "first_appearance",
    within_group_sort: SortConfig | None = None,
) -> tuple[list[EnrichedPlaylistItem], dict[str, list[int]], list[ArtistResolution], list[ChannelMajorityOverride]]:
    """Group playlist items by resolved artist and build target ordering.

    Unavailable (private / deleted) items never take part in the grouping —
    they are pinned back to their original index by :func:`weave_pinned`.

    Args:
        items: Current playlist items in their original order.
        aliases_path: Optional path to artist_aliases.json.
        group_order: How to order groups. One of:
            - "first_appearance": Groups ordered by their earliest item position.
            - "alphabetical": Groups ordered alphabetically by artist key.
            - "count_desc": Groups ordered by item count (largest first).
        within_group_sort: Optional sort applied *inside* each group (e.g.
            view count descending).  Reuses ``executor.apply_sort`` so the
            sort semantics are identical to the ``diff`` path.

    Returns:
        A tuple of:
        - target: The reordered list (grouped by artist).
        - groups: Dict mapping artist_key → list of original indices.
        - resolutions: ArtistResolution for each item (same order as input).
        - channel_majority_overrides: Gray-zone title guesses that were
          overridden back to the channel's majority reading (see
          _apply_channel_majority_override); empty when none applied.
    """
    aliases = _load_aliases(aliases_path)
    alias_lookup = _build_alias_lookup(aliases)

    resolutions: list[ArtistResolution] = []

    for idx, item in enumerate(items):
        if not item.is_available:
            resolutions.append(
                ArtistResolution(
                    artist_key="__pinned__",
                    confidence=0.0,
                    method="unavailable",
                    raw_candidate=item.video_id,
                )
            )
            continue

        resolutions.append(resolve_artist(item, alias_lookup))

    channel_majority_overrides = _apply_channel_majority_override(items, resolutions)

    # Keys that differ only in spacing or punctuation are the same artist
    # ("imaginedragons" from a VEVO channel vs "imagine dragons" from a title).
    # Pick the most readable spelling as the display key for the whole bucket.
    display_of: dict[str, str] = {}
    for resolution in resolutions:
        if resolution.method == "unavailable":
            continue
        key = resolution.artist_key
        bucket = _loose(key) or key
        current = display_of.get(bucket)
        if current is None or (key.count(" "), len(key)) > (current.count(" "), len(current)):
            display_of[bucket] = key

    # A localised name often prefixes the original one ("紅髮艾德 Ed Sheeran",
    # "酷玩樂團 Coldplay").  Fold it into the plain-name bucket when that bucket
    # already exists.  Deliberately limited to a non-ASCII prefix: applying it to
    # any suffix match would merge "Steve Aoki & Alan Walker" into whichever
    # collaborator happened to have a bucket, which is not a decision to make on
    # a coin flip.
    alias_of: dict[str, str] = {}
    buckets = sorted(display_of, key=len)
    for longer in reversed(buckets):
        if longer.isascii():
            continue
        for shorter in buckets:
            if len(shorter) < 6 or not shorter.isascii() or len(shorter) >= len(longer):
                continue
            if longer.endswith(shorter) and _contains_token_run(
                display_of[longer], display_of[shorter]
            ):
                alias_of[longer] = shorter
                break

    groups: dict[str, list[int]] = defaultdict(list)
    for idx, resolution in enumerate(resolutions):
        if resolution.method == "unavailable":
            continue
        bucket = _loose(resolution.artist_key) or resolution.artist_key
        display = display_of[alias_of.get(bucket, bucket)]
        if display != resolution.artist_key:
            resolutions[idx] = resolution.model_copy(update={"artist_key": display})
        groups[display].append(idx)

    # Determine group ordering
    if group_order == "alphabetical":
        ordered_keys = sorted(groups.keys())
    elif group_order == "count_desc":
        ordered_keys = sorted(groups.keys(), key=lambda k: len(groups[k]), reverse=True)
    else:  # first_appearance (default)
        ordered_keys = sorted(groups.keys(), key=lambda k: min(groups[k]))

    # Build target ordering for the movable items
    movable_order: list[EnrichedPlaylistItem] = []
    for key in ordered_keys:
        group_items = [items[idx] for idx in groups[key]]
        if within_group_sort is not None:
            group_items = apply_sort(group_items, within_group_sort)
        movable_order.extend(group_items)

    target = weave_pinned(movable_order, items)

    logger.info(
        "Grouped %d items into %d artist groups (order=%s, within_group_sort=%s, pinned=%d).",
        len(items),
        len(groups),
        group_order,
        within_group_sort.field.value if within_group_sort else "none",
        len(items) - len(movable_order),
    )
    return target, dict(groups), resolutions, channel_majority_overrides


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


#: Backwards-compatible alias (the pipeline is no longer just "optimization").
optimize_reorder = plan_reorder


def run_full_optimization(
    items: list[EnrichedPlaylistItem],
    aliases_path: Path | None = None,
    group_order: str = "first_appearance",
    within_group_sort: SortConfig | None = None,
) -> tuple[list[EnrichedPlaylistItem], list[PositionChange], OptimizationReport, list[ArtistResolution]]:
    """Complete pipeline: group → target → LIS → ordered moves.

    Args:
        items: Current playlist items.
        aliases_path: Optional path to artist_aliases.json.
        group_order: Group ordering strategy.
        within_group_sort: Optional sort applied inside each artist group.

    Returns:
        A tuple of (target_order, ordered_changes, report, resolutions).
    """
    target, groups, resolutions, channel_majority_overrides = group_by_artist(
        items, aliases_path, group_order, within_group_sort
    )
    changes, report = plan_reorder(items, target)

    # Enrich report with group info
    report.groups_found = list(groups.keys())
    report.group_details = {k: len(v) for k, v in groups.items()}
    report.unresolved_count = sum(
        1 for r in resolutions if r.method == "unknown"
    )
    report.metadata_missing_count = sum(
        1 for item in items if not item.metadata_available
    )
    report.channel_majority_overrides = channel_majority_overrides

    return target, changes, report, resolutions
