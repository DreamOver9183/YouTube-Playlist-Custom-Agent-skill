"""
test_optimizer.py — Unit tests for the optimizer module.

Tests cover:
1. LIS anchor computation correctness
2. Title regex parsing (real YouTube title formats)
3. Channel title normalization
4. Fuzzy matching with aliases
5. Full optimization pipeline (group + LIS + drift-safe ordering)
6. Position drift simulation
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
    build_optimized_changes,
    group_by_artist,
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


# ─── Test 6: Drift-Safe Ordering ─────────────────


def test_changes_sorted_tail_first():
    """Changes should be sorted by new_position descending."""
    items = [make_item(f"p{i}", f"v{i}", position=i) for i in range(5)]
    target = [items[4], items[3], items[2], items[1], items[0]]

    anchors = compute_lis_anchors(items, target)
    changes = build_optimized_changes(items, target, anchors)

    positions = [c.new_position for c in changes]
    for i in range(len(positions) - 1):
        assert positions[i] >= positions[i + 1], \
            f"Not tail-first! pos[{i}]={positions[i]} < pos[{i+1}]={positions[i+1]}"
    print(f"  ✓ drift-safe ordering: {positions}")


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


# ─── Test 8: Position Drift Simulation ──────────


def test_position_drift_simulation():
    """Verify the optimizer outputs changes and that they are tail-first ordered.
    
    NOTE: A local pop/insert simulation does NOT perfectly replicate YouTube's
    server-side behavior because YouTube reindexes atomically after each update.
    The tail-first ordering is the best-effort strategy, but the true correctness
    can only be validated against the live API. This test verifies the structural
    properties of the output.
    """
    # 10 items, simple reverse (worst case for drift)
    n = 10
    items = [make_item(f"p{i}", f"v{i}", position=i) for i in range(n)]
    target = list(reversed(items))

    anchors = compute_lis_anchors(items, target)
    changes = build_optimized_changes(items, target, anchors)

    # Verify structural properties
    assert len(changes) > 0, "Should have changes for reversed list"
    assert len(changes) == n - len(anchors), "Changes = total - anchors"

    # Verify tail-first ordering
    positions = [c.new_position for c in changes]
    for i in range(len(positions) - 1):
        assert positions[i] >= positions[i + 1], "Must be tail-first"

    # Verify all changes reference valid items
    item_ids = {item.playlist_item_id for item in items}
    for change in changes:
        assert change.playlist_item_id in item_ids
        assert change.playlist_item_id not in anchors

    print(f"  [PASS] drift simulation: {len(changes)} changes, tail-first order verified")


# ─── Main ────────────────────────────────────────


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
        ("LIS Anchor Computation", [
            test_lis_simple,
            test_lis_already_sorted,
            test_lis_reversed,
            test_lis_grouped_scenario,
        ]),
        ("Drift-Safe Ordering", [
            test_changes_sorted_tail_first,
        ]),
        ("Full Pipeline", [
            test_full_optimization_pipeline,
        ]),
        ("Position Drift Simulation", [
            test_position_drift_simulation,
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
