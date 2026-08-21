"""
test_reorder_property.py — Property tests for the reorder planner.

The single most important guarantee in this project is that replaying the
generated change set produces *exactly* the target ordering.  Everything else
(quota savings, grouping quality) is worthless if the playlist ends up
scrambled — and each wrong move still costs 50 quota units.

These tests replay every plan against a local simulation of
``playlistItems.update`` and compare the result to the target:

1. Exhaustive: all permutations of n = 2..7 (5912 cases).
2. Minimality: the number of API calls always equals the theoretical
   lower bound ``N - LIS``.
3. Randomised: larger playlists (n = 50 / 200).
4. Pinned items: private / deleted videos stay at their original index and
   never receive an API call.
5. Resume: interrupting the plan and continuing still lands on the target.
6. Guards: non-permutation targets are rejected instead of silently written.
"""

import logging
import random
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.optimizer import (
    build_move_plan,
    compute_lis_anchors,
    normalize_target,
    plan_reorder,
    run_full_optimization,
)
from scripts.schemas import EnrichedPlaylistItem, SortConfig, SortField, SortOrder


# ─── Helpers ──────────────────────────────────────


def make_item(
    pid: str,
    title: str = "",
    channel: str = "",
    position: int = 0,
    is_available: bool = True,
    view_count: int = 0,
) -> EnrichedPlaylistItem:
    """Create a minimal EnrichedPlaylistItem for testing."""
    return EnrichedPlaylistItem(
        playlist_item_id=pid,
        video_id=f"v_{pid}",
        position=position,
        added_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        playlist_id="PLtest",
        title=title,
        channel_title=channel,
        is_available=is_available,
        view_count=view_count,
    )


def build_playlist(n: int, unavailable: set[int] | None = None) -> list[EnrichedPlaylistItem]:
    """Build a playlist of *n* items; indices in *unavailable* are pinned."""
    unavailable = unavailable or set()
    return [
        make_item(f"p{i}", title=f"Song {i}", position=i, is_available=i not in unavailable)
        for i in range(n)
    ]


def replay(current, changes) -> list[str]:
    """Simulate YouTube's ``playlistItems.update`` semantics.

    The API removes the item from the playlist and re-inserts it so that it
    ends up at ``position``; every other item is re-indexed.  Changes must be
    replayed in ``execution_order``.
    """
    live = [item.playlist_item_id for item in current]
    for change in sorted(changes, key=lambda c: c.execution_order):
        live.remove(change.playlist_item_id)
        live.insert(change.new_position, change.playlist_item_id)
    return live


def lis_length(current, target) -> int:
    """Theoretical lower bound helper: length of the LIS of original positions."""
    original = {item.playlist_item_id: idx for idx, item in enumerate(current)}
    seq = [original[item.playlist_item_id] for item in target]
    tails: list[int] = []
    for value in seq:
        pos = bisect_left(tails, value)
        if pos == len(tails):
            tails.append(value)
        else:
            tails[pos] = value
    return len(tails)


def ids(items) -> list[str]:
    return [item.playlist_item_id for item in items]


# ─── Test 1 + 2: exhaustive correctness and minimality ───


def test_exhaustive_permutations():
    """All permutations of n = 2..7: replay must reproduce the target exactly."""
    checked = 0
    for n in range(2, 8):
        current = build_playlist(n)
        for perm in permutations(range(n)):
            target = [current[i] for i in perm]
            changes, report = plan_reorder(current, target)

            result = replay(current, changes)
            assert result == ids(target), (
                f"n={n} perm={perm}: replay produced {result}, expected {ids(target)}"
            )
            assert report.need_to_move == len(changes)
            assert report.estimated_quota == len(changes) * 50
            checked += 1
    print(f"  ✓ exhaustive: {checked} permutations replayed to the exact target")


def test_move_count_is_minimal():
    """The plan always uses the theoretical minimum of N - LIS API calls."""
    checked = 0
    for n in range(2, 8):
        current = build_playlist(n)
        for perm in permutations(range(n)):
            target = [current[i] for i in perm]
            changes, _ = plan_reorder(current, target)
            lower_bound = n - lis_length(current, target)
            assert len(changes) == lower_bound, (
                f"n={n} perm={perm}: {len(changes)} moves, lower bound {lower_bound}"
            )
            checked += 1
    print(f"  ✓ minimality: {checked} permutations all hit the N - LIS lower bound")


def test_already_sorted_is_free():
    """A playlist already in the target order costs nothing."""
    current = build_playlist(30)
    changes, report = plan_reorder(current, list(current))
    assert changes == []
    assert report.estimated_quota == 0
    assert report.anchors == 30
    print("  ✓ no-op reorder costs 0 units")


# ─── Test 3: randomised larger playlists ───────────


def test_random_large_playlists():
    """Randomised playlists of 50 and 200 items."""
    rng = random.Random(20260821)
    for n in (50, 200):
        for _ in range(30):
            current = build_playlist(n)
            target = list(current)
            rng.shuffle(target)

            changes, _ = plan_reorder(current, target)
            assert replay(current, changes) == ids(target), f"n={n} random shuffle failed"
            assert len(changes) == n - lis_length(current, target)
    print("  ✓ randomised: 60 large shuffles replayed exactly, all minimal")


def test_local_shuffle_is_cheap():
    """A mostly-sorted playlist should need very few moves (the LIS payoff)."""
    rng = random.Random(7)
    n = 200
    current = build_playlist(n)
    target = list(current)
    # Move 20 random items to random spots — the rest stays in order.
    for _ in range(20):
        item = target.pop(rng.randrange(len(target)))
        target.insert(rng.randrange(len(target) + 1), item)

    changes, report = plan_reorder(current, target)
    assert replay(current, changes) == ids(target)
    assert len(changes) <= 40, f"expected a cheap plan, got {len(changes)} moves"
    assert report.quota_saved_vs_naive > 0
    print(f"  ✓ local shuffle: {len(changes)} moves for {n} items "
          f"(saved {report.quota_saved_vs_naive} units vs naive)")


# ─── Test 4: pinned (private / deleted) items ──────


def test_pinned_items_never_move():
    """Unavailable items keep their exact index and get no API call."""
    rng = random.Random(99)
    for trial in range(50):
        n = rng.randint(4, 14)
        hidden = set(rng.sample(range(n), k=rng.randint(1, max(1, n // 3))))
        current = build_playlist(n, unavailable=hidden)

        movable = [item for item in current if item.is_available]
        shuffled = list(movable)
        rng.shuffle(shuffled)
        # Caller supplies only the movable order; normalize_target pins the rest.
        target = normalize_target(current, shuffled + [current[i] for i in sorted(hidden)])

        changes, report = plan_reorder(current, target)
        result = replay(current, changes)

        assert result == ids(target), f"trial {trial}: {result} != {ids(target)}"
        assert report.pinned_count == len(hidden)
        for idx in hidden:
            assert result[idx] == f"p{idx}", (
                f"trial {trial}: pinned item p{idx} drifted to {result.index(f'p{idx}')}"
            )
        moved_ids = {c.playlist_item_id for c in changes}
        assert not moved_ids & {f"p{i}" for i in hidden}, "a pinned item was moved"
    print("  ✓ pinned items: 50 trials, hidden videos stayed put and cost 0 units")


def test_pinned_anchor_set_stays_increasing():
    """Regression: pinned + LIS anchors must remain a valid increasing subsequence.

    Naively unioning the unconstrained LIS with the pinned ids produces an
    unsatisfiable plan (e.g. current [A, P, B] with P pinned, target [B, P, A]).
    """
    current = [
        make_item("A", position=0),
        make_item("P", position=1, is_available=False),
        make_item("B", position=2),
    ]
    target = normalize_target(current, [current[2], current[1], current[0]])
    changes, _ = plan_reorder(current, target)
    assert replay(current, changes) == ["B", "P", "A"]

    anchors = compute_lis_anchors(current, target)
    assert "P" in anchors
    build_move_plan(current, target, anchors)  # must not raise
    print("  ✓ pinned anchors: constrained LIS keeps the plan satisfiable")


# ─── Test 5: resume after interruption ─────────────


def test_resume_midway_still_lands_on_target():
    """Applying the first k moves, then the rest, reaches the same target."""
    rng = random.Random(4242)
    n = 40
    current = build_playlist(n)
    target = list(current)
    rng.shuffle(target)

    changes, _ = plan_reorder(current, target)
    ordered = sorted(changes, key=lambda c: c.execution_order)

    for cut in (1, len(ordered) // 3, len(ordered) // 2, len(ordered) - 1):
        if cut <= 0:
            continue
        live = ids(current)
        for change in ordered[:cut]:
            live.remove(change.playlist_item_id)
            live.insert(change.new_position, change.playlist_item_id)
        # Resume from where we stopped, same plan, no recomputation.
        for change in ordered[cut:]:
            live.remove(change.playlist_item_id)
            live.insert(change.new_position, change.playlist_item_id)
        assert live == ids(target), f"resume at {cut} diverged"
    print("  ✓ resume: interrupting at any point and continuing still lands on target")


def test_execution_order_is_dense_and_ordered():
    """execution_order must be 0..N-1 so a resume index is unambiguous."""
    rng = random.Random(11)
    current = build_playlist(25)
    target = list(current)
    rng.shuffle(target)
    changes, _ = plan_reorder(current, target)
    assert [c.execution_order for c in changes] == list(range(len(changes)))
    for change in changes:
        assert 0 <= change.new_position < 25
        assert 0 <= change.final_position < 25
    print("  ✓ execution_order is dense, ordered, and positions are in range")


# ─── Test 6: guards ────────────────────────────────


def test_non_permutation_target_is_rejected():
    """A target that drops or adds items must fail loudly, not silently write."""
    current = build_playlist(5)

    try:
        normalize_target(current, current[:3])
    except ValueError as exc:
        assert "permutation" in str(exc)
    else:
        raise AssertionError("dropping items should raise ValueError")

    extra = current + [make_item("ghost", position=99)]
    try:
        normalize_target(current, extra)
    except ValueError as exc:
        assert "permutation" in str(exc)
    else:
        raise AssertionError("unknown items should raise ValueError")
    print("  ✓ guards: non-permutation targets rejected before any API call")


# ─── Test 7: full grouping pipeline end-to-end ─────


def test_full_pipeline_replays_exactly():
    """run_full_optimization output must replay to its own target ordering."""
    rng = random.Random(2026)
    artists = ["ArtistA", "ArtistB", "ArtistC", "ArtistD"]
    items = []
    for i in range(60):
        artist = artists[rng.randrange(len(artists))]
        items.append(
            make_item(
                f"p{i}",
                title=f"{artist} - Song {i}",
                channel=artist,
                position=i,
                view_count=rng.randrange(1000),
            )
        )

    target, changes, report, _ = run_full_optimization(items)
    assert replay(items, changes) == ids(target)
    assert report.need_to_move == len(changes)
    print(f"  ✓ full pipeline: 60 items grouped with {len(changes)} moves, replay exact")


def test_within_group_sort_applied_and_exact():
    """Grouping plus in-group sorting still replays exactly."""
    rng = random.Random(31337)
    artists = ["Alpha", "Beta", "Gamma"]
    items = []
    for i in range(45):
        artist = artists[i % len(artists)]
        items.append(
            make_item(
                f"p{i}",
                title=f"{artist} - Song {i}",
                channel=artist,
                position=i,
                view_count=rng.randrange(100_000),
            )
        )

    sort_config = SortConfig(field=SortField.VIEW_COUNT, order=SortOrder.DESC)
    target, changes, _, _ = run_full_optimization(items, within_group_sort=sort_config)

    assert replay(items, changes) == ids(target)

    # Every group must be contiguous and descending by view count inside.
    seen_groups: list[str] = []
    current_group = None
    previous_views = None
    for item in target:
        if item.channel_title != current_group:
            assert item.channel_title not in seen_groups, "group is not contiguous"
            seen_groups.append(item.channel_title)
            current_group = item.channel_title
            previous_views = None
        if previous_views is not None:
            assert item.view_count <= previous_views, "in-group sort is not descending"
        previous_views = item.view_count
    print("  ✓ within-group sort: groups contiguous, view counts descending, replay exact")


# ─── Main ─────────────────────────────────────────


TESTS = [
    ("Exhaustive Correctness", [
        test_exhaustive_permutations,
        test_move_count_is_minimal,
        test_already_sorted_is_free,
    ]),
    ("Randomised Playlists", [
        test_random_large_playlists,
        test_local_shuffle_is_cheap,
    ]),
    ("Pinned Items", [
        test_pinned_items_never_move,
        test_pinned_anchor_set_stays_increasing,
    ]),
    ("Resume Safety", [
        test_resume_midway_still_lands_on_target,
        test_execution_order_is_dense_and_ordered,
    ]),
    ("Guards", [
        test_non_permutation_target_is_rejected,
    ]),
    ("Full Pipeline", [
        test_full_pipeline_replays_exactly,
        test_within_group_sort_applied_and_exact,
    ]),
]


def run_all_tests() -> bool:
    # The planner logs expected warnings (e.g. pinning items back); keep the
    # test output readable.
    logging.disable(logging.ERROR)

    total = passed = failed = 0
    for group_name, test_funcs in TESTS:
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
    sys.exit(0 if run_all_tests() else 1)
