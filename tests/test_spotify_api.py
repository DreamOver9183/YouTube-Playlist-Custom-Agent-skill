"""
test_spotify_api.py — Unit tests for the Spotify client (no network, no browser).

The e2e suite drives the CLI against ``tests/e2e/fake_spotify.py``; this file
covers what a CLI run cannot reach without a real browser and a real account:

1. Playlist URL / URI parsing and the loopback-only test override.
2. Mapping 2026-shaped entries (``item``) and older ones (``track``), duplicate
   tracks, local files, podcast episodes, partial release dates.
3. The PKCE sign-in end to end: the "browser" follows the authorize URL, the
   callback carries code + state, the token endpoint checks the verifier hashes
   to the challenge.  A forged state must be refused.
4. ``_request``: 401 → refresh → retry; a 5xx on a read is retried, on a write
   it is reported as ambiguous instead of being resent.
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for path in (PROJECT_ROOT, PROJECT_ROOT / "tests" / "e2e"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import fake_spotify  # noqa: E402
from scripts import spotify_api  # noqa: E402
from scripts.spotify_api import (  # noqa: E402
    SpotifyClient,
    SpotifyError,
    entries_to_items,
    extract_spotify_playlist_id,
)

CLIENT_ID = "0123456789abcdef0123456789abcdef"


class FakeEnv:
    """Start the fake and point the client at it for the duration of a test."""

    def __enter__(self) -> fake_spotify.FakeSpotify:
        self.fake = fake_spotify.FakeSpotify()
        base = self.fake.start()
        self.saved = {k: os.environ.get(k) for k in
                      ("YT_SKILL_SPOTIFY_API_BASE", "YT_SKILL_SPOTIFY_ACCOUNTS_BASE")}
        os.environ["YT_SKILL_SPOTIFY_API_BASE"] = f"{base}/v1"
        os.environ["YT_SKILL_SPOTIFY_ACCOUNTS_BASE"] = base
        return self.fake

    def __exit__(self, *_exc) -> None:
        self.fake.stop()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def client_with_token(tmp: Path, *, expired: bool) -> SpotifyClient:
    token_path = tmp / "spotify_token.json"
    token_path.write_text(json.dumps({
        "client_id": CLIENT_ID, "access_token": "e2e-access-token",
        "refresh_token": "e2e-refresh-token",
        "expires_at": time.time() + (-60 if expired else 3600),
    }), encoding="utf-8")
    client = SpotifyClient(CLIENT_ID, token_path)
    client.authenticate()
    return client


# ─── 1. parsing and overrides ─────────────────────


def test_playlist_id_parsing():
    cases = {
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M": "37i9dQZF1DXcBWIGoYBM5M",
        "https://open.spotify.com/intl-ja/playlist/37i9dQZF1DXcBWIGoYBM5M?si=1a2b": "37i9dQZF1DXcBWIGoYBM5M",
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M": "37i9dQZF1DXcBWIGoYBM5M",
        "https://www.youtube.com/playlist?list=PLabc": None,
        "PLabc": None,
        "https://open.spotify.com/album/1A2B3C": None,
    }
    for raw, expected in cases.items():
        assert extract_spotify_playlist_id(raw) == expected, raw
    print("  ✓ playlist id parsing: URL, intl URL, URI; YouTube and albums rejected")


def test_override_is_loopback_only():
    os.environ["YT_SKILL_SPOTIFY_API_BASE"] = "https://evil.example.com/v1"
    try:
        spotify_api.api_base()
    except ValueError:
        pass
    else:
        raise AssertionError("a non-loopback API base must be refused")
    finally:
        os.environ.pop("YT_SKILL_SPOTIFY_API_BASE", None)
    assert spotify_api.api_base() == "https://api.spotify.com/v1"
    print("  ✓ base-URL override refuses non-loopback hosts")


# ─── 2. mapping ───────────────────────────────────


def test_entry_mapping():
    song = fake_spotify.track(1, ("ar1", "Aimer"), ("al1", "Walpurgis"), number=3)
    legacy = {"added_at": "2025-01-01T00:00:00Z", "track": dict(song["item"], uri="spotify:track:old", id="old")}
    local = fake_spotify.track(2, ("", "Local Band"), ("", "Demo"), 1, local=True)
    episode = {"added_at": "2026-02-02T00:00:00Z", "item": {
        "id": "ep1", "uri": "spotify:episode:ep1", "name": "Ep 1", "duration_ms": 60000,
        "type": "episode", "show": {"id": "sh1", "name": "Some Show"}, "release_date": "2026"}}
    items = entries_to_items([song, legacy, dict(song), local, episode], "PL")

    assert [i.playlist_item_id for i in items] == [
        "spotify:track:t001#0", "spotify:track:old#0", "spotify:track:t001#1",
        local["item"]["uri"] + "#0", "spotify:episode:ep1#0"]
    first = items[0]
    assert first.platform == "spotify" and first.artists[0].id == "ar1" and first.album.id == "al1"
    assert first.track_number == 3 and first.isrc == "E2E000000001" and first.channel_title == "Aimer"
    assert first.published_at.year == 2024 and first.published_at.month == 5  # "2024-05" padded
    assert items[1].title == "Song 1", "legacy 'track' key must still be read"
    assert items[3].artists[0].id == "" and items[3].is_available, "local file: name-only, movable"
    assert items[4].artists[0].name == "Some Show" and items[4].published_at.year == 2026
    print("  ✓ mapping: item/track keys, duplicate '#1', local file, episode, partial dates")


# ─── 3. PKCE sign-in ──────────────────────────────


def _drive_browser(fake: fake_spotify.FakeSpotify, forge_state: bool):
    """Stand in for the user's browser: read the authorize URL, hit the callback."""
    def open_url(url: str) -> bool:
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == spotify_api.REDIRECT_URI
        assert "playlist-modify-private" in query["scope"]
        fake.expected_challenge = query["code_challenge"]
        state = "forged" if forge_state else query["state"]
        callback = f"{spotify_api.REDIRECT_URI}?code=e2e-code&state={state}"
        for _ in range(50):  # the callback server starts just before the browser opens
            try:
                urllib.request.urlopen(callback, timeout=2).read()
                return True
            except OSError:
                time.sleep(0.1)
        raise AssertionError("callback server never came up")
    return open_url


def test_pkce_sign_in():
    with FakeEnv() as fake, tempfile.TemporaryDirectory() as tmp:
        original_open = spotify_api.webbrowser.open
        original_stdout = sys.stdout
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # swallow waiting_for_user
            spotify_api.webbrowser.open = _drive_browser(fake, forge_state=False)
            client = SpotifyClient(CLIENT_ID, Path(tmp) / "spotify_token.json")
            client.authenticate()
            stored = json.loads((Path(tmp) / "spotify_token.json").read_text(encoding="utf-8"))
            assert stored["access_token"] == "e2e-access-token-login"
            assert stored["refresh_token"] == "e2e-refresh-token" and stored["client_id"] == CLIENT_ID
            if os.name == "posix":
                assert (Path(tmp) / "spotify_token.json").stat().st_mode & 0o777 == 0o600

            spotify_api.webbrowser.open = _drive_browser(fake, forge_state=True)
            forged = SpotifyClient(CLIENT_ID, Path(tmp) / "other_token.json")
            try:
                forged.authenticate()
            except SpotifyError as exc:
                assert exc.code == "AUTH_FAILED" and "state" in str(exc)
            else:
                raise AssertionError("a callback with a forged state must be refused")
        finally:
            sys.stdout.close()
            sys.stdout = original_stdout
            spotify_api.webbrowser.open = original_open
    print("  ✓ PKCE sign-in: verifier matches challenge, token saved; forged state refused")


# ─── 4. request policy ────────────────────────────


def test_refresh_and_retry_policy():
    with FakeEnv() as fake, tempfile.TemporaryDirectory() as tmp:
        fake.add_playlist("PL1", [fake_spotify.track(i, ("a", "A"), ("b", "B"), i) for i in range(3)])
        client = client_with_token(Path(tmp), expired=False)

        fake.valid_tokens.discard("e2e-access-token")  # server-side revocation → 401
        assert client.get_snapshot_id("PL1") == fake.snapshot_id("PL1")
        assert fake.count("POST", "/api/token") == 1, "401 should trigger exactly one refresh"

        fake.arm("PL1", when_applied=0, status=503)
        try:
            client.reorder("PL1", 0, 3, 1)
        except SpotifyError as exc:
            assert exc.ambiguous and exc.code == "SERVER_ERROR"
        else:
            raise AssertionError("a 5xx on a write must surface as ambiguous")
        assert fake.count("PUT") == 1, "an ambiguous write must not be resent by the client"

        fake.arm("PL1", when_applied=0, status=429, retry_after=1)
        new_snapshot = client.reorder("PL1", 0, 3, 1)
        assert new_snapshot == fake.snapshot_id("PL1") and fake.count("PUT") == 3, "429 should be retried"
        assert fake.uris("PL1")[-1] == "spotify:track:t000"
    print("  ✓ request policy: 401→refresh, write 5xx ambiguous (not resent), 429 retried")


TESTS = [
    test_playlist_id_parsing,
    test_override_is_loopback_only,
    test_entry_mapping,
    test_pkce_sign_in,
    test_refresh_and_retry_policy,
]


def run_all_tests() -> bool:
    logging.disable(logging.ERROR)
    passed = failed = 0
    print(f"\n{'=' * 60}\n Spotify client\n{'=' * 60}")
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
