"""
test_optimizer.py — Unit tests for the optimizer module.

Tests cover:
1. LIS anchor computation correctness
2. Full optimization pipeline (group + LIS + ordered move plan)
3. Position drift simulation (replayed against the API's real semantics)
4. Grouping with aliases and Topic channels, duplicates, the grouping-benefit gate

The artist formula itself is tested in ``tests/test_resolver_formula.py``; see
also ``tests/test_reorder_property.py`` for the exhaustive replay tests.
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
    compute_lis_anchors,
    build_move_plan,
    evaluate_grouping_benefit,
    find_duplicate_videos,
    group_by_artist,
    plan_reorder,
    run_full_optimization,
)
from scripts.resolver import Resolution, normalize
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
    assert report.unresolved_count == 0  # every item has its artist's own channel

    # Verify target is grouped
    target_artists = [normalize(item.channel_title) for item in target]
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


# --- Topic channels and aliases ---


def test_topic_channel_beats_a_dash_title():
    """"<Song> - <Romanisation>" on an "<Artist> - Topic" channel stays with the artist.

    v3 let a 0.80 dash_separator title beat the channel's 0.75, so these split
    into one group per song unless an alias file listed every title.  The
    Topic channel is now a platform field (P, weight 1.0), and a title guess
    (T, 0.25 × 0.80 without an Agent label) cannot outweigh it.
    """
    items = [
        make_item("p1", "v1", "晚餐歌 - Bansanka", "tuki. - Topic", 0),
        make_item("p2", "v2", "一輪花 - Ichirinka", "tuki. - Topic", 1),
        make_item("p3", "v3", "地獄恋文 - Inferno Love Letter", "tuki. - Topic", 2),
    ]
    _, groups, _ = group_by_artist(items)
    assert list(groups) == ["tuki."], f"expected one tuki. group: {list(groups)}"
    print("  ✓ Topic channel keeps title-hijacked tracks with their artist")


def test_aliases_merge_recognised_but_differently_spelled_keys():
    """The alias table must merge groups that were recognised, just spelled apart."""
    items = [
        make_item("p1", "v1", "Lemon", "Kenshi Yonezu - Topic", 0),
        make_item("p2", "v2", "パプリカ", "米津玄師 - Topic", 1),
        make_item("p3", "v3", "KICK BACK", "kenshi yonezu - Topic", 2),
    ]
    _, groups_without, _ = group_by_artist(items)
    assert len(groups_without) == 2, (
        f"cross-language split should survive without aliases: {list(groups_without)}"
    )
    _, groups_with, resolutions = group_by_artist(items, aliases={"米津玄師": ["Kenshi Yonezu"]})
    assert len(groups_with) == 1, f"aliases should merge them: {list(groups_with)}"
    assert sum(len(v) for v in groups_with.values()) == 3
    assert len({r.key for r in resolutions}) == 1
    print("  ✓ aliases merge kenshi yonezu / 米津玄師 into one group")


def test_agent_label_overrides_a_label_channel():
    """A K-pop video on a multi-artist label channel has no P, T or usable C: only the Agent knows."""
    items = [
        make_item("p1", "v1", 'TWICE "TT" M/V', "JYP Entertainment", 0),
        make_item("p2", "v2", 'ITZY "WANNABE" M/V', "JYP Entertainment", 1),
        make_item("p3", "v3", 'TWICE "FANCY" M/V', "JYP Entertainment", 2),
    ]
    _, groups, _ = group_by_artist(items)
    assert list(groups) == ["unknown"], f"label channel is not an artist: {list(groups)}"
    labels = {f"p{n}": {"artist": artist, "conf": "high"} for n, artist in ((1, "TWICE"), (2, "ITZY"), (3, "TWICE"))}
    target, groups, _ = group_by_artist(items, labels=labels)
    assert groups == {"TWICE": [0, 2], "ITZY": [1]}, groups
    assert [item.playlist_item_id for item in target] == ["p1", "p3", "p2"]
    print("  ✓ Agent labels group a label channel the formula cannot read")


# --- Grouping benefit gate ---


def _benefit_for(keys, pinned=0):
    """Build groups/resolutions straight from a list of artist keys."""
    resolutions = []
    groups = {}
    for idx, key in enumerate(keys):
        resolutions.append(Resolution(key, key, 1.0, "C" if key != "unknown" else ""))
        groups.setdefault(key, []).append(idx)
    # Pinned items are outside `groups` entirely, exactly as group_by_artist
    # leaves them.
    resolutions += [Resolution("__pinned__", "__pinned__", 0.0, "")] * pinned
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
        ("Grouping: Topic Channels, Aliases, Agent Labels", [
            test_topic_channel_beats_a_dash_title,
            test_aliases_merge_recognised_but_differently_spelled_keys,
            test_agent_label_overrides_a_label_channel,
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
    # Windows consoles default to cp950/cp1252 and cannot print ✓/✗.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    success = run_all_tests()
    sys.exit(0 if success else 1)
