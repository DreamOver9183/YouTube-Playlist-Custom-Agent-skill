"""
test_optimizer.py — Unit tests for the optimizer module.

Tests cover:
1. LIS anchor computation correctness
2. Title regex parsing (real YouTube title formats)
3. Channel title normalization
4. Fuzzy matching with aliases
5. Full optimization pipeline (group + LIS + ordered move plan)
6. Position drift simulation (replayed against the API's real semantics)

See also ``tests/test_reorder_property.py`` for the exhaustive replay tests.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.optimizer import (
    _clean_title,
    _normalize_name,
    _parse_title_for_artist,
    _resolve_channel,
    _is_distributor_channel,
    _build_alias_lookup,
    _fuzzy_match_artist,
    compute_lis_anchors,
    build_move_plan,
    evaluate_grouping_benefit,
    find_duplicate_videos,
    group_by_artist,
    plan_reorder,
    resolve_artist,
    run_full_optimization,
)
from scripts.schemas import EnrichedPlaylistItem


# ─── Test Helpers ─────────────────────────────────


def make_item(
    pid: str,
    vid: str,
    title: str = "",
    channel: str = "",
    position: int = 0,
) -> EnrichedPlaylistItem:
    """Create a minimal EnrichedPlaylistItem for testing."""
    return EnrichedPlaylistItem(
        playlist_item_id=pid,
        video_id=vid,
        position=position,
        added_at=datetime.now(timezone.utc),
        playlist_id="PLtest",
        title=title,
        channel_title=channel,
    )


# ─── Test 1: Title Regex Parsing ──────────────────


def test_bracket_prefix_fullwidth():
    """【Artist】Song format."""
    r = _parse_title_for_artist("【YOASOBI】 夜に駆ける")
    assert r is not None
    assert r.artist_key == "yoasobi"
    assert r.method == "bracket_prefix"
    assert r.confidence == 0.95
    print("  ✓ bracket_prefix (fullwidth)")


def test_bracket_prefix_halfwidth():
    """[Artist] Song format."""
    r = _parse_title_for_artist("[aespa 에스파] 'Supernova' MV")
    assert r is not None
    assert "aespa" in r.artist_key
    assert r.method == "bracket_prefix"
    print("  ✓ bracket_prefix (halfwidth)")


def test_dash_separator_standard():
    """Standard ARTIST - SONG (Official MV) format."""
    r = _parse_title_for_artist("BTS (방탄소년단) - Dynamite (Official MV)")
    assert r is not None
    assert r.artist_key == "bts"
    assert r.method == "dash_separator"
    assert r.confidence == 0.80
    print("  ✓ dash_separator (BTS)")


def test_dash_separator_blackpink():
    """BLACKPINK - SONG M/V."""
    r = _parse_title_for_artist("BLACKPINK - 'Kill This Love' M/V")
    assert r is not None
    assert r.artist_key == "blackpink"
    assert r.method == "dash_separator"
    print("  ✓ dash_separator (BLACKPINK)")


def test_dash_separator_newjeans():
    """NewJeans (Korean) title — no dash, so tests bracket_suffix or fallback."""
    r = _parse_title_for_artist("NewJeans (뉴진스) - Hype Boy Official MV")
    # The dash-separator variant should extract NewJeans
    assert r is not None
    assert "newjeans" in r.artist_key
    print(f"  [PASS] NewJeans -> artist_key={r.artist_key}, method={r.method}")


def test_noise_removal():
    """Noise like 'Official Music Video' should be stripped."""
    cleaned = _clean_title("Some Song Official Music Video 4K Remastered")
    assert "official" not in cleaned.lower()
    assert "4k" not in cleaned.lower()
    assert "remastered" not in cleaned.lower()
    print("  ✓ noise removal")


# ─── Test 2: Channel Title Normalization ──────────


def test_normalize_channel_official():
    assert _normalize_name("BLACKPINK Official") == "blackpink"
    print("  ✓ normalize 'BLACKPINK Official' → 'blackpink'")


def test_normalize_channel_plain():
    assert _normalize_name("BTS") == "bts"
    print("  ✓ normalize 'BTS' → 'bts'")


def test_normalize_channel_vevo():
    assert _normalize_name("AdeleVEVO") == "adele"
    print("  ✓ normalize 'AdeleVEVO' → 'adele'")


def test_distributor_channel_blacklist():
    assert _is_distributor_channel("1theK") is True
    assert _is_distributor_channel("SMTOWN") is True
    assert _is_distributor_channel("BTS") is False
    print("  ✓ distributor channel blacklist")


def test_resolve_channel_normal():
    r = _resolve_channel("BLACKPINK")
    assert r is not None
    assert r.artist_key == "blackpink"
    assert r.method == "channel"
    print("  ✓ resolve_channel (BLACKPINK)")


def test_resolve_channel_distributor():
    r = _resolve_channel("Big Hit Labels")
    assert r is None
    print("  ✓ resolve_channel rejects distributors")


# ─── Test 3: Fuzzy Matching ──────────────────────


def test_fuzzy_alias_match():
    aliases = {"BTS": ["Bangtan Boys", "방탄소년단", "BTS (방탄소년단)"]}
    lookup = _build_alias_lookup(aliases)
    result = _fuzzy_match_artist("Bangtan Boys", lookup)
    assert result is not None
    assert result.artist_key == "bts"
    assert result.method == "fuzzy"
    print(f"  ✓ fuzzy match 'Bangtan Boys' → '{result.artist_key}' (score={result.confidence:.2f})")


def test_fuzzy_no_false_positive():
    aliases = {"BTS": ["Bangtan Boys"]}
    lookup = _build_alias_lookup(aliases)
    result = _fuzzy_match_artist("BTS Fan Channel Compilation", lookup, threshold=90.0)
    # Should NOT match with high threshold
    if result is not None:
        assert result.confidence < 0.90
    print("  ✓ fuzzy no false positive for 'BTS Fan Channel Compilation'")


# ─── Test 4: Three-Layer Resolution ─────────────


def test_resolve_artist_cross_validation():
    """Title + channel both say 'BTS' → confidence boosted."""
    item = make_item("p1", "v1", title="BTS - Dynamite", channel="BTS")
    r = resolve_artist(item, {})
    assert r.artist_key == "bts"
    assert r.confidence > 0.80
    assert "channel" in r.method
    print(f"  ✓ cross-validation: method={r.method}, confidence={r.confidence:.2f}")


def test_resolve_artist_channel_fallback():
    """Title has no clear artist pattern → use channel."""
    item = make_item("p1", "v1", title="Just a random video title", channel="Adele")
    r = resolve_artist(item, {})
    assert r.artist_key == "adele"
    assert r.method == "channel"
    print(f"  ✓ channel fallback: artist_key={r.artist_key}")


def test_resolve_artist_unknown():
    """Both title and channel are unresolvable."""
    item = make_item("p1", "v1", title="Random", channel="1theK")
    r = resolve_artist(item, {})
    # 1theK is a distributor, should fall through to unknown
    assert r.method == "unknown" or r.confidence < 0.70
    print(f"  ✓ unknown resolution: method={r.method}, confidence={r.confidence:.2f}")


# ─── Test 4b: Real-world channel/title shapes ─────
#
# Every case here comes from a real 202-track playlist where the resolver used
# to scatter one artist across up to nine groups.


def test_topic_channel_keeps_no_dangling_dash():
    """'Alan Walker - Topic' must normalise to the artist, not 'alan walker -'."""
    assert _normalize_name("Alan Walker - Topic") == "alan walker"
    assert _normalize_name("Linkin Park - Topic") == "linkin park"
    print("  ✓ '<Artist> - Topic' → '<artist>' (no dangling separator)")


def test_reversed_title_prefers_channel():
    """'Song [...] - Artist' must not turn the song name into the artist."""
    item = make_item("p1", "v1",
                     title="One More Light [Official Music Video] - Linkin Park",
                     channel="Linkin Park")
    r = resolve_artist(item, {})
    assert r.artist_key == "linkin park", r.artist_key
    print(f"  ✓ reversed title → {r.artist_key!r} (method={r.method})")


def test_version_annotation_prefers_channel():
    """'Song - Duet Version' is a version marker, not 'Song' by 'Song'."""
    item = make_item("p1", "v1", title="Valhalla Calling - Duet Version",
                     channel="Miracle Of Sound - Topic")
    r = resolve_artist(item, {})
    assert r.artist_key == "miracle of sound", r.artist_key
    print(f"  ✓ version tail → {r.artist_key!r} (method={r.method})")


def test_collaboration_prefers_owning_channel():
    """A collab title must stay in the uploading artist's group."""
    for title in ("Alan Walker & Torine - Hello World (Official Music Video)",
                  "Alan Walker vs Coldplay - Hymn For The Weekend",
                  "Hans Zimmer & Alan Walker – Time (Official Remix)"):
        r = resolve_artist(make_item("p1", "v1", title=title, channel="Alan Walker"), {})
        assert r.artist_key == "alan walker", f"{title} → {r.artist_key}"
    print("  ✓ 3 collaboration titles all resolve to 'alan walker'")


def test_collaboration_with_spaceless_vevo_channel():
    """VEVO channels drop the spaces; the collab check must still match."""
    item = make_item("p1", "v1", title="Lady Gaga, Bruno Mars - Die With A Smile",
                     channel="LadyGagaVEVO")
    r = resolve_artist(item, {})
    assert r.artist_key == "lady gaga", r.artist_key
    print(f"  ✓ 'LadyGagaVEVO' + collab title → {r.artist_key!r}")


def test_label_channel_still_yields_to_title():
    """A label channel must not swallow the artist named in the title."""
    item = make_item("p1", "v1",
                     title="Starship - Nothing's Gonna Stop Us Now (Official Video)",
                     channel="RHINO")
    r = resolve_artist(item, {})
    assert r.artist_key == "starship", r.artist_key
    print(f"  ✓ label channel 'RHINO' → title artist {r.artist_key!r}")


def test_empty_bracket_residue_never_becomes_a_group():
    """Noise removal used to leave 'Numb ()' as the group name."""
    item = make_item("p1", "v1", title="Numb (Official Music Video) [4K UPGRADE] – Linkin Park",
                     channel="Linkin Park")
    r = resolve_artist(item, {})
    assert "(" not in r.artist_key and ")" not in r.artist_key, r.artist_key
    assert r.artist_key == "linkin park", r.artist_key
    print("  ✓ no empty-bracket residue in the group key")


def test_group_merges_channel_spelling_variants():
    """Topic / VEVO / plain channels for one artist must form a single group."""
    items = [
        make_item("p1", "v1", title="Believer", channel="Imagine Dragons - Topic", position=0),
        make_item("p2", "v2", title="Imagine Dragons - Radioactive (Official Music Video)",
                  channel="ImagineDragonsVEVO", position=1),
        make_item("p3", "v3", title="Thunder", channel="Imagine Dragons", position=2),
    ]
    _target, groups, _res, _overrides = group_by_artist(items)
    assert len(groups) == 1, groups
    assert sum(len(v) for v in groups.values()) == 3
    print(f"  ✓ Topic + VEVO + plain → one group {list(groups)[0]!r}")


def test_group_merges_localised_name_prefix():
    """'紅髮艾德 Ed Sheeran' belongs with 'Ed Sheeran'."""
    items = [
        make_item("p1", "v1", title="Shape of You", channel="Ed Sheeran - Topic", position=0),
        make_item("p2", "v2", title="紅髮艾德 Ed Sheeran - Perfect 完美無瑕",
                  channel="華納音樂西洋日韓頻道", position=1),
    ]
    _target, groups, _res, _overrides = group_by_artist(items)
    assert len(groups) == 1, groups
    print(f"  ✓ localised prefix folded into {list(groups)[0]!r}")


def test_unrelated_artists_stay_apart():
    """The merge heuristics must not fuse genuinely different artists."""
    items = [
        make_item("p1", "v1", title="Siamese Dream", channel="Sia", position=0),
        make_item("p2", "v2", title="Chandelier", channel="Sia", position=1),
        make_item("p3", "v3", title="Skillet - Monster", channel="Atlantic Records", position=2),
        make_item("p4", "v4", title="Alphaville - Forever Young", channel="RHINO", position=3),
    ]
    _target, groups, _res, _overrides = group_by_artist(items)
    assert len(groups) == 3, groups
    print(f"  ✓ 3 distinct groups kept apart: {sorted(groups)}")


# ─── Test 4b: Channel-Majority Override (Gray-Zone Second-Guessing) ───
#
# See docs/agent/AGENT_SOP.md's `channel_majority_overrides` report field.
# resolve_artist() flags a gray-zone title win (confidence in [0.80, 0.90),
# today only dash_separator) with `channel_override_candidate`; these tests
# cover the aggregate pass in group_by_artist() that decides whether the
# channel's own majority actually overrides it.


def test_channel_majority_override_fixes_gray_zone_outlier():
    """A dash_separator outlier is overridden when its channel has an
    established majority elsewhere — the real Hiroyuki SAWANO case that
    motivated this mechanism (Aldnoah.Zero OST tracks on his Topic channel)."""
    items = [
        make_item("p1", "v1", title="Aldnoah.Zero OP", channel="Hiroyuki SAWANO - Topic", position=0),
        make_item("p2", "v2", title="&Z", channel="Hiroyuki SAWANO - Topic", position=1),
        make_item("p3", "v3", title="ignited", channel="Hiroyuki SAWANO - Topic", position=2),
        make_item("p4", "v4", title="Mio Mare (2V-Alk Version)", channel="Hiroyuki SAWANO - Topic", position=3),
        make_item("p5", "v5", title="VV-Alk", channel="Hiroyuki SAWANO - Topic", position=4),
    ]
    _target, groups, _res, overrides = group_by_artist(items)
    assert len(groups) == 1, groups
    assert list(groups)[0] == "hiroyuki sawano", list(groups)
    assert len(overrides) == 2, overrides
    assert {o.title for o in overrides} == {"Mio Mare (2V-Alk Version)", "VV-Alk"}
    for o in overrides:
        assert o.to_artist_key == "hiroyuki sawano", o
        assert o.corroborating_count == 3, o
    print("  ✓ 2 gray-zone outliers overridden back to channel majority 'hiroyuki sawano'")


def test_channel_majority_override_leaves_label_channel_alone():
    """A label/reissue channel hosting several different real artists must
    never be merged — none of them ever accumulate a channel-layer majority
    under one key, so the override must not fire for any of them."""
    items = [
        make_item("p1", "v1",
                   title="Starship - Nothing's Gonna Stop Us Now (Official Music Video) [HD]",
                   channel="RHINO", position=0),
        make_item("p2", "v2",
                   title="Alphaville - Forever Young (Official Music Video)",
                   channel="RHINO", position=1),
        make_item("p3", "v3",
                   title="Kim Wilde - Kids in America (Official Music Video)",
                   channel="RHINO", position=2),
    ]
    _target, groups, _res, overrides = group_by_artist(items)
    assert len(groups) == 3, groups
    assert overrides == [], overrides
    print("  ✓ label channel 'RHINO': 3 different artists stay apart, no override applied")


def test_channel_majority_override_needs_at_least_two_corroborators():
    """A single corroborating item is not enough evidence. This is the guard
    against the channel really hosting two different artists: with only one
    channel-layer hit, the gray-zone guess for the second artist must be left
    alone rather than steamrolled into the first artist's key."""
    items = [
        make_item("p1", "v1", title="Homecoming", channel="Indie Collective", position=0),
        make_item("p2", "v2", title="Artist Y - Roadtrip (Official Video)",
                   channel="Indie Collective", position=1),
    ]
    _target, groups, _res, overrides = group_by_artist(items)
    assert len(groups) == 2, groups
    assert overrides == [], overrides
    print("  ✓ single corroborator is not enough evidence; both artists stay apart")


def test_bracket_prefix_never_overridden_even_with_channel_majority():
    """An explicit 【Artist】 marker (confidence 0.95) is a deliberate,
    human-authored signal and must never be second-guessed, even when the
    channel has an established majority for a different artist."""
    items = [
        make_item("p1", "v1", title="Intro", channel="Various Uploads", position=0),
        make_item("p2", "v2", title="Interlude", channel="Various Uploads", position=1),
        make_item("p3", "v3", title="【Guest Artist】Special Collab Track",
                   channel="Various Uploads", position=2),
    ]
    _target, groups, _res, overrides = group_by_artist(items)
    assert len(groups) == 2, groups
    assert overrides == [], overrides
    print("  ✓ bracket_prefix (0.95) is never reconsidered, even with a 2-item channel majority")


# ─── Test 5: LIS Anchor Computation ─────────────


def test_lis_simple():
    """Simple case: [2, 0, 1, 3] → LIS = [0, 1, 3], length 3."""
    current = [make_item(f"p{i}", f"v{i}", position=i) for i in range(4)]
    # Target order: items 2, 0, 1, 3
    target = [current[2], current[0], current[1], current[3]]

    anchors = compute_lis_anchors(current, target)
    # Items 0, 1, 3 form the LIS (original indices 0, 1, 3 are increasing)
    # In target: target_original_indices = [2, 0, 1, 3]
    # LIS of [2, 0, 1, 3] → [0, 1, 3] = length 3
    assert len(anchors) == 3
    assert "p2" not in anchors  # item 2 was moved to front, not an anchor
    print(f"  ✓ LIS simple: {len(anchors)} anchors (expected 3)")


def test_lis_already_sorted():
    """If already in target order, all items are anchors."""
    items = [make_item(f"p{i}", f"v{i}", position=i) for i in range(5)]
    target = list(items)  # same order

    anchors = compute_lis_anchors(items, target)
    assert len(anchors) == 5
    print(f"  ✓ LIS already sorted: {len(anchors)} anchors (all 5)")


def test_lis_reversed():
    """Fully reversed: LIS length = 1 (worst case)."""
    items = [make_item(f"p{i}", f"v{i}", position=i) for i in range(5)]
    target = list(reversed(items))

    anchors = compute_lis_anchors(items, target)
    assert len(anchors) == 1  # Only 1 element can be kept
    print(f"  ✓ LIS fully reversed: {len(anchors)} anchors (expected 1)")


def test_lis_grouped_scenario():
    """Simulate grouping: 8 items from 2 artists, interleaved → grouped."""
    # Current: A0, B0, A1, B1, A2, B2, A3, B3
    items = []
    for i in range(4):
        items.append(make_item(f"a{i}", f"va{i}", title=f"ArtistA - Song{i}", channel="ArtistA"))
        items.append(make_item(f"b{i}", f"vb{i}", title=f"ArtistB - Song{i}", channel="ArtistB"))

    # Target: A0, A1, A2, A3, B0, B1, B2, B3
    target = [items[0], items[2], items[4], items[6], items[1], items[3], items[5], items[7]]

    anchors = compute_lis_anchors(items, target)
    moves = 8 - len(anchors)
    print(f"  ✓ LIS grouped scenario: {len(anchors)} anchors, {moves} moves needed (out of 8)")
    assert len(anchors) >= 4  # At least 4 items can stay (the A-series is already in order)


# ─── Test 6: Ordered Move Plan ───────────────────


def _replay(items, changes):
    """Simulate playlistItems.update: remove the item, re-insert at position."""
    live = [item.playlist_item_id for item in items]
    for change in sorted(changes, key=lambda c: c.execution_order):
        live.remove(change.playlist_item_id)
        live.insert(change.new_position, change.playlist_item_id)
    return live


def test_reversed_plan_replays_exactly():
    """A fully reversed playlist must replay to the exact reversed order."""
    items = [make_item(f"p{i}", f"v{i}", position=i) for i in range(5)]
    target = [items[4], items[3], items[2], items[1], items[0]]

    changes, report = plan_reorder(items, target)

    assert [c.execution_order for c in changes] == list(range(len(changes)))
    result = _replay(items, changes)
    expected = [item.playlist_item_id for item in target]
    assert result == expected, f"replay produced {result}, expected {expected}"
    assert report.need_to_move == 4  # 5 items - LIS length 1
    print(f"  ✓ ordered move plan: {len(changes)} moves replay to the exact target")


# ─── Test 7: Full Pipeline ──────────────────────


def test_full_optimization_pipeline():
    """End-to-end: group by artist → LIS → changes."""
    items = [
        make_item("p0", "v0", title="ArtistA - Song1", channel="ArtistA"),
        make_item("p1", "v1", title="ArtistB - Song1", channel="ArtistB"),
        make_item("p2", "v2", title="ArtistA - Song2", channel="ArtistA"),
        make_item("p3", "v3", title="ArtistB - Song2", channel="ArtistB"),
        make_item("p4", "v4", title="ArtistA - Song3", channel="ArtistA"),
        make_item("p5", "v5", title="ArtistC - Song1", channel="ArtistC"),
    ]

    target, changes, report, resolutions = run_full_optimization(items)

    print(f"  Total: {report.total_items}, Anchors: {report.anchors}, Moves: {report.need_to_move}")
    print(f"  Quota: {report.estimated_quota} units (saved {report.quota_saved_vs_naive} vs naive)")
    print(f"  Groups: {report.groups_found}")
    print(f"  Unresolved: {report.unresolved_count}")

    # Basic assertions
    assert report.total_items == 6
    assert report.need_to_move <= 6
    assert report.estimated_quota == report.need_to_move * 50
    assert len(report.groups_found) >= 3  # At least A, B, C
    assert report.unresolved_count == 0  # All should resolve via dash_separator + channel

    # Verify target is grouped
    target_artists = [_normalize_name(item.channel_title) for item in target]
    # Check that same artists are contiguous
    seen = set()
    current_artist = None
    for a in target_artists:
        if a != current_artist:
            assert a not in seen, f"Artist '{a}' appears in non-contiguous groups!"
            seen.add(a)
            current_artist = a

    print("  ✓ full pipeline: groups are contiguous, quota optimized")


def test_metadata_missing_items_are_flagged_and_still_movable():
    """videos.list omitting a video (e.g. region lock) must be distinguishable
    from a genuine artist-resolution failure, and must not block reordering."""
    items = [
        make_item("p0", "v0", title="ArtistA - Song1", channel="ArtistA"),
        make_item("p1", "v1", title="ArtistB - Song1", channel="ArtistB"),
        make_item("p2", "v2", title="", channel=""),  # videos.list gap
    ]
    items[2] = items[2].model_copy(update={"metadata_available": False})

    target, changes, report, resolutions = run_full_optimization(items)

    assert report.metadata_missing_count == 1
    assert report.unresolved_count == 1  # the gap item still resolves to "unknown"
    # The gap item is a normal movable item, not pinned like a private video.
    assert {item.playlist_item_id for item in target} == {"p0", "p1", "p2"}
    assert report.pinned_count == 0


def test_metadata_available_defaults_true_for_normal_items():
    item = make_item("p0", "v0", title="ArtistA - Song1", channel="ArtistA")
    assert item.metadata_available is True


def test_from_item_and_metadata_flags_missing_metadata():
    from scripts.schemas import PlaylistItemData, VideoMetadata, EnrichedPlaylistItem

    playlist_item = PlaylistItemData(
        playlist_item_id="p0",
        video_id="v0",
        position=0,
        added_at=datetime.now(timezone.utc),
        channel_title="",
        playlist_id="PLtest",
        is_available=True,
    )

    missing = EnrichedPlaylistItem.from_item_and_metadata(playlist_item, None)
    assert missing.metadata_available is False
    assert missing.is_available is True  # not the same thing as private/deleted

    present = EnrichedPlaylistItem.from_item_and_metadata(
        playlist_item,
        VideoMetadata(video_id="v0", title="ArtistA - Song1", channel_title="ArtistA"),
    )
    assert present.metadata_available is True


# ─── Test 8: Position Drift Simulation ──────────


def test_position_drift_simulation():
    """Replay the plan against the API's real reorder semantics.

    ``playlistItems.update`` removes the item and re-inserts it at ``position``,
    which is exactly a local ``list.remove`` + ``list.insert``.  A local
    simulation therefore *does* reproduce the server behaviour, and this test is
    the guard that catches drift before it reaches a user's playlist.
    """
    # 10 items, simple reverse (worst case for drift)
    n = 10
    items = [make_item(f"p{i}", f"v{i}", position=i) for i in range(n)]
    target = list(reversed(items))

    anchors = compute_lis_anchors(items, target)
    changes = build_move_plan(items, target, anchors)

    # Structural properties
    assert len(changes) > 0, "Should have changes for reversed list"
    assert len(changes) == n - len(anchors), "Changes = total - anchors"

    # Every change references a real, non-anchored item
    item_ids = {item.playlist_item_id for item in items}
    for change in changes:
        assert change.playlist_item_id in item_ids
        assert change.playlist_item_id not in anchors

    # The actual guarantee: replaying the plan yields the target order
    result = _replay(items, changes)
    expected = [item.playlist_item_id for item in target]
    assert result == expected, f"drift! replay produced {result}, expected {expected}"

    print(f"  [PASS] drift simulation: {len(changes)} changes replay to the exact target")


# ─── Main ────────────────────────────────────────


def _write_aliases(mapping):
    """Write an alias table to a temp file and return its path."""
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(mapping, handle, ensure_ascii=False)
    handle.close()
    return Path(handle.name)


# --- Topic channels are the richest artist signal; keep them out of the blacklist ---


def test_topic_channel_is_not_a_distributor():
    """"<Artist> - Topic" must resolve through the channel layer, not be blacklisted.

    _is_distributor_channel once carried an ``endswith(" - topic")`` branch that
    could never fire, because _normalize_name strips the suffix first.  Deleting
    dead code is harmless; "repairing" it against the raw title would not be.
    Topic channels are auto-generated per artist, they carry 67% of the items in
    the collected samples, and 96.6% of those resolve correctly this way.  Send
    them to title parsing instead and you get song titles as artist names.
    """
    assert not _is_distributor_channel("tuki. - Topic")
    assert not _is_distributor_channel("Hiroyuki SAWANO - Topic")

    resolved = _resolve_channel("tuki. - Topic")
    assert resolved is not None
    assert resolved.artist_key == "tuki."
    assert resolved.method == "channel"
    assert resolved.confidence == 0.75

    # Real label channels must still be rejected.
    assert _is_distributor_channel("HYBE LABELS")
    print("  ✓ Topic channels resolve to the artist and are not distributors")


def test_song_title_can_still_hijack_a_topic_channel_without_aliases():
    """A known, measured limitation — and the reason --aliases exists.

    "<Song> - <Romanisation>" is a dash_separator match at 0.80, which is enough
    to beat the channel's 0.75.  That lands it in the gray zone, where
    _apply_channel_majority_override would normally put it back — but the
    override needs >=2 *other* items on the same channel that resolved through
    the channel layer, and when every track on the channel is titled this way
    there are none.  Measured on the collected samples this hits 21 of 623
    Topic-channel items (3.4%); the other 602 resolve to the channel correctly.

    The remedy is the alias table, not a looser threshold: dropping the
    corroboration requirement would let a single label-channel video override a
    correctly named artist.  data/artist_aliases.json therefore lists these song
    titles under their artist, and the canonicalisation pass in group_by_artist
    folds them back together.
    """
    items = [
        make_item("p1", "v1", "晚餐歌 - Bansanka", "tuki. - Topic", 0),
        make_item("p2", "v2", "一輪花 - Ichirinka", "tuki. - Topic", 1),
        make_item("p3", "v3", "地獄恋文 - Inferno Love Letter", "tuki. - Topic", 2),
    ]

    _, groups_without, _, _ = group_by_artist(items)
    assert len(groups_without) == 3, (
        f"documented limitation changed: {list(groups_without)}"
    )

    aliases = _write_aliases(
        {"tuki.": ["晚餐歌", "一輪花", "地獄恋文"]}
    )
    _, groups_with, _, _ = group_by_artist(items, aliases)
    assert len(groups_with) == 1, f"aliases should rescue them: {list(groups_with)}"
    assert sum(len(v) for v in groups_with.values()) == 3
    print("  ✓ title-hijacked Topic tracks split without aliases, merge with them")


# --- Alias canonicalisation ---


def test_aliases_merge_recognised_but_differently_spelled_keys():
    """The alias table must merge groups that were recognised, just spelled apart."""
    items = [
        make_item("p1", "v1", "Lemon", "Kenshi Yonezu - Topic", 0),
        make_item("p2", "v2", "パプリカ", "米津玄師 - Topic", 1),
        make_item("p3", "v3", "KICK BACK", "kenshi yonezu - Topic", 2),
    ]

    tmp_aliases = _write_aliases(
        {"米津玄師": ["Kenshi Yonezu", "kenshi yonezu"]}
    )

    _, groups_without, _, _ = group_by_artist(items)
    assert len(groups_without) == 2, (
        f"cross-language split should survive without aliases: {list(groups_without)}"
    )

    _, groups_with, _, _ = group_by_artist(items, tmp_aliases)
    assert len(groups_with) == 1, f"aliases should merge them: {list(groups_with)}"
    assert sum(len(v) for v in groups_with.values()) == 3
    print("  ✓ --aliases merges kenshi yonezu / 米津玄師 into one group")


def test_alias_canonical_is_normalised_not_verbatim():
    """Group keys stay _normalize_name output whatever case the alias file uses.

    Adopting the alias file's own capitalisation would make the display name
    depend on item order: an alias-matched "Aurora" and a channel-derived
    "aurora" share a _loose bucket where display_of breaks ties by
    (spaces, length) — identical for both.
    """
    items = [
        make_item("p1", "v1", "Song A", "Aurora Borealis Project", 0),
        make_item("p2", "v2", "Song B", "aurora", 1),
    ]
    tmp_aliases = _write_aliases({"Aurora": ["Aurora Borealis Project"]})
    _, groups, _, _ = group_by_artist(items, tmp_aliases)
    assert len(groups) == 1, f"expected one merged group: {list(groups)}"
    key = next(iter(groups))
    assert key == key.lower(), f"group key should be normalised, got {key!r}"
    print("  ✓ alias canonical name is normalised, not copied verbatim")


# --- Grouping benefit gate ---


def _benefit_for(keys, methods=None, pinned=0):
    """Build groups/resolutions straight from a list of artist keys."""
    from scripts.schemas import ArtistResolution

    resolutions = []
    groups = {}
    for idx, key in enumerate(keys):
        method = (methods or {}).get(idx, "channel" if key != "unknown" else "unknown")
        resolutions.append(
            ArtistResolution(
                artist_key=key, confidence=0.75, method=method, raw_candidate=key
            )
        )
        groups.setdefault(key, []).append(idx)
    # Pinned items are outside `groups` entirely, exactly as group_by_artist
    # leaves them.
    for extra in range(pinned):
        resolutions.append(
            ArtistResolution(
                artist_key="__pinned__",
                confidence=0.0,
                method="unavailable",
                raw_candidate=f"pinned{extra}",
            )
        )
    return evaluate_grouping_benefit(groups, resolutions)


def test_gate_flags_a_playlist_that_is_all_singletons():
    """40 groups holding 44 songs is a wasted run; the gate must say so."""
    benefit = _benefit_for([f"artist{i}" for i in range(10)])
    assert benefit.verdict == "low"
    assert benefit.effective_grouping_ratio == 0.0
    assert benefit.orphan_group_count == 10
    assert any("聚在一起" in w for w in benefit.warnings)
    print("  ✓ all-singleton playlist is flagged low benefit")


def test_gate_flags_high_unknown_share():
    """Label channels (HYBE, SMTOWN) resolve to nothing; that is not grouping."""
    keys = ["unknown"] * 4 + ["aespa"] * 3 + ["illit"] * 3
    benefit = _benefit_for(keys)
    assert benefit.verdict == "low"
    assert benefit.unknown_ratio == 0.4
    assert any("unknown" in w for w in benefit.warnings)
    print("  ✓ high unknown share is flagged low benefit")


def test_gate_does_not_count_unknown_as_a_group():
    """The unknown bucket is a bin, not a cluster — it must not inflate the ratio."""
    keys = ["unknown"] * 5 + ["aespa"] * 5
    benefit = _benefit_for(keys)
    # Counting unknown as a group would give 1.0 here.
    assert benefit.effective_grouping_ratio == 0.5, benefit.effective_grouping_ratio
    print("  ✓ unknown bucket excluded from effective grouping ratio")


def test_gate_ignores_pinned_items_in_the_denominator():
    """Unavailable videos never take part in grouping, so they cannot dilute it."""
    keys = ["aespa"] * 5 + ["illit"] * 5
    without_pinned = _benefit_for(keys)
    with_pinned = _benefit_for(keys, pinned=10)
    assert without_pinned.effective_grouping_ratio == 1.0
    assert with_pinned.effective_grouping_ratio == 1.0
    assert with_pinned.verdict == "ok"
    print("  ✓ pinned items stay out of the grouping-benefit denominator")


def test_gate_passes_a_genuinely_groupable_playlist():
    """A playlist that really does cluster must not be warned about."""
    keys = ["aespa"] * 6 + ["illit"] * 5 + ["bts"] * 4 + ["solo"]
    benefit = _benefit_for(keys)
    assert benefit.verdict == "ok"
    assert benefit.warnings == []
    assert benefit.orphan_group_count == 1
    print("  ✓ a genuinely groupable playlist passes the gate")


# --- Duplicate detection (exact video_id only) ---


def test_duplicates_found_in_the_real_192_item_playlist():
    """The Task3 baseline is 192 items over 188 videos — four known repeats."""
    cache = PROJECT_ROOT / "scripts" / "cache" / "playlist_PLLpKeZeMXlNY.json"
    if not cache.is_file():
        print("  - skipped: cache fixture not present")
        return
    raw = json.loads(cache.read_text(encoding="utf-8"))["items"]
    items = [EnrichedPlaylistItem.model_validate(r) for r in raw]

    groups = find_duplicate_videos(items)
    assert len(items) == 192, len(items)
    assert len(groups) == 4, f"expected 4 duplicate videos, got {len(groups)}"
    assert sum(len(g["removable"]) for g in groups) == 4

    found = {g["video_id"] for g in groups}
    assert found == {"_lxS-x7DACQ", "nSkDpIj0Y7w", "2NiyrtYegso", "V-KY9Z3WMi8"}, found

    for group in groups:
        keep_pos = group["keep"]["position"]
        assert all(r["position"] > keep_pos for r in group["removable"]), (
            "the earliest occurrence must be the one kept"
        )
    print("  ✓ 4 duplicate videos found in the 192-item playlist")


def test_no_duplicates_reports_nothing():
    """A clean playlist must produce an empty report, not a near-miss."""
    items = [make_item(f"p{i}", f"v{i}", f"Song {i}", "Artist - Topic", i) for i in range(6)]
    assert find_duplicate_videos(items) == []
    print("  ✓ clean playlist reports no duplicates")


def test_different_versions_are_not_duplicates():
    """aLIEz vs aLIEz (Remastered): different videos, deliberately collected."""
    items = [
        make_item("p1", "v1", "aLIEz", "Hiroyuki SAWANO - Topic", 0),
        make_item("p2", "v2", "aLIEz (Remastered)", "Hiroyuki SAWANO - Topic", 1),
        make_item("p3", "v3", "sh0ut", "Hiroyuki SAWANO - Topic", 2),
        make_item("p4", "v4", "sh0ut (Remastered)", "Hiroyuki SAWANO - Topic", 3),
    ]
    assert find_duplicate_videos(items) == [], (
        "title-similarity matching was removed on purpose; these are 4 distinct videos"
    )
    print("  ✓ remastered versions are not reported as duplicates")


def run_all_tests():
    """Run all tests and report results."""
    tests = [
        ("Title Regex Parsing", [
            test_bracket_prefix_fullwidth,
            test_bracket_prefix_halfwidth,
            test_dash_separator_standard,
            test_dash_separator_blackpink,
            test_dash_separator_newjeans,
            test_noise_removal,
        ]),
        ("Channel Title Normalization", [
            test_normalize_channel_official,
            test_normalize_channel_plain,
            test_normalize_channel_vevo,
            test_distributor_channel_blacklist,
            test_resolve_channel_normal,
            test_resolve_channel_distributor,
        ]),
        ("Fuzzy Matching", [
            test_fuzzy_alias_match,
            test_fuzzy_no_false_positive,
        ]),
        ("Three-Layer Resolution", [
            test_resolve_artist_cross_validation,
            test_resolve_artist_channel_fallback,
            test_resolve_artist_unknown,
        ]),
        ("Real-world Channel / Title Shapes", [
            test_topic_channel_keeps_no_dangling_dash,
            test_reversed_title_prefers_channel,
            test_version_annotation_prefers_channel,
            test_collaboration_prefers_owning_channel,
            test_collaboration_with_spaceless_vevo_channel,
            test_label_channel_still_yields_to_title,
            test_empty_bracket_residue_never_becomes_a_group,
            test_group_merges_channel_spelling_variants,
            test_group_merges_localised_name_prefix,
            test_unrelated_artists_stay_apart,
        ]),
        ("Channel-Majority Override (Gray-Zone Second-Guessing)", [
            test_channel_majority_override_fixes_gray_zone_outlier,
            test_channel_majority_override_leaves_label_channel_alone,
            test_channel_majority_override_needs_at_least_two_corroborators,
            test_bracket_prefix_never_overridden_even_with_channel_majority,
        ]),
        ("LIS Anchor Computation", [
            test_lis_simple,
            test_lis_already_sorted,
            test_lis_reversed,
            test_lis_grouped_scenario,
        ]),
        ("Ordered Move Plan", [
            test_reversed_plan_replays_exactly,
        ]),
        ("Full Pipeline", [
            test_full_optimization_pipeline,
        ]),
        ("Metadata Availability", [
            test_metadata_missing_items_are_flagged_and_still_movable,
            test_metadata_available_defaults_true_for_normal_items,
            test_from_item_and_metadata_flags_missing_metadata,
        ]),
        ("Position Drift Simulation", [
            test_position_drift_simulation,
        ]),
        ("Topic Channels", [
            test_topic_channel_is_not_a_distributor,
            test_song_title_can_still_hijack_a_topic_channel_without_aliases,
        ]),
        ("Alias Canonicalisation", [
            test_aliases_merge_recognised_but_differently_spelled_keys,
            test_alias_canonical_is_normalised_not_verbatim,
        ]),
        ("Duplicate Detection", [
            test_duplicates_found_in_the_real_192_item_playlist,
            test_no_duplicates_reports_nothing,
            test_different_versions_are_not_duplicates,
        ]),
        ("Grouping Benefit Gate", [
            test_gate_flags_a_playlist_that_is_all_singletons,
            test_gate_flags_high_unknown_share,
            test_gate_does_not_count_unknown_as_a_group,
            test_gate_ignores_pinned_items_in_the_denominator,
            test_gate_passes_a_genuinely_groupable_playlist,
        ]),
    ]

    total = 0
    passed = 0
    failed = 0

    for group_name, test_funcs in tests:
        print(f"\n{'='*60}")
        print(f" {group_name}")
        print(f"{'='*60}")
        for test_func in test_funcs:
            total += 1
            try:
                test_func()
                passed += 1
            except Exception as exc:
                failed += 1
                print(f"  ✗ {test_func.__name__}: {exc}")

    print(f"\n{'='*60}")
    print(f" Results: {passed}/{total} passed, {failed} failed")
    print(f"{'='*60}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
