"""
spotify_api.py — Spotify Web API client: PKCE sign-in, playlist read, block reorder.

Only ``requests`` and the standard library, on purpose: Spotify renamed the
playlist endpoints in 2026 (``/tracks`` → ``/items``, ``track`` → ``item``), and
owning the three paths below is cheaper than waiting for a wrapper library to
follow.  All endpoint paths live in this file.

Auth is Authorization Code with PKCE: the user's own Dashboard app supplies a
client id, no client secret exists anywhere.  See ``docs/SPOTIFY_SETUP.md``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from scripts.schemas import AlbumRef, ArtistRef, EnrichedPlaylistItem

logger = logging.getLogger(__name__)

# ── Endpoints (the only place they are spelled out) ─────────────────────────

_ACCOUNTS_BASE_DEFAULT = "https://accounts.spotify.com"
_API_BASE_DEFAULT = "https://api.spotify.com/v1"

#: Must match the Redirect URI the user registered (docs/SPOTIFY_SETUP.md).
#: Spotify rejects ``localhost``; it has to be the loopback IP.
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private"
)

PAGE_SIZE = 50  # documented maximum for GET /playlists/{id}/items
LOGIN_TIMEOUT_SECONDS = 300
MAX_RETRY_AFTER_SECONDS = 60  # a longer 429 stops the run instead of stalling it
READ_RETRIES = 4

#: Spotify client ids are 32 hex characters.
CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

_PLAYLIST_ID_PATTERNS = (
    re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]+)"),
    re.compile(r"^spotify:playlist:([A-Za-z0-9]+)$"),
)


def _base_override(env_name: str, default: str) -> str:
    """Allow tests to point the client at a local fake — loopback only.

    Anything else is refused so a stray environment variable can never send
    the user's tokens to a third-party host.
    """
    value = os.environ.get(env_name, "").strip()
    if not value:
        return default
    host = urlparse(value).hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{env_name} may only point at a loopback address, got {value!r}")
    return value.rstrip("/")


def accounts_base() -> str:
    return _base_override("YT_SKILL_SPOTIFY_ACCOUNTS_BASE", _ACCOUNTS_BASE_DEFAULT)


def api_base() -> str:
    return _base_override("YT_SKILL_SPOTIFY_API_BASE", _API_BASE_DEFAULT)


def extract_spotify_playlist_id(raw: str) -> str | None:
    """Return the playlist id for a Spotify URL / URI, or None if *raw* is not Spotify."""
    text = raw.strip()
    for pattern in _PLAYLIST_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


class SpotifyError(Exception):
    """A classified Spotify failure.

    ``ambiguous`` marks a write whose outcome is unknown (5xx or a dropped
    connection after the request left): the caller must re-read the playlist
    before deciding whether to retry, because a reorder is not idempotent.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        retry_after: float | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.ambiguous = ambiguous


def _classify(response: requests.Response) -> SpotifyError:
    status = response.status_code
    try:
        detail = response.json().get("error", {})
        message = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    except ValueError:
        message = response.text[:200]
    if status == 401:
        return SpotifyError("AUTH_FAILED", message or "unauthorized", status=status)
    if status == 403:
        return SpotifyError(
            "FORBIDDEN",
            message or "forbidden — only playlists you own or collaborate on can be read or reordered",
            status=status,
        )
    if status == 404:
        return SpotifyError("PLAYLIST_NOT_FOUND", message or "playlist not found", status=status)
    if status == 429:
        retry_after = float(response.headers.get("Retry-After", "1") or 1)
        return SpotifyError("RATE_LIMITED", message or "rate limited", status=status, retry_after=retry_after)
    if status >= 500:
        return SpotifyError("SERVER_ERROR", message or f"HTTP {status}", status=status, ambiguous=True)
    return SpotifyError(f"HTTP_{status}", message or f"HTTP {status}", status=status)


# ── PKCE sign-in ─────────────────────────────────────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — http.server naming
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).result.update(query)
        ok = "code" in query
        body = (
            "Spotify 授權完成，可以關閉這個分頁回到 Agent。"
            if ok
            else f"Spotify 授權未完成：{query.get('error', 'unknown')}。請回到 Agent 重試。"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<!doctype html><meta charset=utf-8><p>{body}</p>".encode("utf-8"))

    def log_message(self, *_args: Any) -> None:  # keep stdout clean for the Agent
        return


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* with 0o700 dir / 0o600 file permissions (see .jules/sentinel.md)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.touch(mode=0o600, exist_ok=True)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── Client ───────────────────────────────────────────────────────────────────


class SpotifyClient:
    """Minimal Spotify Web API client for one user's playlists."""

    def __init__(self, client_id: str, token_path: Path, timeout: float = 30.0) -> None:
        self.client_id = client_id
        self.token_path = token_path
        self.timeout = timeout
        self._session = requests.Session()
        self._token: dict[str, Any] = {}
        #: Every HTTP call made, for the cost report (Spotify has no unit quota).
        self.calls = 0

    # -- auth ---------------------------------------------------------------

    def authenticate(self) -> None:
        """Load the cached token, refresh it, or run the browser sign-in."""
        if self.token_path.is_file():
            try:
                self._token = json.loads(self.token_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("Unreadable Spotify token file; signing in again.")
                self._token = {}
        if self._token.get("client_id") not in (None, self.client_id):
            logger.info("Token belongs to a different client id; signing in again.")
            self._token = {}

        if self._token.get("access_token") and time.time() < float(self._token.get("expires_at", 0)) - 60:
            return
        if self._token.get("refresh_token"):
            try:
                self._refresh()
                return
            except SpotifyError as exc:
                # Refresh tokens expire 6 months after the original consent.
                logger.warning("Spotify token refresh failed (%s); signing in again.", exc.code)
        self._login()

    def _store(self, payload: dict[str, Any]) -> None:
        refresh = payload.get("refresh_token") or self._token.get("refresh_token", "")
        self._token = {
            "client_id": self.client_id,
            "access_token": payload["access_token"],
            "refresh_token": refresh,
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
            "scope": payload.get("scope", self._token.get("scope", "")),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_private_json(self.token_path, self._token)

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        self.calls += 1
        try:
            response = self._session.post(
                f"{accounts_base()}/api/token", data=data, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SpotifyError("NETWORK_ERROR", str(exc)) from exc
        if response.status_code != 200:
            try:
                reason = response.json().get("error", "")
            except ValueError:
                reason = response.text[:200]
            raise SpotifyError("AUTH_FAILED", f"token endpoint: {reason}", status=response.status_code)
        return response.json()

    def _refresh(self) -> None:
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._token["refresh_token"],
                "client_id": self.client_id,
            }
        )
        self._store(payload)
        logger.info("Refreshed Spotify access token.")

    def _login(self) -> None:
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        url = f"{accounts_base()}/authorize?" + urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "scope": SCOPES,
                "state": state,
            }
        )

        _CallbackHandler.result = {}
        try:
            server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
        except OSError as exc:
            raise SpotifyError(
                "AUTH_FAILED",
                f"無法在 127.0.0.1:{REDIRECT_PORT} 接收授權回呼（連接埠被占用？）：{exc}",
            ) from exc
        server.timeout = 1

        print(json.dumps({
            "status": "waiting_for_user",
            "message": "正在開啟瀏覽器進行 Spotify 授權，請在瀏覽器中按「同意」...",
            "url": url,
        }, ensure_ascii=False))
        sys.stdout.flush()
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        try:
            while "code" not in _CallbackHandler.result and "error" not in _CallbackHandler.result:
                if time.time() > deadline:
                    raise SpotifyError("AUTH_TIMEOUT", f"{LOGIN_TIMEOUT_SECONDS} 秒內沒有完成 Spotify 授權。")
                server.handle_request()
        finally:
            server.server_close()

        result = dict(_CallbackHandler.result)
        if "error" in result:
            raise SpotifyError("AUTH_DENIED", f"Spotify 授權被拒絕：{result['error']}")
        if result.get("state") != state:
            raise SpotifyError("AUTH_FAILED", "授權回呼的 state 不符，已中止（可能是過期或偽造的回呼）。")

        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": self.client_id,
                "code_verifier": verifier,
            }
        )
        self._store(payload)
        logger.info("Completed Spotify PKCE sign-in.")

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One API call with token refresh and rate-limit handling.

        Reads retry 429 / 5xx / network errors.  Writes retry only 429 (Spotify
        rejected the call, nothing happened); a 5xx or dropped connection on a
        write is raised as ``ambiguous`` for the caller to reconcile.
        """
        is_write = method != "GET"
        refreshed = False
        attempt = 0
        while True:
            self.calls += 1
            try:
                response = self._session.request(
                    method,
                    f"{api_base()}{path}",
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {self._token.get('access_token', '')}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                error = SpotifyError("NETWORK_ERROR", str(exc), ambiguous=is_write)
                if is_write or attempt >= READ_RETRIES:
                    raise error from exc
                attempt += 1
                time.sleep(2 ** attempt)
                continue

            if response.status_code in (200, 201):
                return response.json() if response.content else {}

            error = _classify(response)
            if error.code == "AUTH_FAILED" and not refreshed and self._token.get("refresh_token"):
                self._refresh()
                refreshed = True
                continue
            if error.code == "RATE_LIMITED" and attempt < READ_RETRIES:
                wait = error.retry_after or 1
                if wait > MAX_RETRY_AFTER_SECONDS:
                    raise error
                logger.warning("Spotify 429; waiting %.0fs (Retry-After).", wait)
                time.sleep(wait)
                attempt += 1
                continue
            if error.code == "SERVER_ERROR" and not is_write and attempt < READ_RETRIES:
                attempt += 1
                time.sleep(2 ** attempt)
                continue
            if not is_write:
                error.ambiguous = False
            raise error

    # -- endpoints ----------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def get_snapshot_id(self, playlist_id: str) -> str:
        data = self._request("GET", f"/playlists/{playlist_id}", params={"fields": "snapshot_id,name"})
        return str(data.get("snapshot_id", ""))

    def get_playlist_meta(self, playlist_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/playlists/{playlist_id}", params={"fields": "snapshot_id,name,owner(id),collaborative"}
        )

    def get_playlist_entries(self, playlist_id: str) -> list[dict[str, Any]]:
        """All playlist entries in order (``GET /playlists/{id}/items``, 50 per page)."""
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                f"/playlists/{playlist_id}/items",
                params={"limit": PAGE_SIZE, "offset": offset, "additional_types": "track,episode"},
            )
            batch = page.get("items") or []
            entries.extend(batch)
            if not page.get("next") or not batch:
                return entries
            offset += len(batch)

    def get_playlist_uris(self, playlist_id: str) -> list[str]:
        return [entry_uri(entry) for entry in self.get_playlist_entries(playlist_id)]

    def reorder(self, playlist_id: str, range_start: int, insert_before: int,
                range_length: int, snapshot_id: str = "") -> str:
        """Move one contiguous block; returns the new snapshot id."""
        body: dict[str, Any] = {
            "range_start": range_start,
            "insert_before": insert_before,
            "range_length": range_length,
        }
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        data = self._request("PUT", f"/playlists/{playlist_id}/items", json_body=body)
        return str(data.get("snapshot_id", ""))


# ── Mapping ──────────────────────────────────────────────────────────────────


def _entry_media(entry: dict[str, Any]) -> dict[str, Any]:
    # 2026 responses use ``item``; older ones (and some apps) still say ``track``.
    return entry.get("item") or entry.get("track") or {}


def entry_uri(entry: dict[str, Any]) -> str:
    media = _entry_media(entry)
    return str(media.get("uri") or "")


def entries_to_items(entries: list[dict[str, Any]], playlist_id: str) -> list[EnrichedPlaylistItem]:
    """Map Spotify playlist entries to the shared item model.

    Spotify entries have no id of their own and a track may appear twice, so
    the item key is ``<uri>#<occurrence>`` — unique within one snapshot.
    """
    seen: dict[str, int] = {}
    items: list[EnrichedPlaylistItem] = []
    for position, entry in enumerate(entries):
        media = _entry_media(entry)
        uri = str(media.get("uri") or f"spotify:unavailable:{position}")
        occurrence = seen.get(uri, 0)
        seen[uri] = occurrence + 1

        artists = [
            ArtistRef(name=str(a.get("name") or ""), id=str(a.get("id") or ""))
            for a in (media.get("artists") or [])
        ]
        show = media.get("show") or {}
        if not artists and show.get("name"):  # podcast episode: the show is the "artist"
            artists = [ArtistRef(name=str(show["name"]), id=str(show.get("id") or ""))]

        raw_album = media.get("album") or {}
        album = (
            AlbumRef(
                id=str(raw_album.get("id") or ""),
                name=str(raw_album.get("name") or ""),
                album_type=str(raw_album.get("album_type") or ""),
                total_tracks=int(raw_album.get("total_tracks") or 0),
            )
            if raw_album
            else None
        )

        added_at = entry.get("added_at") or "1970-01-01T00:00:00Z"
        release = raw_album.get("release_date") or media.get("release_date") or ""
        published_at = None
        if release:
            padded = {4: f"{release}-01-01", 7: f"{release}-01"}.get(len(release), release)
            published_at = f"{padded[:10]}T00:00:00Z"

        items.append(
            EnrichedPlaylistItem(
                playlist_item_id=f"{uri}#{occurrence}",
                video_id=str(media.get("id") or uri),
                position=position,
                added_at=added_at,
                playlist_id=playlist_id,
                platform="spotify",
                title=str(media.get("name") or ""),
                channel_title=artists[0].name if artists else "",
                published_at=published_at,
                duration_seconds=int(media.get("duration_ms") or 0) // 1000,
                artists=artists,
                album=album,
                track_number=int(media.get("track_number") or 0),
                disc_number=int(media.get("disc_number") or 0),
                isrc=str((media.get("external_ids") or {}).get("isrc") or ""),
                uri=uri,
                # Local files and greyed-out tracks still sit at an index that
                # Spotify reorders like any other (docs/BACKLOG.md §6, M1 assumption 7).
                is_available=True,
                metadata_available=bool(media),
            )
        )
    return items


def uri_of_key(key: str) -> str:
    """``spotify:track:abc#1`` → ``spotify:track:abc``."""
    return key.rsplit("#", 1)[0]
