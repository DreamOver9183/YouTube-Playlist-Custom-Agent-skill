"""
test_review_server.py — The drag-and-drop review page, tested over real HTTP (no browser).

What a user's browser can send is exactly what these tests send, so the guards
that matter are checked here:

1. The page needs the token and a loopback Host header; a title containing
   ``</script>`` cannot break out of the embedded data.
2. A saved order that is not a permutation is refused (``TARGET_MISMATCH``) and
   the server keeps running; a valid one is stored with unavailable items
   pinned back, the drag log and relabels are kept, and the server stops.
3. ``review wait`` turns a save into a change set whose replay is the saved
   order, plus the one-line cost; a cancel makes it return at once; with
   nobody saving it returns ``waiting``.
4. Label mode stores labels and refuses keys that are not in the playlist.
5. The page gets every field we fetched (artists, album, cover, links), cover URLs
   are https-only, the page is served with a strict CSP, and the edit count is
   measured on the state (moves from the proposal + regrouped songs), not on clicks.
"""

import json
import logging
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import review_server as rs  # noqa: E402
from scripts import yt_tool  # noqa: E402
from scripts.schemas import EnrichedPlaylistItem, PlaylistItemData  # noqa: E402
from scripts.youtube_api import YouTubeClient  # noqa: E402

EVIL_TITLE = "Song </script><script>window.pwned=1</script>"


def make_items() -> list[dict]:
    artists = ["A", "B", "A", "C", "B", "A", "C", "B"]
    items = []
    for i, artist in enumerate(artists):
        items.append({
            "playlist_item_id": f"p{i}", "video_id": f"v{i}", "position": i,
            "added_at": "2026-01-01T00:00:00Z", "playlist_id": "PLreview",
            "title": EVIL_TITLE if i == 1 else f"{artist} - Song {i}", "channel_title": artist,
            "is_available": i != 3,  # p3 is a private video: pinned at index 3
        })
    return items


def make_run(root: Path, mode: str = "order") -> tuple[Path, list[dict], list[dict]]:
    run_dir = root / f"run_{secrets.token_hex(3)}"
    run_dir.mkdir()
    snapshot = make_items()
    group = {r["playlist_item_id"]: r["channel_title"] for r in snapshot if r["is_available"]}
    movable = sorted((r for r in snapshot if r["is_available"]), key=lambda r: ("ABC".index(group[r["playlist_item_id"]]), r["position"]))
    proposed = [dict(r, group=group[r["playlist_item_id"]]) for r in movable]
    proposed.insert(3, dict(snapshot[3], group=""))
    (run_dir / rs.SNAPSHOT_FILE).write_text(json.dumps(snapshot), encoding="utf-8")
    (run_dir / rs.PROPOSED_FILE).write_text(json.dumps(proposed), encoding="utf-8")
    (run_dir / rs.STATE_FILE).write_text(json.dumps({"token": "tok123", "mode": mode}), encoding="utf-8")
    return run_dir, snapshot, proposed


class Running:
    """``rs.serve`` in a thread, exactly as the detached process runs it."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.thread = threading.Thread(target=rs.serve, args=(run_dir,), daemon=True)
        self.thread.start()
        for _ in range(100):
            if (run_dir / rs.SERVER_FILE).is_file():
                break
            time.sleep(0.05)
        self.port = json.loads((run_dir / rs.SERVER_FILE).read_text(encoding="utf-8"))["port"]

    def call(self, method: str, path: str, body=None, token: str = "tok123", host: str | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}?t={token}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def stopped(self, timeout: float = 5) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()


def replay(ids: list[str], change_set: dict) -> list[str]:
    """YouTube semantics (remove, then insert at new_position), from the API contract."""
    live = list(ids)
    for change in sorted(change_set["changes"], key=lambda c: c["execution_order"]):
        live.remove(change["playlist_item_id"])
        live.insert(change["new_position"], change["playlist_item_id"])
    return live


# ─── tests ─────────────────────────────────────────


def test_page_guards():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir, snapshot, _ = make_run(Path(tmp))
        server = Running(run_dir)
        try:
            assert server.call("GET", "/", token="nope")[0] == 403
            assert server.call("GET", "/", host=f"evil.example:{server.port}")[0] == 403
            status, html = server.call("GET", "/")
            assert status == 200
            assert "window.pwned=1</script>" not in html, "a title broke out of the data block"
            raw = html.split('id="review-data" type="application/json">', 1)[1].split("</script>", 1)[0]
            data = json.loads(raw)
            assert [i["key"] for i in data["items"]] == [r["playlist_item_id"] for r in snapshot]
            assert data["items"][1]["title"] == EVIL_TITLE and data["items"][3]["pinned"] is True
            assert data["platform"] == "youtube" and data["mode"] == "order"
        finally:
            server.call("POST", "/cancel", {})
            assert server.stopped()
    print("  ✓ page: token + loopback Host required; '</script>' in a title stays inert")


def test_save_order():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir, snapshot, proposed = make_run(Path(tmp))
        server = Running(run_dir)
        keys = [r["playlist_item_id"] for r in proposed]

        status, body = server.call("POST", "/save", {"order": keys[:-1]})
        assert status == 400 and json.loads(body)["code"] == "TARGET_MISMATCH"
        assert not (run_dir / rs.SAVED_FILE).exists() and server.thread.is_alive()

        # The browser moves p7 to the very front and drops the private p3 at the end:
        # p3 must come back to index 3; p7 is relabelled from B to A.
        edited = ["p7"] + [k for k in keys if k not in ("p7", "p3")] + ["p3"]
        labels = {r["playlist_item_id"]: r["group"] for r in proposed}
        labels["p7"] = "A"
        log = [{"type": "item", "keys": ["p0"]}, {"type": "revert"},
               {"type": "item", "keys": ["p7"], "from_group": "B", "to_group": "A"}]
        status, body = server.call("POST", "/save", {"order": edited, "labels": labels, "log": log})
        saved = json.loads(body)
        assert status == 200 and saved["relabels"] == 1, body
        # Edits are counted on the state: p7 moved (1) + p7 regrouped (1); the revert in the log is history only.
        assert saved["edits"] == 2 and saved["moves"] == 1, f"edits should be 1 move + 1 regroup: {saved}"
        assert server.stopped(), "server should stop after a save"
        assert json.loads((run_dir / rs.SERVER_FILE).read_text(encoding="utf-8")).get("stopped_at")

        stored = json.loads((run_dir / rs.EDITED_FILE).read_text(encoding="utf-8"))
        stored_ids = [r["playlist_item_id"] for r in stored]
        assert stored_ids[3] == "p3", f"pinned item not restored: {stored_ids}"
        assert stored_ids[0] == "p7" and stored[0]["group"] == "A"
        assert json.loads((run_dir / rs.SAVED_FILE).read_text(encoding="utf-8"))["repinned"] is True
        user_labels = json.loads((run_dir / rs.USER_LABELS_FILE).read_text(encoding="utf-8"))["labels"]
        assert user_labels["p7"]["from"] == "B" and user_labels["p7"]["to"] == "A"
        assert len((run_dir / rs.LOG_FILE).read_text(encoding="utf-8").splitlines()) == 3, "the log keeps reverts too"

        summary = yt_tool.wait_for_review(run_dir, 1)
        assert summary["status"] == "saved" and summary["mode"] == "order"
        change_set = json.loads(Path(summary["changes_file"]).read_text(encoding="utf-8"))
        assert replay([r["playlist_item_id"] for r in snapshot], change_set) == stored_ids
        assert summary["need_to_move"] == len(change_set["changes"])
        assert summary["estimated_quota"] == 1 + 50 * summary["need_to_move"]
        assert "units" in summary["summary_line"] and "要寫回嗎" in summary["summary_line"]
    print("  ✓ save: non-permutation refused; pinned restored; relabel + log kept; change set replays exactly")


def test_cancel_and_waiting():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir, _, _ = make_run(Path(tmp))
        server = Running(run_dir)
        started = time.time()
        assert yt_tool.wait_for_review(run_dir, 0.4)["status"] == "waiting"
        assert time.time() - started < 2
        assert server.call("POST", "/cancel", {})[0] == 200
        started = time.time()
        result = yt_tool.wait_for_review(run_dir, 30)
        assert result["status"] == "cancelled" and time.time() - started < 2, result
        assert server.stopped()
        assert not (run_dir / rs.EDITED_FILE).exists()
    print("  ✓ wait: 'waiting' on timeout; cancel returns 'cancelled' at once and writes nothing")


def test_label_mode():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir, snapshot, _ = make_run(Path(tmp), mode="label")
        server = Running(run_dir)
        status, body = server.call("POST", "/save", {"labels": {"p0": "A", "ghost": "X"}})
        assert status == 400 and json.loads(body)["code"] == "LABELS_INVALID"
        labels = {r["playlist_item_id"]: r["channel_title"] for r in snapshot if r["is_available"]}
        status, _ = server.call("POST", "/save", {"labels": labels, "order": [r["playlist_item_id"] for r in snapshot]})
        assert status == 200 and server.stopped()
        stored = json.loads((run_dir / rs.LABELS_FILE).read_text(encoding="utf-8"))["labels"]
        assert stored == labels and "p3" not in stored
        assert not (run_dir / rs.EDITED_FILE).exists(), "label mode must not produce an order"
        summary = yt_tool.wait_for_review(run_dir, 1)
        assert summary["status"] == "saved" and summary["mode"] == "label" and summary["labeled"] == 7
    print("  ✓ label mode: unknown keys refused; labels stored; no order or change set produced")


def test_page_data_and_csp():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        base = {"position": 0, "added_at": "2026-01-02T00:00:00Z", "playlist_id": "PL", "platform": "spotify"}
        snapshot = [
            dict(base, playlist_item_id="spotify:track:t1#0", video_id="t1", uri="spotify:track:t1", title="Song",
                 artists=[{"name": "A", "id": "a"}, {"name": "B", "id": "b"}], duration_seconds=7200,
                 album={"id": "al", "name": "Album", "album_type": "single", "total_tracks": 3}, track_number=2,
                 image_url="https://i.scdn.co/image/x"),
            dict(base, playlist_item_id="spotify:episode:e1#0", video_id="e1", uri="spotify:episode:e1",
                 title="Ep", image_url="javascript:alert(1)"),
            dict(base, playlist_item_id="spotify:local:x#0", video_id="spotify:local:x", uri="spotify:local:x",
                 title="Local", image_url="http://insecure.example/x.jpg"),
        ]
        (run_dir / rs.SNAPSHOT_FILE).write_text(json.dumps(snapshot), encoding="utf-8")
        (run_dir / rs.PROPOSED_FILE).write_text(json.dumps(snapshot), encoding="utf-8")
        (run_dir / rs.STATE_FILE).write_text(json.dumps({"token": "tok123", "mode": "order"}), encoding="utf-8")
        items = rs.build_page_data(run_dir)["items"]
        first = items[0]
        for field in ("artists", "album", "duration_seconds", "added_at", "image_url", "media_url", "orig"):
            assert field in first, f"page data lacks {field}"
        assert first["artists"] == ["A", "B"]
        assert first["album"] == {"name": "Album", "type": "single", "total_tracks": 3}
        assert [i["media_url"] for i in items] == [
            "https://open.spotify.com/track/t1", "https://open.spotify.com/episode/e1", ""]
        assert [i["image_url"] for i in items] == ["https://i.scdn.co/image/x", "", ""], "non-https covers must be dropped"

        yt = {"playlist_item_id": "y1", "video_id": "abc", "position": 0, "added_at": "2026-01-01T00:00:00Z"}
        assert rs._media_url(yt) == "https://www.youtube.com/watch?v=abc"

        server = Running(run_dir)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{server.port}/?t=tok123")
            with urllib.request.urlopen(req, timeout=5) as resp:
                csp = resp.headers.get("Content-Security-Policy", "")
            assert csp == rs.CONTENT_SECURITY_POLICY
            assert "img-src https: data:" in csp and "connect-src 'self'" in csp
        finally:
            server.call("POST", "/cancel", {})
            assert server.stopped()
    print("  ✓ page data: artists/album/cover/links; covers https-only; media_url per platform; CSP header sent")


def test_edit_count_is_state_based():
    def saved_edits(order_fn, labels_fn=None) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _snapshot, proposed = make_run(Path(tmp))
            keys = [r["playlist_item_id"] for r in proposed]
            labels = {r["playlist_item_id"]: r["group"] for r in proposed}
            if labels_fn:
                labels_fn(labels)
            return rs.save_order(run_dir, {"order": order_fn(list(keys)), "labels": labels})["edits"]

    def move(keys, key, steps):
        i = keys.index(key)
        keys.insert(i + steps, keys.pop(i))
        return keys

    def regroup(labels):
        labels["p0"] = "Z"

    assert saved_edits(lambda k: move(move(k, "p0", 5), "p0", -5)) == 0, "moved away and back"
    assert saved_edits(lambda k: k, regroup) == 1, "one regroup"
    assert saved_edits(lambda k: move(k, "p0", 6)) == 1, "one book across five others"
    assert saved_edits(lambda k: move(k, "p0", 6), regroup) == 2, "one move + one regroup"
    assert saved_edits(lambda k: move(move(k, "p0", 6), "p6", -6)) == 2, "two books moved"
    print("  ✓ edits = moves from the proposal + regrouped songs (0 / 1 / 1 / 2 / 2)")


def test_cover_sources():
    raw = {"id": "v1", "snippet": {"title": "t", "thumbnails": {
        "default": {"url": "https://i.ytimg.com/d.jpg"}, "medium": {"url": "https://i.ytimg.com/m.jpg"}}}}
    assert YouTubeClient._parse_video_metadata(raw).image_url == "https://i.ytimg.com/m.jpg"
    raw["snippet"]["thumbnails"].pop("medium")
    meta = YouTubeClient._parse_video_metadata(raw)
    assert meta.image_url == "https://i.ytimg.com/d.jpg"
    raw["snippet"].pop("thumbnails")
    assert YouTubeClient._parse_video_metadata(raw).image_url == ""
    item = PlaylistItemData(playlist_item_id="p", video_id="v1", position=0, added_at="2026-01-01T00:00:00Z")
    assert EnrichedPlaylistItem.from_item_and_metadata(item, meta).image_url == "https://i.ytimg.com/d.jpg"
    legacy = {"playlist_item_id": "p", "video_id": "v", "position": 0, "added_at": "2026-01-01T00:00:00Z"}
    assert EnrichedPlaylistItem.model_validate(legacy).image_url == "", "old snapshots must still load"
    print("  ✓ YouTube thumbnails medium → default → none; carried into items; old JSON loads")


TESTS = [test_page_guards, test_save_order, test_cancel_and_waiting, test_label_mode,
         test_page_data_and_csp, test_edit_count_is_state_based, test_cover_sources]


def run_all_tests() -> bool:
    logging.disable(logging.ERROR)
    passed = failed = 0
    print(f"\n{'=' * 60}\n Review page server\n{'=' * 60}")
    for test in TESTS:
        try:
            test()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{'=' * 60}\n Results: {passed}/{passed + failed} passed, {failed} failed\n{'=' * 60}")
    return failed == 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(0 if run_all_tests() else 1)
