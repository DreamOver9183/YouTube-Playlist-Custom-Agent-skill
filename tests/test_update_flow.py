"""
test_update_flow.py — Write-back safety tests for ``yt_tool update``.

These run entirely offline against a fake YouTube client that reproduces the
API's reorder semantics (remove + insert), and cover the failure modes that
previously turned a "successful" run into a scrambled playlist:

1. Happy path — the remote playlist ends up exactly in the target order.
2. Staleness — a playlist changed underneath the plan is refused (1 unit).
3. Abort + resume — a mid-plan failure stops immediately and resumes exactly.
4. Progress pollution — a leftover resume file from another plan is ignored.
5. Legacy change sets — v1 files (tail-first, static positions) are refused.
6. Transient errors — rate limits are retried, quota exhaustion aborts.
"""

import argparse
import io
import json
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from googleapiclient.errors import HttpError

from scripts import yt_tool
from scripts.optimizer import plan_reorder
from scripts.schemas import EnrichedPlaylistItem

PLAYLIST_ID = "PLtestflow"


# ─── Fakes ────────────────────────────────────────


def http_error(status: int, reason: str) -> HttpError:
    resp = types.SimpleNamespace(status=status, reason=reason)
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode("utf-8")
    return HttpError(resp, content)


class FakeYouTubeClient:
    """Reproduces playlistItems.update semantics on an in-memory playlist."""

    def __init__(self, item_ids, failures=None):
        self.live = list(item_ids)
        #: {move_index: [exc, exc, ...]} — one entry consumed per attempt at
        #: that move, so a move can fail twice and then succeed.
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls = 0     # every attempt, including failed ones
        self.applied = 0   # successful moves
        self.head_reads = 0

    def get_playlist_head(self, playlist_id, max_results=50):
        self.head_reads += 1
        return self.live[:max_results]

    def update_item_position(self, playlist_item_id, playlist_id, video_id, new_position):
        self.calls += 1
        queued = self.failures.get(self.applied)
        if queued:
            raise queued.pop(0)
        self.live.remove(playlist_item_id)
        self.live.insert(new_position, playlist_item_id)
        self.applied += 1


def make_items(n: int) -> list[EnrichedPlaylistItem]:
    return [
        EnrichedPlaylistItem(
            playlist_item_id=f"p{i}",
            video_id=f"v{i}",
            position=i,
            added_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            playlist_id=PLAYLIST_ID,
            title=f"Song {i}",
            channel_title="Chan",
        )
        for i in range(n)
    ]


def run_update(tmp_dir: Path, client: FakeYouTubeClient, changes_file: Path) -> dict:
    """Invoke cmd_update with a fake client and return the final JSON payload."""
    yt_tool.get_authenticated_client = lambda _path: client  # type: ignore[assignment]
    yt_tool.ensure_credentials = lambda _path: None  # type: ignore[assignment]
    yt_tool.time.sleep = lambda _s: None  # type: ignore[assignment]

    args = argparse.Namespace(
        playlist=PLAYLIST_ID,
        changes=str(changes_file),
        credentials=None,
        skip_verify=False,
    )
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            yt_tool.cmd_update(args)
    except SystemExit:
        pass

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    return json.loads(lines[-1])


def write_plan(tmp_dir: Path, current, target, name="changes.json"):
    changes, _report = plan_reorder(current, target)
    path = tmp_dir / name
    fingerprint = yt_tool.write_change_set(path, PLAYLIST_ID, current, changes)
    return path, changes, fingerprint


def reset_state():
    yt_tool.clear_progress(PLAYLIST_ID)


# ─── Tests ────────────────────────────────────────


def test_happy_path_reaches_target(tmp_dir: Path):
    reset_state()
    current = make_items(12)
    target = list(reversed(current))
    path, changes, _ = write_plan(tmp_dir, current, target)

    client = FakeYouTubeClient([i.playlist_item_id for i in current])
    result = run_update(tmp_dir, client, path)

    assert result["status"] == "success", result
    assert client.live == [i.playlist_item_id for i in target], client.live
    assert result["quota_used"] == 1 + 50 * len(changes)
    assert client.head_reads == 1, "the staleness check must cost exactly one read"
    assert not yt_tool.progress_path(PLAYLIST_ID).is_file(), "progress file must be cleared"
    print(f"  ✓ happy path: {len(changes)} moves, playlist matches target exactly")


def test_stale_playlist_is_refused(tmp_dir: Path):
    reset_state()
    current = make_items(10)
    target = list(reversed(current))
    path, _, _ = write_plan(tmp_dir, current, target, name="changes_stale.json")

    # Someone reordered the playlist on YouTube after we fetched it.
    scrambled = [i.playlist_item_id for i in current]
    scrambled.insert(0, scrambled.pop(7))
    client = FakeYouTubeClient(scrambled)

    result = run_update(tmp_dir, client, path)

    assert result["code"] == "STALE_SNAPSHOT", result
    assert client.calls == 0, "no writes may happen against a stale playlist"
    print("  ✓ staleness: drifted playlist refused before spending any write quota")


def test_abort_then_resume(tmp_dir: Path):
    reset_state()
    current = make_items(14)
    target = list(reversed(current))
    path, changes, fingerprint = write_plan(tmp_dir, current, target, name="changes_resume.json")

    # Fail on the 4th move with a non-retryable error.
    client = FakeYouTubeClient(
        [i.playlist_item_id for i in current],
        failures={3: [http_error(404, "playlistItemNotFound")]},
    )
    first = run_update(tmp_dir, client, path)

    assert first["status"] == "partial", first
    assert first["code"] == "ITEM_NOT_FOUND"
    assert first["resume_from"] == 3
    assert client.calls == 4, "must stop at the failing move, not keep going"
    assert yt_tool.load_progress(PLAYLIST_ID, fingerprint) == 3

    # Retry with a healthy client; the resume must verify against the
    # *partially applied* state and finish the remaining moves.
    resumed_client = FakeYouTubeClient(client.live)
    second = run_update(tmp_dir, resumed_client, path)

    assert second["status"] == "success", second
    assert second["successful"] == len(changes)
    assert resumed_client.calls == len(changes) - 3
    assert resumed_client.live == [i.playlist_item_id for i in target]
    assert not yt_tool.progress_path(PLAYLIST_ID).is_file()
    print("  ✓ abort + resume: stopped at the failure, resumed exactly, target reached")


def test_stale_progress_file_is_ignored(tmp_dir: Path):
    reset_state()
    current = make_items(9)
    target = list(reversed(current))
    path, changes, _ = write_plan(tmp_dir, current, target, name="changes_a.json")

    # A leftover resume file from a completely different plan.
    yt_tool.save_progress(PLAYLIST_ID, "deadbeefdeadbeef", 7)

    client = FakeYouTubeClient([i.playlist_item_id for i in current])
    result = run_update(tmp_dir, client, path)

    assert result["status"] == "success", result
    assert client.calls == len(changes), "a foreign progress file must not skip moves"
    assert client.live == [i.playlist_item_id for i in target]
    print("  ✓ progress pollution: foreign resume file ignored, every move applied")


def test_legacy_change_set_is_refused(tmp_dir: Path):
    reset_state()
    current = make_items(6)
    target = list(reversed(current))
    changes, _ = plan_reorder(current, target)

    legacy = tmp_dir / "legacy.json"
    legacy.write_text(
        json.dumps([c.model_dump(mode="json") for c in changes], ensure_ascii=False),
        encoding="utf-8",
    )

    client = FakeYouTubeClient([i.playlist_item_id for i in current])
    result = run_update(tmp_dir, client, legacy)

    assert result["code"] == "LEGACY_CHANGE_SET", result
    assert client.calls == 0
    print("  ✓ legacy format: v1 change sets refused instead of executed")


def test_rate_limit_is_retried(tmp_dir: Path):
    reset_state()
    current = make_items(8)
    target = list(reversed(current))
    path, changes, _ = write_plan(tmp_dir, current, target, name="changes_retry.json")

    client = FakeYouTubeClient(
        [i.playlist_item_id for i in current],
        failures={2: [http_error(503, "backendError"), http_error(429, "rateLimitExceeded")]},
    )
    result = run_update(tmp_dir, client, path)

    assert result["status"] == "success", result
    assert client.live == [i.playlist_item_id for i in target]
    assert client.calls == len(changes) + 2, "the two transient errors must be retried"
    print("  ✓ transient errors: 5xx and 429 retried, plan still completed")


def test_quota_exhaustion_aborts_immediately(tmp_dir: Path):
    reset_state()
    current = make_items(20)
    target = list(reversed(current))
    path, _, _ = write_plan(tmp_dir, current, target, name="changes_quota.json")

    client = FakeYouTubeClient(
        [i.playlist_item_id for i in current],
        failures={2: [http_error(403, "quotaExceeded")]},
    )
    result = run_update(tmp_dir, client, path)

    assert result["status"] == "partial", result
    assert result["code"] == "QUOTA_EXCEEDED"
    assert client.calls == 3, "must not keep hammering the API after quota is gone"
    print("  ✓ quota exhaustion: aborts at once instead of burning the remaining calls")


# ─── Main ─────────────────────────────────────────


TESTS = [
    test_happy_path_reaches_target,
    test_stale_playlist_is_refused,
    test_abort_then_resume,
    test_stale_progress_file_is_ignored,
    test_legacy_change_set_is_refused,
    test_rate_limit_is_retried,
    test_quota_exhaustion_aborts_immediately,
]


def run_all_tests() -> bool:
    import logging
    import tempfile

    logging.disable(logging.ERROR)
    passed = failed = 0

    print(f"\n{'='*60}")
    print(" Update Write-Back Safety")
    print(f"{'='*60}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for test_func in TESTS:
            try:
                test_func(tmp_dir)
                passed += 1
            except Exception as exc:
                failed += 1
                print(f"  ✗ {test_func.__name__}: {exc}")

    reset_state()
    print(f"\n{'='*60}")
    print(f" Results: {passed}/{passed + failed} passed, {failed} failed")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all_tests() else 1)
