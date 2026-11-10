"""
review_server.py — The local drag-and-drop review page (standard library only).

``yt_tool review start`` creates a run directory and launches this module as a
detached process::

    python -m scripts.review_server <run_dir>

It serves ``scripts/review/template.html`` with the run's data embedded, on
127.0.0.1 and a random port, behind a random token.  The page posts back:

- ``POST /save``   — order mode: the edited order (validated as a permutation of
  the snapshot, unavailable items pinned back); label mode: a group label per item.
- ``POST /cancel`` — the user abandoned the review; nothing will be written.

Either one writes a marker file (``saved.json`` / ``cancelled.json``) and shuts
the server down, which is what ``yt_tool review wait`` watches for.  An unused
server exits after an hour on its own.

Files in the run directory:

====================  =========================================================
``review_state.json`` token + mode (written by ``review start``, read here)
``server.json``       port / pid / ``stopped_at`` (written here)
``snapshot.json``     the fetched playlist, in current order
``proposed_order.json`` the Agent's proposal (items may carry a ``group`` key)
``edited_order.json`` what the user saved (order mode)
``labels.json``       what the user saved (label mode)
``review_log.jsonl``  one line per drag / relabel, in the order they happened
``user_labels.json``  items the user moved into a different group (order mode)
====================  =========================================================
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bisect import bisect_left

from scripts.optimizer import normalize_target
from scripts.schemas import EnrichedPlaylistItem

TEMPLATE_PATH = Path(__file__).resolve().parent / "review" / "template.html"
DATA_PLACEHOLDER = "/*__REVIEW_DATA__*/null"

STATE_FILE = "review_state.json"
SERVER_FILE = "server.json"
SNAPSHOT_FILE = "snapshot.json"
PROPOSED_FILE = "proposed_order.json"
EDITED_FILE = "edited_order.json"
LABELS_FILE = "labels.json"
LOG_FILE = "review_log.jsonl"
USER_LABELS_FILE = "user_labels.json"
SAVED_FILE = "saved.json"
CANCELLED_FILE = "cancelled.json"

IDLE_TIMEOUT_SECONDS = 3600
MAX_BODY_BYTES = 8 * 1024 * 1024

#: The page may load cover images over https and talk to this server only.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src https: data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _media_url(record: dict[str, Any]) -> str:
    """Link to the song on its platform; '' when there is nothing to open."""
    media_id = str(record.get("video_id") or "")
    if (record.get("platform") or "youtube") == "youtube":
        return f"https://www.youtube.com/watch?v={media_id}" if media_id else ""
    uri = str(record.get("uri") or "")
    if uri.startswith("spotify:track:"):
        return f"https://open.spotify.com/track/{uri.split(':')[-1]}"
    if uri.startswith("spotify:episode:"):
        return f"https://open.spotify.com/episode/{uri.split(':')[-1]}"
    return ""  # local files and unavailable items


def _safe_image(url: Any) -> str:
    text = str(url or "")
    return text if text.startswith("https://") else ""


def _page_item(record: dict[str, Any], index: int, group: str) -> dict[str, Any]:
    pinned = record.get("is_available") is False
    album = record.get("album") or None
    artists = [a.get("name", "") for a in record.get("artists") or [] if a.get("name")]
    return {
        "key": record["playlist_item_id"],
        "orig": index,
        "title": record.get("title") or "（沒有標題）",
        "artists": artists,
        "channel": record.get("channel_title") or "",
        "sub": ", ".join(artists) if artists else (record.get("channel_title") or ""),
        "group": "" if pinned else group,
        "pinned": pinned,
        "album": (
            {"name": album.get("name", ""), "type": album.get("album_type", ""),
             "total_tracks": int(album.get("total_tracks") or 0)}
            if isinstance(album, dict) and (album.get("name") or album.get("id")) else None
        ),
        "track_number": int(record.get("track_number") or 0),
        "disc_number": int(record.get("disc_number") or 0),
        "duration_seconds": int(record.get("duration_seconds") or 0),
        "published_at": record.get("published_at") or "",
        "added_at": record.get("added_at") or "",
        "view_count": int(record.get("view_count") or 0),
        "like_count": int(record.get("like_count") or 0),
        "isrc": record.get("isrc") or "",
        "image_url": _safe_image(record.get("image_url")),
        "media_url": _media_url(record),
    }


def build_page_data(run_dir: Path) -> dict[str, Any]:
    """Everything the template needs, as plain JSON."""
    state = _read_json(run_dir / STATE_FILE)
    snapshot = _read_json(run_dir / SNAPSHOT_FILE)
    proposed = _read_json(run_dir / PROPOSED_FILE)
    group_of = {r["playlist_item_id"]: r.get("group", "") for r in proposed}
    platform = (snapshot[0].get("platform") if snapshot else None) or "youtube"
    return {
        "mode": state.get("mode", "order"),
        "platform": platform,
        "playlist_id": next((r.get("playlist_id") for r in snapshot if r.get("playlist_id")), ""),
        "items": [_page_item(r, i, group_of.get(r["playlist_item_id"], "")) for i, r in enumerate(snapshot)],
        "proposed": [r["playlist_item_id"] for r in proposed],
    }


def render_page(run_dir: Path) -> bytes:
    data = json.dumps(build_page_data(run_dir), ensure_ascii=False)
    # Titles come from YouTube / Spotify: a "</script>" inside one must not end the data block.
    data = data.replace("</", "<\\/").replace("<!--", "<\\!--")
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    if DATA_PLACEHOLDER not in html:
        raise RuntimeError("review template is missing its data placeholder")
    return html.replace(DATA_PLACEHOLDER, data).encode("utf-8")


class ReviewError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def save_order(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and store an edited order.  Returns the ``saved.json`` content."""
    snapshot_raw = _read_json(run_dir / SNAPSHOT_FILE)
    snapshot = [EnrichedPlaylistItem.model_validate(r) for r in snapshot_raw]
    by_key = {item.playlist_item_id: item for item in snapshot}
    order = payload.get("order")
    if not isinstance(order, list) or sorted(map(str, order)) != sorted(by_key):
        raise ReviewError(400, "TARGET_MISMATCH", "保存的順序不是原清單的重排（有項目遺失或多出來）。")

    target = [by_key[key] for key in order]
    normalized = normalize_target(snapshot, target)
    repinned = [i.playlist_item_id for i in normalized] != list(order)

    proposed_raw = _read_json(run_dir / PROPOSED_FILE)
    proposed_groups = {r["playlist_item_id"]: r.get("group", "") for r in proposed_raw}
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
    group_of = {key: str(labels.get(key, proposed_groups.get(key, ""))) for key in by_key}

    _write_json(
        run_dir / EDITED_FILE,
        [dict(item.model_dump(mode="json"), group=group_of[item.playlist_item_id]) for item in normalized],
    )
    relabels = {
        key: {"from": proposed_groups.get(key, ""), "to": group, "title": by_key[key].title,
              "media_id": by_key[key].video_id, "platform": by_key[key].platform, "source": "user"}
        for key, group in group_of.items()
        if group != proposed_groups.get(key, "") and by_key[key].is_available
    }
    if relabels:
        _write_json(run_dir / USER_LABELS_FILE, {"saved_at": _now(), "labels": relabels})
    _write_log(run_dir, payload.get("log"))

    # Edits are measured on the state, not on the clicks: how many songs the user moved
    # relative to the proposal (movable songs − LIS of their proposal positions), plus the
    # songs whose group changed.  Moving a song away and back is 0; moving one song across
    # five others is 1.  Pins are ignored on purpose: this counts what the user did, the API
    # cost (which does honour pins) is reported separately by `review wait`.
    proposed_index = {r["playlist_item_id"]: i for i, r in enumerate(
        r for r in proposed_raw if r.get("is_available") is not False)}
    moves = _moved_count([proposed_index[i.playlist_item_id] for i in normalized if i.is_available])
    return {"mode": "order", "saved_at": _now(), "edits": moves + len(relabels),
            "moves": moves, "relabels": len(relabels), "repinned": repinned}


def _moved_count(seq: list[int]) -> int:
    """len(seq) − length of its longest increasing subsequence."""
    tails: list[int] = []
    for value in seq:
        slot = bisect_left(tails, value)
        if slot == len(tails):
            tails.append(value)
        else:
            tails[slot] = value
    return len(seq) - len(tails)


def save_labels(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Store a group label per item (label mode, for building ground truth)."""
    snapshot = _read_json(run_dir / SNAPSHOT_FILE)
    keys = {r["playlist_item_id"] for r in snapshot if r.get("is_available") is not False}
    labels = payload.get("labels")
    if not isinstance(labels, dict) or not set(labels) <= {r["playlist_item_id"] for r in snapshot}:
        raise ReviewError(400, "LABELS_INVALID", "標籤裡有不屬於這份清單的項目。")
    clean = {key: str(labels[key]) for key in sorted(keys) if key in labels}
    _write_json(run_dir / LABELS_FILE, {"saved_at": _now(), "labels": clean})
    _write_log(run_dir, payload.get("log"))
    proposed_groups = {r["playlist_item_id"]: r.get("group", "") for r in _read_json(run_dir / PROPOSED_FILE)}
    changed = sum(1 for key, group in clean.items() if group != proposed_groups.get(key, ""))
    return {"mode": "label", "saved_at": _now(), "edits": changed,
            "labeled": len(clean), "unlabeled": len(keys) - len(clean)}


def _write_log(run_dir: Path, log: Any) -> None:
    lines = [json.dumps(entry, ensure_ascii=False) for entry in (log if isinstance(log, list) else [])
             if isinstance(entry, dict)]
    (run_dir / LOG_FILE).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        state = _read_json(run_dir / STATE_FILE)
        self.token = state["token"]
        self.mode = state.get("mode", "order")
        self.last_activity = time.time()
        self.finished = threading.Event()
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def finish(self) -> None:
        """Stop serving once the current response has been sent."""
        self.finished.set()
        threading.Thread(target=self.shutdown, daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, *_args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_type == "text/html":
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _authorized(self) -> bool:
        self.server.last_activity = time.time()
        # Same-host checks: a DNS-rebinding page cannot present this Host header,
        # and nobody without the URL has the token.
        host = (self.headers.get("Host") or "").lower()
        if host not in {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}:
            self._json(403, {"status": "error", "code": "FORBIDDEN", "message": "bad host"})
            return False
        token = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        if token != self.server.token:
            self._json(403, {"status": "error", "code": "FORBIDDEN", "message": "bad token"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 — http.server naming
        if urlparse(self.path).path != "/":
            self._send(404, b"{}")
            return
        if not self._authorized():
            return
        self._send(200, render_page(self.server.run_dir), "text/html")

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route not in {"/save", "/cancel"}:
            self._send(404, b"{}")
            return
        if not self._authorized():
            return
        if self.server.finished.is_set():
            self._json(409, {"status": "error", "code": "ALREADY_CLOSED", "message": "這次檢查已經結束了。"})
            return

        run_dir = self.server.run_dir
        if route == "/cancel":
            _write_json(run_dir / CANCELLED_FILE, {"cancelled_at": _now()})
            self._json(200, {"status": "cancelled"})
            self.server.finish()
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"status": "error", "code": "BAD_BODY", "message": "empty or too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            saver = save_labels if self.server.mode == "label" else save_order
            saved = saver(run_dir, payload)
        except ReviewError as exc:
            self._json(exc.status, {"status": "error", "code": exc.code, "message": exc.message})
            return
        except (ValueError, KeyError) as exc:
            self._json(400, {"status": "error", "code": "BAD_BODY", "message": str(exc)})
            return
        _write_json(run_dir / SAVED_FILE, saved)
        self._json(200, {"status": "saved", **saved})
        self.server.finish()


def _watch_idle(server: ReviewServer) -> None:
    while not server.finished.wait(30):
        if time.time() - server.last_activity > IDLE_TIMEOUT_SECONDS:
            server.finish()


def serve(run_dir: Path) -> None:
    """Run until saved, cancelled or idle; record the port and the stop time."""
    server = ReviewServer(run_dir)
    server_file = run_dir / SERVER_FILE
    _write_json(server_file, {"port": server.port, "pid": os.getpid(), "started_at": _now()})
    threading.Thread(target=_watch_idle, args=(server,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        info = _read_json(server_file)
        info["stopped_at"] = _now()
        _write_json(server_file, info)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m scripts.review_server <run_dir>", file=sys.stderr)
        sys.exit(2)
    serve(Path(sys.argv[1]))
