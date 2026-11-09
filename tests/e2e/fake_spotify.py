"""
fake_spotify.py — Offline stand-in for the slice of the Spotify Web API the skill uses.

Unlike ``fake_youtube.py`` (patched into child processes), this is a real HTTP
server on 127.0.0.1: the client under test makes genuine ``requests`` calls,
so URL building, headers, paging, status handling and JSON parsing are all
exercised.  The skill reaches it through ``YT_SKILL_SPOTIFY_API_BASE`` /
``YT_SKILL_SPOTIFY_ACCOUNTS_BASE``, which the client only accepts for loopback
hosts.

Semantics reproduced from the Web API reference (2026):

- ``GET  /v1/playlists/{id}`` → ``snapshot_id``, ``name``, ``owner``, ``collaborative``
- ``GET  /v1/playlists/{id}/items`` → pages of ``{added_at, is_local, item}`` (max 50)
- ``PUT  /v1/playlists/{id}/items`` → move ``[range_start, range_start+range_length)``
  to before the item that was at ``insert_before`` **before** the call; returns a
  new ``snapshot_id``
- ``POST /api/token`` → refresh-token grant
- 401 for a stale bearer token, 429 + ``Retry-After`` / 4xx / 5xx on demand

Faults are armed per reorder count, like the YouTube fake.  A fault with
``apply_first`` applies the move and *then* fails, which is the ambiguous case
a non-idempotent write has to reconcile.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

USER_ID = "e2e-user"


class FakeSpotify:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.playlists: dict[str, list[dict[str, Any]]] = {}
        self.versions: dict[str, int] = {}
        self.owners: dict[str, str] = {}
        self.valid_tokens: set[str] = {"e2e-access-token"}
        self.token_serial = 0
        self.faults: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.reorders_applied: dict[str, int] = {}
        #: Set by a test that drives the PKCE sign-in (the "browser" sees it first).
        self.expected_challenge = ""
        self.server: ThreadingHTTPServer | None = None

    # -- state helpers --------------------------------------------------

    def add_playlist(self, playlist_id: str, entries: list[dict[str, Any]], owner: str = USER_ID) -> None:
        with self.lock:
            self.playlists[playlist_id] = [dict(e) for e in entries]
            self.versions[playlist_id] = 1
            self.owners[playlist_id] = owner
            self.reorders_applied[playlist_id] = 0

    def snapshot_id(self, playlist_id: str) -> str:
        return f"snap-{playlist_id}-{self.versions[playlist_id]}"

    def uris(self, playlist_id: str) -> list[str]:
        return [e["item"]["uri"] for e in self.playlists[playlist_id]]

    def arm(self, playlist_id: str, when_applied: int, status: int, *,
            retry_after: int | None = None, apply_first: bool = False, remaining: int = 1) -> None:
        with self.lock:
            self.faults.append({
                "playlist_id": playlist_id, "when_applied": when_applied, "status": status,
                "retry_after": retry_after, "apply_first": apply_first, "remaining": remaining,
            })

    def count(self, method: str, path_part: str = "") -> int:
        return len([c for c in self.calls if c["method"] == method and path_part in c["path"]])

    # -- server lifecycle -----------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any] | None = None,
                      headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload or {}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                if token in fake.valid_tokens:
                    return True
                self._send(401, {"error": {"status": 401, "message": "The access token expired"}})
                return False

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                with fake.lock:
                    fake.calls.append({"method": "POST", "path": self.path, "body": form})
                    if self.path == "/api/token" and form.get("grant_type") == "authorization_code":
                        # PKCE: the verifier must hash to the challenge the browser carried.
                        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                        if form.get("code") != "e2e-code" or challenge != fake.expected_challenge:
                            self._send(400, {"error": "invalid_grant"})
                            return
                        fake.valid_tokens.add("e2e-access-token-login")
                        self._send(200, {"access_token": "e2e-access-token-login", "token_type": "Bearer",
                                         "expires_in": 3600, "refresh_token": "e2e-refresh-token",
                                         "scope": "playlist-modify-private"})
                        return
                    if self.path != "/api/token" or form.get("grant_type") != "refresh_token":
                        self._send(400, {"error": "unsupported_grant_type"})
                        return
                    if form.get("refresh_token") != "e2e-refresh-token":
                        self._send(400, {"error": "invalid_grant"})
                        return
                    fake.token_serial += 1
                    token = f"e2e-access-token-{fake.token_serial}"
                    fake.valid_tokens.add(token)
                    self._send(200, {"access_token": token, "token_type": "Bearer",
                                     "expires_in": 3600, "scope": "playlist-modify-private"})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                with fake.lock:
                    fake.calls.append({"method": "GET", "path": parsed.path, "query": query})
                    if not self._authorized():
                        return
                    parts = parsed.path.strip("/").split("/")
                    if parts == ["v1", "me"]:
                        self._send(200, {"id": USER_ID, "display_name": "E2E User"})
                        return
                    if len(parts) >= 3 and parts[:2] == ["v1", "playlists"]:
                        playlist_id = parts[2]
                        if playlist_id not in fake.playlists:
                            self._send(404, {"error": {"status": 404, "message": "Not found."}})
                            return
                        entries = fake.playlists[playlist_id]
                        if len(parts) == 3:
                            self._send(200, {
                                "snapshot_id": fake.snapshot_id(playlist_id),
                                "name": f"E2E {playlist_id}",
                                "owner": {"id": fake.owners[playlist_id]},
                                "collaborative": False,
                            })
                            return
                        if parts[3] == "items":
                            limit = min(int(query.get("limit", 20)), 50)
                            offset = int(query.get("offset", 0))
                            page = entries[offset:offset + limit]
                            nxt = (
                                f"http://fake/v1/playlists/{playlist_id}/items?offset={offset + limit}"
                                if offset + limit < len(entries) else None
                            )
                            self._send(200, {"items": page, "next": nxt, "total": len(entries),
                                             "limit": limit, "offset": offset})
                            return
                    self._send(404, {"error": {"status": 404, "message": "Unknown path"}})

            def do_PUT(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                with fake.lock:
                    fake.calls.append({"method": "PUT", "path": parsed.path, "body": body})
                    if not self._authorized():
                        return
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) != 4 or parts[:2] != ["v1", "playlists"] or parts[3] != "items":
                        self._send(404, {"error": {"status": 404, "message": "Unknown path"}})
                        return
                    playlist_id = parts[2]
                    if fake.owners.get(playlist_id) != USER_ID:
                        self._send(403, {"error": {"status": 403, "message": "Forbidden"}})
                        return

                    applied = fake.reorders_applied[playlist_id]
                    fault = next(
                        (f for f in fake.faults
                         if f["playlist_id"] == playlist_id and f["when_applied"] == applied
                         and f["remaining"] > 0),
                        None,
                    )
                    if fault and not fault["apply_first"]:
                        fault["remaining"] -= 1
                        headers = {"Retry-After": str(fault["retry_after"])} if fault["retry_after"] else None
                        self._send(fault["status"], {"error": {"status": fault["status"],
                                                               "message": "injected fault"}}, headers)
                        return

                    entries = fake.playlists[playlist_id]
                    start = int(body["range_start"])
                    span = int(body.get("range_length", 1))
                    before = int(body["insert_before"])
                    if not (0 <= start and start + span <= len(entries) and 0 <= before <= len(entries)):
                        self._send(400, {"error": {"status": 400, "message": "Index out of bounds"}})
                        return
                    if start < before < start + span:
                        self._send(400, {"error": {"status": 400, "message": "insert_before inside range"}})
                        return
                    # Index arithmetic, not value lookup: duplicate tracks are equal dicts.
                    block = entries[start:start + span]
                    rest = entries[:start] + entries[start + span:]
                    slot = before if before <= start else before - span
                    fake.playlists[playlist_id] = rest[:slot] + block + rest[slot:]
                    fake.versions[playlist_id] += 1
                    fake.reorders_applied[playlist_id] = applied + 1

                    if fault and fault["apply_first"]:
                        fault["remaining"] -= 1
                        self._send(fault["status"], {"error": {"status": fault["status"],
                                                               "message": "injected after apply"}})
                        return
                    self._send(200, {"snapshot_id": fake.snapshot_id(playlist_id)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def track(serial: int, artist: tuple[str, str], album: tuple[str, str], number: int,
          local: bool = False) -> dict[str, Any]:
    """One playlist entry in the 2026 response shape (``item``, not ``track``)."""
    if local:
        return {
            "added_at": "2026-01-01T00:00:00Z",
            "is_local": True,
            "item": {
                "id": None, "uri": f"spotify:local:{artist[1]}:{album[1]}:Local+{serial}:200",
                "name": f"Local {serial}", "duration_ms": 200000, "type": "track",
                "artists": [{"id": None, "name": artist[1]}],
                "album": {"id": None, "name": album[1], "album_type": None, "total_tracks": 0},
            },
        }
    return {
        "added_at": f"2026-01-{1 + serial % 28:02d}T00:00:00Z",
        "is_local": False,
        "item": {
            "id": f"t{serial:03d}", "uri": f"spotify:track:t{serial:03d}", "name": f"Song {serial}",
            "duration_ms": 180000 + serial * 1000, "type": "track",
            "track_number": number, "disc_number": 1,
            "external_ids": {"isrc": f"E2E{serial:09d}"},
            "artists": [{"id": artist[0], "name": artist[1]}],
            "album": {"id": album[0], "name": album[1], "album_type": "album",
                      "total_tracks": 12, "release_date": "2024-05"},
        },
    }
