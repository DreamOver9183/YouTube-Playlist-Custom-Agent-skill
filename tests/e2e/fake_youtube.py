"""
fake_youtube.py — An offline stand-in for the YouTube Data API v3.

The end-to-end suite drives the *real* CLI in real subprocesses; only the
network boundary is replaced.  Everything below the boundary
(``scripts/youtube_api.py`` parsing, pagination, batching, error
classification, retry/resume logic) runs untouched.

What is reproduced faithfully:

* ``playlistItems.list``  — 50 items per page with ``nextPageToken``; 1 unit.
* ``videos.list``         — max 50 ids per call, private videos omitted from
                            the response (the real API hides them); 1 unit.
* ``playlistItems.update``— *remove then insert* semantics, which re-indexes
                            every item after the target; 50 units.
* ``HttpError``           — real ``googleapiclient`` exceptions carrying the
                            same JSON error payload Google sends, so
                            ``classify_http_error`` is exercised for real.

State (playlist order, video metadata, quota, call log, scripted faults) lives
in the JSON file named by ``$YT_E2E_STATE`` and is re-read/rewritten on every
call, so it survives across the many subprocesses one scenario spawns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Quota costs per the YouTube Data API v3 published table.
COST_LIST = 1
COST_UPDATE = 50

PAGE_SIZE = 50
VIDEOS_BATCH_LIMIT = 50


# ─── State file ──────────────────────────────────────────────────


def state_path() -> Path:
    raw = os.environ.get("YT_E2E_STATE")
    if not raw:
        raise RuntimeError("YT_E2E_STATE is not set")
    return Path(raw)


def load_state() -> dict[str, Any]:
    return json.loads(state_path().read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _record(state: dict[str, Any], op: str, cost: int, **extra: Any) -> None:
    state["quota_used"] = state.get("quota_used", 0) + cost
    state.setdefault("calls", []).append({"op": op, "cost": cost, **extra})


# ─── Errors ──────────────────────────────────────────────────────


class _Resp:
    """Minimal httplib2-style response object for HttpError."""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


def http_error(status: int, reason: str, message: str = "") -> Exception:
    from googleapiclient.errors import HttpError

    payload = {
        "error": {
            "code": status,
            "message": message or reason,
            "errors": [{"reason": reason, "message": message or reason}],
        }
    }
    return HttpError(
        _Resp(status, reason), json.dumps(payload).encode("utf-8"), uri="https://e2e.local"
    )


def _pop_fault(state: dict[str, Any], op: str, applied: int = 0,
               playlist_id: str = "") -> Exception | None:
    """Consume one scripted fault for *op* if one is armed for this call.

    A fault entry looks like::

        {"op": "playlistItems.update", "when_applied": 3,
         "status": 403, "reason": "quotaExceeded", "remaining": 1}

    ``when_applied`` matches the number of *successful* updates so far, so a
    fault fires deterministically at a known point in the plan regardless of
    how many retry attempts happen.
    """
    for fault in state.get("faults", []):
        if fault.get("op") != op or fault.get("remaining", 0) <= 0:
            continue
        scoped = fault.get("playlist_id")
        if scoped and playlist_id and scoped != playlist_id:
            continue
        when = fault.get("when_applied")
        if when is not None and when != applied:
            continue
        fault["remaining"] -= 1
        return http_error(
            int(fault.get("status", 500)),
            str(fault.get("reason", "backendError")),
            str(fault.get("message", "")),
        )
    return None


# ─── Request / resource objects ──────────────────────────────────


class _Request:
    def __init__(self, fn) -> None:
        self._fn = fn

    def execute(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._fn()


class _PlaylistItems:
    def list(
        self,
        part: str = "",
        playlistId: str = "",  # noqa: N803 — mirrors the Google client signature
        maxResults: int = PAGE_SIZE,  # noqa: N803
        pageToken: str | None = None,  # noqa: N803
        **_kwargs: Any,
    ) -> _Request:
        def run() -> dict[str, Any]:
            state = load_state()
            fault = _pop_fault(state, "playlistItems.list", 0, playlistId)
            if fault is not None:
                save_state(state)
                raise fault

            playlist = state["playlists"].get(playlistId)
            if playlist is None:
                _record(state, "playlistItems.list", COST_LIST, playlist=playlistId,
                        error="playlistNotFound")
                save_state(state)
                raise http_error(404, "playlistNotFound", f"Playlist {playlistId} not found")

            start = int(pageToken) if pageToken else 0
            size = max(1, min(int(maxResults or PAGE_SIZE), PAGE_SIZE))
            page = playlist[start : start + size]

            items = [
                {
                    "kind": "youtube#playlistItem",
                    "id": entry["playlist_item_id"],
                    "snippet": {
                        "playlistId": playlistId,
                        "position": start + offset,
                        "publishedAt": entry.get("added_at", "2024-01-01T00:00:00Z"),
                        "videoOwnerChannelTitle": entry.get("channel_title", ""),
                        "title": state["videos"].get(entry["video_id"], {}).get("title", ""),
                        "resourceId": {"kind": "youtube#video", "videoId": entry["video_id"]},
                    },
                    "status": {"privacyStatus": entry.get("privacy_status", "public")},
                }
                for offset, entry in enumerate(page)
            ]

            response: dict[str, Any] = {"kind": "youtube#playlistItemListResponse", "items": items}
            next_start = start + len(page)
            if next_start < len(playlist):
                response["nextPageToken"] = str(next_start)

            _record(state, "playlistItems.list", COST_LIST, playlist=playlistId,
                    part=part, page_start=start, returned=len(items))
            save_state(state)
            return response

        return _Request(run)

    def update(self, part: str = "", body: dict[str, Any] | None = None, **_kwargs: Any) -> _Request:
        def run() -> dict[str, Any]:
            state = load_state()
            payload = body or {}
            item_id = payload.get("id", "")
            snippet = payload.get("snippet", {})
            playlist_id = snippet.get("playlistId", "")
            position = int(snippet.get("position", 0))
            video_id = snippet.get("resourceId", {}).get("videoId", "")

            # Faults are armed per playlist, so a scenario can say "fail on the
            # 4th move of *this* plan" without knowing what ran before it.
            counters = state.setdefault("applied_updates", {})
            applied = int(counters.get(playlist_id, 0))

            fault = _pop_fault(state, "playlistItems.update", applied, playlist_id)
            if fault is not None:
                # A rejected write costs no quota; log the attempt so the suite
                # can tell a retry apart from a fresh call.
                state.setdefault("calls", []).append(
                    {"op": "playlistItems.update", "cost": 0, "playlist": playlist_id,
                     "item": item_id, "rejected": True}
                )
                save_state(state)
                raise fault

            playlist = state["playlists"].get(playlist_id)
            if playlist is None:
                _record(state, "playlistItems.update", COST_UPDATE, error="playlistNotFound")
                save_state(state)
                raise http_error(404, "playlistNotFound", f"Playlist {playlist_id} not found")

            index = next(
                (i for i, e in enumerate(playlist) if e["playlist_item_id"] == item_id), None
            )
            if index is None:
                _record(state, "playlistItems.update", COST_UPDATE, error="playlistItemNotFound")
                save_state(state)
                raise http_error(404, "playlistItemNotFound", f"Item {item_id} not found")

            if video_id and playlist[index]["video_id"] != video_id:
                _record(state, "playlistItems.update", COST_UPDATE, error="videoNotFound")
                save_state(state)
                raise http_error(400, "invalidSnippet", "resourceId does not match the item")

            entry = playlist.pop(index)
            playlist.insert(max(0, min(position, len(playlist))), entry)
            counters[playlist_id] = applied + 1

            _record(state, "playlistItems.update", COST_UPDATE, playlist=playlist_id,
                    item=item_id, position=position)
            save_state(state)
            return {"kind": "youtube#playlistItem", "id": item_id}

        return _Request(run)

    def delete(self, id: str = "", **_kwargs: Any) -> _Request:  # noqa: A002
        def run() -> dict[str, Any]:
            state = load_state()
            for playlist in state["playlists"].values():
                for i, entry in enumerate(playlist):
                    if entry["playlist_item_id"] == id:
                        playlist.pop(i)
                        _record(state, "playlistItems.delete", COST_UPDATE, item=id)
                        save_state(state)
                        return {}
            _record(state, "playlistItems.delete", COST_UPDATE, error="playlistItemNotFound")
            save_state(state)
            raise http_error(404, "playlistItemNotFound", f"Item {id} not found")

        return _Request(run)


class _Videos:
    def list(self, part: str = "", id: str = "", **_kwargs: Any) -> _Request:  # noqa: A002
        def run() -> dict[str, Any]:
            state = load_state()
            requested = [v for v in id.split(",") if v]
            if len(requested) > VIDEOS_BATCH_LIMIT:
                _record(state, "videos.list", COST_LIST, error="tooManyIds")
                save_state(state)
                raise http_error(400, "invalidParameter", "Too many ids in one videos.list call")

            items = []
            for video_id in requested:
                meta = state["videos"].get(video_id)
                # The real API does not return private videos to a third party.
                if not meta or meta.get("privacy_status") == "private":
                    continue
                items.append(
                    {
                        "kind": "youtube#video",
                        "id": video_id,
                        "snippet": {
                            "title": meta.get("title", ""),
                            "description": meta.get("description", ""),
                            "channelTitle": meta.get("channel_title", ""),
                            "publishedAt": meta.get("published_at", "2024-01-01T00:00:00Z"),
                            "tags": meta.get("tags", []),
                        },
                        "contentDetails": {"duration": meta.get("duration", "PT3M0S")},
                        "statistics": {
                            "viewCount": str(meta.get("view_count", 0)),
                            "likeCount": str(meta.get("like_count", 0)),
                            "commentCount": str(meta.get("comment_count", 0)),
                        },
                        "status": {"privacyStatus": meta.get("privacy_status", "public")},
                    }
                )

            _record(state, "videos.list", COST_LIST, part=part,
                    requested=len(requested), returned=len(items))
            save_state(state)
            return {"kind": "youtube#videoListResponse", "items": items}

        return _Request(run)


class FakeYouTube:
    """Stands in for the object returned by ``googleapiclient.discovery.build``."""

    def playlistItems(self) -> _PlaylistItems:  # noqa: N802 — Google naming
        return _PlaylistItems()

    def videos(self) -> _Videos:
        return _Videos()


# ─── OAuth stand-in ──────────────────────────────────────────────

TOKEN_EXPIRY = "2099-01-01T00:00:00Z"


class _FakeCredentials:
    valid = True
    expired = False
    refresh_token = "e2e-refresh-token"
    token = "e2e-access-token"

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "e2e.apps.googleusercontent.com",
                "client_secret": "e2e-client-secret",
                "scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"],
                "universe_domain": "googleapis.com",
                "expiry": TOKEN_EXPIRY,
            }
        )


class _FakeFlow:
    def __init__(self, client_secrets_file: str) -> None:
        self.client_secrets_file = client_secrets_file

    def run_local_server(self, *_args: Any, **_kwargs: Any) -> _FakeCredentials:
        state = load_state()
        state["oauth_consent_runs"] = int(state.get("oauth_consent_runs", 0)) + 1
        save_state(state)
        return _FakeCredentials()


# ─── Installation ────────────────────────────────────────────────


def install() -> None:
    """Redirect the Google client stack at the fake, before anything imports it."""
    import google_auth_oauthlib.flow as oauth_flow
    import googleapiclient.discovery as discovery

    def fake_build(*_args: Any, **_kwargs: Any) -> FakeYouTube:
        return FakeYouTube()

    discovery.build = fake_build  # type: ignore[assignment]
    oauth_flow.InstalledAppFlow.from_client_secrets_file = staticmethod(  # type: ignore[assignment]
        lambda path, *_a, **_k: _FakeFlow(str(path))
    )
