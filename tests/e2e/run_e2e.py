"""
run_e2e.py — Full end-to-end suite for the YouTube Playlist Agent-Skill.

Black box by design: every step is a real OS process running a real entry point
(``python -m scripts.yt_tool ...`` and ``node dist/cli.js ...``), exactly the
commands ``docs/agent/AGENT_SOP.md`` tells an Agent to run.  Nothing from
``scripts/`` is imported here, so the assertions below never validate the
implementation against itself — the expected orderings, quota figures and
reorder replays are computed independently in this file.

Only the network boundary is faked (``tests/e2e/fake_youtube.py``), so no real
OAuth consent, no real quota, no real playlist is touched.

Run:
    python tests/e2e/run_e2e.py            # full suite
    python tests/e2e/run_e2e.py --keep     # keep the temp workspace for inspection
    python tests/e2e/run_e2e.py -k update  # only scenarios whose name matches

Exit code 0 iff every scenario passes.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# ─── Playlists used by the suite (each scenario owns its own) ────

PL_MAIN = "PLE2EMAIN"        # 120 items, 2 private — fetch / cache / optimize / update
PL_STALE = "PLE2ESTALE"      # 30 items — remote drift must be refused
PL_RESUME = "PLE2ERESUME"    # 30 items — retry, abort, resume
PL_CLEAN = "PLE2ECLEAN"      # 60 items, no private — TS engine + minimality
PL_SORTED = "PLE2ESORTED"    # 24 items already grouped — the zero-change case

QUOTA_LIST = 1
QUOTA_UPDATE = 50


# ─── Result plumbing ─────────────────────────────────────────────


class Failure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


@dataclass
class Scenario:
    key: str
    phase: str
    title: str
    fn: Callable[["Harness"], list[str]]


@dataclass
class Outcome:
    scenario: Scenario
    passed: bool
    seconds: float
    details: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class Run:
    """One finished subprocess."""

    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def payloads(self) -> list[dict[str, Any]]:
        out = []
        for line in self.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    @property
    def last(self) -> dict[str, Any]:
        payloads = self.payloads
        if not payloads:
            raise Failure(
                f"command emitted no JSON: {' '.join(self.argv)}\n"
                f"stdout={self.stdout!r}\nstderr={self.stderr[-2000:]!r}"
            )
        return payloads[-1]


# ─── Independent oracles (deliberately not imported from scripts/) ─


def replay(order: list[str], changes: list[dict[str, Any]]) -> list[str]:
    """Apply a change set the way ``playlistItems.update`` does: remove + insert.

    Written from the API contract, not from the project's planner, so that a
    planner bug cannot hide behind a matching bug here.
    """
    live = list(order)
    for change in sorted(changes, key=lambda c: c["execution_order"]):
        live.remove(change["playlist_item_id"])
        live.insert(change["new_position"], change["playlist_item_id"])
    return live


def lis_length(current: list[str], target: list[str]) -> int:
    """Longest increasing subsequence of original positions read in target order."""
    original = {item_id: idx for idx, item_id in enumerate(current)}
    tails: list[int] = []
    for item_id in target:
        value = original[item_id]
        slot = bisect.bisect_left(tails, value)
        if slot == len(tails):
            tails.append(value)
        else:
            tails[slot] = value
    return len(tails)


def ids_of(records: list[dict[str, Any]]) -> list[str]:
    return [r["playlist_item_id"] for r in records]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ─── Fixture ─────────────────────────────────────────────────────

MAIN_ARTISTS = [("Aurora", 40), ("Borealis", 30), ("Cascade", 30), ("Delta", 18)]
CLEAN_ARTISTS = [("Nimbus", 22), ("Stratus", 20), ("Cirrus", 18)]
SORTED_ARTISTS = [("Zephyr", 8), ("Mistral", 8), ("Sirocco", 8)]


def build_fixture(seed: int = 20260821) -> dict[str, Any]:
    """Deterministic remote state for the fake API."""
    rng = random.Random(seed)
    videos: dict[str, dict[str, Any]] = {}
    playlists: dict[str, list[dict[str, Any]]] = {}
    used_views: set[int] = set()
    counter = {"n": 0}

    def make(artist: str, index: int, playlist_id: str) -> dict[str, Any]:
        counter["n"] += 1
        n = counter["n"]
        video_id = f"vid{n:05d}"
        while True:
            views = rng.randrange(1_000, 5_000_000)
            if views not in used_views:
                used_views.add(views)
                break
        videos[video_id] = {
            "title": f"{artist} - Track {index:02d}",
            "channel_title": artist,
            "published_at": f"20{15 + index % 9:02d}-0{1 + index % 9}-1{index % 9}T08:00:00Z",
            "duration": f"PT{2 + index % 7}M{10 + index % 45}S",
            "view_count": views,
            "like_count": views // 20,
            "comment_count": views // 400,
            "tags": [artist.lower()],
            "privacy_status": "public",
        }
        return {
            "playlist_item_id": f"pli{n:05d}",
            "video_id": video_id,
            "channel_title": artist,
            "added_at": f"2024-{1 + n % 12:02d}-{1 + n % 28:02d}T12:00:00Z",
            "privacy_status": "public",
        }

    def private_entry(tag: str) -> dict[str, Any]:
        counter["n"] += 1
        n = counter["n"]
        video_id = f"vid{n:05d}"
        videos[video_id] = {
            "title": f"private {tag}",
            "channel_title": "Private Uploader",
            "privacy_status": "private",
            "view_count": 0,
            "duration": "PT1M0S",
        }
        return {
            "playlist_item_id": f"pli{n:05d}",
            "video_id": video_id,
            "channel_title": "Private Uploader",
            "added_at": "2024-06-01T12:00:00Z",
            "privacy_status": "private",
        }

    # Main playlist: shuffled, with two immovable private videos.
    main = [make(a, i, PL_MAIN) for a, count in MAIN_ARTISTS for i in range(count)]
    rng.shuffle(main)
    main.insert(17, private_entry("A"))
    main.insert(84, private_entry("B"))
    playlists[PL_MAIN] = main

    for playlist_id, spec in ((PL_STALE, MAIN_ARTISTS[:2]), (PL_RESUME, MAIN_ARTISTS[:2])):
        items = [make(a, i, playlist_id) for a, count in spec for i in range(count // 2)]
        rng.shuffle(items)
        playlists[playlist_id] = items[:30]

    clean = [make(a, i, PL_CLEAN) for a, count in CLEAN_ARTISTS for i in range(count)]
    rng.shuffle(clean)
    playlists[PL_CLEAN] = clean

    # Already grouped *and* already sorted by views inside each group.
    grouped: list[dict[str, Any]] = []
    for artist, count in SORTED_ARTISTS:
        block = [make(artist, i, PL_SORTED) for i in range(count)]
        block.sort(key=lambda e: videos[e["video_id"]]["view_count"], reverse=True)
        grouped.extend(block)
    playlists[PL_SORTED] = grouped

    return {
        "playlists": playlists,
        "videos": videos,
        "quota_used": 0,
        "applied_updates": {},
        "calls": [],
        "faults": [],
        "oauth_consent_runs": 0,
    }


VALID_OAUTH_JSON = {
    "installed": {
        "client_id": "e2e.apps.googleusercontent.com",
        "project_id": "yt-playlist-e2e",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "e2e-client-secret",
        "redirect_uris": ["http://localhost"],
    }
}


# ─── Harness ─────────────────────────────────────────────────────


class Harness:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.data = workspace / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.skill_home = workspace / "skill_home"
        self.state_file = workspace / "remote_state.json"
        self.state_file.write_text(
            json.dumps(build_fixture(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.creds_source = workspace / "downloads" / "client_secret.json"
        self.creds_source.parent.mkdir(parents=True, exist_ok=True)
        self.creds_source.write_text(json.dumps(VALID_OAUTH_JSON), encoding="utf-8")
        self.node_ready = False

    # -- state --------------------------------------------------

    @property
    def state(self) -> dict[str, Any]:
        return read_json(self.state_file)

    def write_state(self, state: dict[str, Any]) -> None:
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def remote_ids(self, playlist_id: str) -> list[str]:
        return [e["playlist_item_id"] for e in self.state["playlists"][playlist_id]]

    def quota(self) -> int:
        return int(self.state.get("quota_used", 0))

    def call_count(self, op: str | None = None) -> int:
        calls = self.state.get("calls", [])
        return len([c for c in calls if op is None or c["op"] == op])

    def arm_fault(self, op: str, playlist_id: str, when_applied: int | None,
                  status: int, reason: str, remaining: int = 1) -> None:
        state = self.state
        state.setdefault("faults", []).append(
            {"op": op, "playlist_id": playlist_id, "when_applied": when_applied,
             "status": status, "reason": reason, "remaining": remaining}
        )
        self.write_state(state)

    def clear_faults(self) -> None:
        state = self.state
        state["faults"] = []
        self.write_state(state)

    # -- processes ----------------------------------------------

    def env(self, *, with_fake: bool = True, skill_home: Path | None = None,
            extra_path: list[Path] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        parts = [str(p) for p in (extra_path or [])]
        if with_fake:
            parts.append(str(HERE))
        parts.append(str(REPO))
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["YT_SKILL_HOME"] = str(skill_home if skill_home is not None else self.skill_home)
        if with_fake:
            env["YT_E2E_STATE"] = str(self.state_file)
        else:
            env.pop("YT_E2E_STATE", None)
        return env

    def tool(self, *args: str, **env_kwargs: Any) -> Run:
        argv = [sys.executable, "-m", "scripts.yt_tool", *args]
        proc = subprocess.run(
            argv, cwd=str(REPO), env=self.env(**env_kwargs), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        return Run(argv, proc.returncode, proc.stdout, proc.stderr)

    def node(self, *args: str) -> Run:
        argv = ["node", str(REPO / "dist" / "cli.js"), *args]
        proc = subprocess.run(
            argv, cwd=str(REPO), env=self.env(), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        return Run(argv, proc.returncode, proc.stdout, proc.stderr)

    def ensure_node_build(self) -> None:
        if self.node_ready:
            return
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        check(npm is not None, "npm not found on PATH — the TypeScript path cannot be tested")
        proc = subprocess.run(
            [npm, "run", "build"], cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=(os.name == "nt"), timeout=600,
        )
        check(proc.returncode == 0, f"npm run build failed:\n{proc.stdout}\n{proc.stderr}")
        check((REPO / "dist" / "cli.js").is_file(), "dist/cli.js missing after build")
        self.node_ready = True

    # -- convenience --------------------------------------------

    def path(self, name: str) -> Path:
        return self.data / name

    def install_credentials(self) -> None:
        run = self.tool("setup_credentials", str(self.creds_source))
        check(run.code == 0, f"setup_credentials failed: {run.last}")

    def fetch(self, playlist_id: str, out: str, refresh: bool = True, **env_kwargs: Any) -> Run:
        args = ["fetch", playlist_id, "--out", str(self.path(out))]
        if refresh:
            args.append("--refresh")
        return self.tool(*args, **env_kwargs)


# ─── Shared assertions ───────────────────────────────────────────


def assert_change_set_shape(change_set: dict[str, Any], playlist_id: str,
                            current_ids: list[str]) -> None:
    check(change_set.get("version") == 2, f"change set version must be 2, got {change_set.get('version')}")
    check(change_set.get("playlist_id") == playlist_id,
          f"change set playlist_id mismatch: {change_set.get('playlist_id')}")
    check(bool(change_set.get("fingerprint")), "change set has no fingerprint")
    snapshot = change_set.get("source_snapshot", {})
    check(snapshot.get("item_ids") == current_ids,
          "source_snapshot.item_ids does not match the fetched playlist order")
    check(snapshot.get("item_count") == len(current_ids), "source_snapshot.item_count mismatch")

    orders = [c["execution_order"] for c in change_set["changes"]]
    check(orders == list(range(len(orders))),
          f"execution_order must be contiguous 0..n-1, got {orders[:10]}...")
    for change in change_set["changes"]:
        check(change["playlist_item_id"] in current_ids,
              f"change references unknown item {change['playlist_item_id']}")
        check(change["new_position"] >= 0, "negative new_position")
        check(change["resource_id"] == change["video_id"],
              "resource_id must carry the videoId the update API requires")


def assert_permutation(current: list[dict[str, Any]], target: list[dict[str, Any]]) -> None:
    check(len(current) == len(target),
          f"target has {len(target)} items, current has {len(current)}")
    check(sorted(ids_of(current)) == sorted(ids_of(target)),
          "target is not a permutation of current (items added or dropped)")


def assert_pinned_in_place(current: list[dict[str, Any]], target: list[dict[str, Any]]) -> int:
    pinned = 0
    for index, record in enumerate(current):
        if record.get("is_available") is False:
            pinned += 1
            check(target[index]["playlist_item_id"] == record["playlist_item_id"],
                  f"pinned item {record['playlist_item_id']} moved from index {index}")
    return pinned


def assert_grouped_and_sorted(target: list[dict[str, Any]], current: list[dict[str, Any]],
                              group_field: str, sort_field: str) -> list[str]:
    """Groups contiguous, first-appearance order, descending sort inside each group."""
    movable = [r for r in target if r.get("is_available") is not False]
    runs: list[str] = []
    for record in movable:
        key = record.get(group_field) or ""
        if not runs or runs[-1] != key:
            runs.append(key)
    check(len(runs) == len(set(runs)),
          f"groups are not contiguous — group sequence was {runs}")

    first_seen: list[str] = []
    for record in current:
        if record.get("is_available") is False:
            continue
        key = record.get(group_field) or ""
        if key not in first_seen:
            first_seen.append(key)
    check(runs == first_seen,
          f"group order is not first-appearance: got {runs}, expected {first_seen}")

    for group in runs:
        values = [r[sort_field] for r in movable if (r.get(group_field) or "") == group]
        check(values == sorted(values, reverse=True),
              f"group {group!r} is not sorted by {sort_field} descending")
    return runs


# ─── Scenarios ───────────────────────────────────────────────────


def s01_entry_points(h: Harness) -> list[str]:
    """SOP structure: every documented Agent entry point exists and the CLI runs."""
    entries = [
        ".claude/skills/yt-playlist-manager/SKILL.md",
        ".gemini/skills/yt-playlist-manager/SKILL.md",
        "AGENTS.md",
        "docs/agent/AGENT_SOP.md",
    ]
    for rel in entries:
        check((REPO / rel).is_file(), f"documented entry point missing: {rel}")

    helped = h.tool("--help")
    check(helped.code == 0, f"`yt_tool --help` exited {helped.code}")
    for command in ("setup_credentials", "fetch", "optimize", "diff", "update"):
        check(command in helped.stdout, f"`--help` does not list the {command} command")

    bogus = h.tool("frobnicate")
    check(bogus.code != 0, "an unknown subcommand must not exit 0")
    return [f"{len(entries)} entry points present", "5 documented subcommands exposed"]


def s02_credentials_gate(h: Harness) -> list[str]:
    """Phase 0: missing / bad / good credentials all behave as the SOP promises."""
    empty_home = h.workspace / "empty_home"
    missing = h.fetch(PL_MAIN, "should_not_exist.json", skill_home=empty_home)
    check(missing.code == 1, f"fetch without credentials must exit 1, got {missing.code}")
    check(missing.last.get("code") == "CREDENTIALS_MISSING",
          f"expected CREDENTIALS_MISSING, got {missing.last}")
    check("expected_path" in missing.last, "CREDENTIALS_MISSING must tell the Agent where to put it")
    check(not h.path("should_not_exist.json").exists(), "fetch wrote output despite failing")
    check(h.quota() == 0, "a credential failure must not touch the API")

    absent = h.tool("setup_credentials", str(h.workspace / "nope" / "client_secret.json"))
    check(absent.last.get("code") == "FILE_NOT_FOUND", f"expected FILE_NOT_FOUND, got {absent.last}")

    junk = h.workspace / "downloads" / "junk.json"
    junk.write_text(json.dumps({"api_key": "not-an-oauth-client"}), encoding="utf-8")
    invalid = h.tool("setup_credentials", str(junk))
    check(invalid.last.get("code") == "INVALID_JSON", f"expected INVALID_JSON, got {invalid.last}")

    ok = h.tool("setup_credentials", str(h.creds_source))
    check(ok.last.get("status") == "success", f"setup_credentials failed: {ok.last}")
    installed = h.skill_home / "credentials" / "client_secret.json"
    check(installed.is_file(), f"credentials were not installed at {installed}")
    check(read_json(installed) == VALID_OAUTH_JSON, "installed credentials differ from the source")
    return ["CREDENTIALS_MISSING → FILE_NOT_FOUND → INVALID_JSON → success",
            f"installed under $YT_SKILL_HOME ({installed.name})"]


def s03_fetch_pagination(h: Harness) -> list[str]:
    """Phase 1: 120 items over 3 pages, 3 metadata batches, private videos kept."""
    before = h.quota()
    run = h.fetch(PL_MAIN, "current.json")
    check(run.code == 0, f"fetch failed: {run.last}")
    payload = run.last
    check(payload["status"] == "success", f"fetch not successful: {payload}")
    check(payload["item_count"] == 120, f"expected 120 items, got {payload['item_count']}")
    check(payload["hidden_count"] == 2, f"expected 2 hidden items, got {payload['hidden_count']}")
    check(payload["source"] == "api", f"--refresh must hit the API, got {payload['source']}")
    check(bool(payload["hidden_note"]), "hidden items must come with an explanation for the user")

    consent = [p for p in run.payloads if p.get("status") == "waiting_for_user"]
    check(len(consent) == 1, "first authentication must announce the OAuth consent step")
    check((h.skill_home / "credentials" / "token.json").is_file(), "token.json was not cached")

    # 120 items = 3 playlistItems.list pages; 120 video ids = 3 videos.list batches.
    spent = h.quota() - before
    check(spent == 6 * QUOTA_LIST, f"fetch of 120 items should cost 6 units, spent {spent}")
    check(h.call_count("playlistItems.list") == 3, "expected exactly 3 paginated list calls")
    check(h.call_count("videos.list") == 3, "expected exactly 3 batched metadata calls")

    current = read_json(h.path("current.json"))
    check(len(current) == 120, "current.json item count mismatch")
    check(ids_of(current) == h.remote_ids(PL_MAIN), "current.json order differs from the remote order")
    check([r["position"] for r in current] == list(range(120)), "positions are not 0..119")
    hidden = [r for r in current if r["is_available"] is False]
    check(len(hidden) == 2, "private videos must be kept as placeholders, not dropped")
    check(all(r["title"] for r in current if r["is_available"]),
          "every available item should carry a title from videos.list")
    check(all(r["view_count"] > 0 for r in current if r["is_available"]), "view counts not enriched")
    return ["120 items over 3 pages, 6 quota units", "2 private videos preserved as pinned placeholders",
            "OAuth consent + token cache exercised"]


def s04_cache_behaviour(h: Harness) -> list[str]:
    """Phase 1: the second read is free; --refresh goes back to the API."""
    before_calls = h.call_count()
    cached = h.fetch(PL_MAIN, "cached.json", refresh=False)
    check(cached.last["source"] == "cache", f"second fetch should hit the cache, got {cached.last['source']}")
    check(h.call_count() == before_calls, "a cache hit must not call the API at all")
    check(read_json(h.path("cached.json")) == read_json(h.path("current.json")),
          "the cached snapshot differs from the freshly fetched one")
    check(len([p for p in cached.payloads if p.get("status") == "waiting_for_user"]) == 0,
          "a cached read re-ran the OAuth consent flow")

    refreshed = h.fetch(PL_MAIN, "current.json")
    check(refreshed.last["source"] == "api", "--refresh did not bypass the cache")
    check(h.call_count() == before_calls + 6, "--refresh should re-read 3 pages + 3 batches")
    check(len([p for p in refreshed.payloads if p.get("status") == "waiting_for_user"]) == 0,
          "the cached token was not reused — consent should only happen once")
    return ["cache hit = 0 units", "--refresh = 6 units", "token reused without re-consent"]


def s05_optimize_is_local(h: Harness) -> list[str]:
    """Phase 2 path A: grouping + within-group sort, provably without the API."""
    before = h.quota()
    aliases = h.path("artist_aliases.json")
    aliases.write_text(json.dumps({"Aurora": ["Aurora Borealis Project"]},
                                  ensure_ascii=False), encoding="utf-8")

    run = h.tool(
        "optimize", str(h.path("current.json")),
        "--target-out", str(h.path("target.json")),
        "--out", str(h.path("changes.json")),
        "--group-order", "first_appearance",
        "--within-group-sort", "viewCount:desc",
        "--aliases", str(aliases),
    )
    check(run.code == 0, f"optimize failed: {run.last}")
    report = run.last
    check(h.quota() == before, f"optimize must cost 0 API units, spent {h.quota() - before}")

    current = read_json(h.path("current.json"))
    target = read_json(h.path("target.json"))
    assert_permutation(current, target)
    pinned = assert_pinned_in_place(current, target)
    check(pinned == 2 == report["pinned_count"], f"pinned bookkeeping wrong: {report['pinned_count']}")

    groups = assert_grouped_and_sorted(target, current, "channel_title", "view_count")
    check(len(groups) == len(MAIN_ARTISTS),
          f"expected {len(MAIN_ARTISTS)} groups, target has {len(groups)}: {groups}")
    check(report["unresolved_count"] == 0,
          f"every item should resolve to an artist, {report['unresolved_count']} did not")
    check(sorted(report["groups_found"]) == sorted(a.lower() for a, _ in MAIN_ARTISTS),
          f"unexpected group keys: {report['groups_found']}")

    change_set = read_json(h.path("changes.json"))
    assert_change_set_shape(change_set, PL_MAIN, ids_of(current))
    changes = change_set["changes"]
    check(len(changes) == report["need_to_move"],
          f"report says {report['need_to_move']} moves, file has {len(changes)}")
    check(report["anchors"] + report["need_to_move"] == report["total_items"] == 120,
          f"anchors({report['anchors']}) + moves({report['need_to_move']}) != 120")
    check(report["estimated_quota"] == QUOTA_UPDATE * len(changes),
          f"estimated_quota {report['estimated_quota']} != 50 x {len(changes)}")
    # Naive baseline: one update call for every item not already sitting at its
    # final index (computed here from the two files, not read back from the tool).
    current_index = {r["playlist_item_id"]: i for i, r in enumerate(current)}
    naive_moves = sum(
        1 for final, record in enumerate(target)
        if current_index[record["playlist_item_id"]] != final
    )
    saved = QUOTA_UPDATE * (naive_moves - len(changes))
    check(report["quota_saved_vs_naive"] == saved,
          f"quota_saved_vs_naive {report['quota_saved_vs_naive']} != {saved} "
          f"(naive would move {naive_moves} of {len(current)} items)")
    check(saved > 0, "the LIS plan saved nothing over the naive baseline")

    # The invariant everything else rests on.
    check(replay(ids_of(current), changes) == ids_of(target),
          "replaying the plan against the current order does NOT produce the target order")

    lower_bound = 120 - lis_length(ids_of(current), ids_of(target))
    check(len(changes) >= lower_bound, f"fewer moves ({len(changes)}) than N-LIS ({lower_bound}) is impossible")
    return [f"{report['anchors']} anchors / {report['need_to_move']} moves, 0 API units",
            f"replay(current, changes) == target ({len(current)} items)",
            f"saved {report['quota_saved_vs_naive']} units vs a naive {naive_moves}-move plan"]


def s06_optimize_without_google_stack(h: Harness) -> list[str]:
    """Phase 2 path A: the '0 API units' claim holds even with no Google libs importable."""
    shim = h.workspace / "nogoogle"
    for package in ("googleapiclient", "google_auth_oauthlib"):
        pkg = shim / package
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text(
            "raise RuntimeError('E2E: the Google client stack must not be imported here')\n",
            encoding="utf-8",
        )
    run = h.tool(
        "optimize", str(h.path("current.json")),
        "--target-out", str(h.path("target_nogoogle.json")),
        "--out", str(h.path("changes_nogoogle.json")),
        "--group-order", "first_appearance",
        "--within-group-sort", "viewCount:desc",
        with_fake=False, skill_home=h.workspace / "empty_home", extra_path=[shim],
    )
    check(run.code == 0, f"optimize needs the Google stack / credentials: {run.stderr[-800:]}")
    check(read_json(h.path("target_nogoogle.json")) == read_json(h.path("target.json")),
          "optimize is not deterministic across runs")
    return ["optimize runs with googleapiclient un-importable and no credentials",
            "byte-identical target ordering across runs (deterministic)"]


def s07_preview_payload(h: Harness) -> list[str]:
    """Phase 3: the preview table the Agent must draw is backed by real data."""
    current = read_json(h.path("current.json"))
    target = read_json(h.path("target.json"))
    changes = read_json(h.path("changes.json"))["changes"]

    current_index = {r["playlist_item_id"]: i for i, r in enumerate(current)}
    target_index = {r["playlist_item_id"]: i for i, r in enumerate(target)}
    titles = {r["playlist_item_id"]: r["title"] for r in current}

    for change in changes:
        item = change["playlist_item_id"]
        check(change["old_position"] == current_index[item],
              f"old_position {change['old_position']} != real index {current_index[item]}")
        check(change["final_position"] == target_index[item],
              f"final_position {change['final_position']} != target index {target_index[item]}")
        check(change["title"] == titles[item], "change set carries a title that is not the item's title")

    drifting = [c for c in changes if c["new_position"] != c["final_position"]]
    check(drifting, "new_position never differs from final_position — the drift model looks broken")
    preview = changes[:20]
    check(all(c["title"] for c in preview), "preview rows must have titles to show the user")
    return [f"{len(changes)} rows verified against current/target indices",
            f"{len(drifting)} rows where API position != final position (drift is real)"]


def s08_update_write_back(h: Harness) -> list[str]:
    """Phase 4: sequential write-back lands the remote playlist exactly on target."""
    target_ids = ids_of(read_json(h.path("target.json")))
    changes = read_json(h.path("changes.json"))["changes"]
    before = h.quota()

    run = h.tool("update", PL_MAIN, str(h.path("changes.json")))
    check(run.code == 0, f"update failed: {run.last}")
    payload = run.last
    check(payload["status"] == "success", f"update not successful: {payload}")
    check(payload["successful"] == len(changes) == payload["total"],
          f"applied {payload['successful']} of {len(changes)} moves")
    check(payload["failed"] == 0, f"{payload['failed']} moves failed")
    expected_quota = 1 + QUOTA_UPDATE * len(changes)
    check(payload["quota_used"] == expected_quota,
          f"reported quota {payload['quota_used']} != 1 verify + 50 x {len(changes)}")
    check(h.quota() - before == expected_quota,
          f"real API spend {h.quota() - before} != reported {expected_quota}")

    check(h.remote_ids(PL_MAIN) == target_ids,
          "the remote playlist did NOT end up in the target order")
    progress = REPO / "scripts" / "logs" / f"progress_{PL_MAIN}.json"
    check(not progress.exists(), "the resume file must be cleared after a complete run")

    after = h.fetch(PL_MAIN, "after.json", refresh=False)
    check(after.last["source"] == "api", "the cache was not invalidated by the write-back")
    check(ids_of(read_json(h.path("after.json"))) == target_ids,
          "a fresh fetch does not agree with the target order")
    return [f"{len(changes)} moves applied, {expected_quota} units",
            "remote order == target order (verified through a fresh fetch)",
            "resume file cleared, cache invalidated"]


def s09_double_apply_is_refused(h: Harness) -> list[str]:
    """Phase 4: re-running a spent change set cannot scramble the playlist."""
    before_ids = h.remote_ids(PL_MAIN)
    before = h.quota()
    run = h.tool("update", PL_MAIN, str(h.path("changes.json")))
    check(run.code == 1, "re-applying a finished change set must fail loudly")
    check(run.last.get("code") == "STALE_SNAPSHOT", f"expected STALE_SNAPSHOT, got {run.last}")
    check(h.remote_ids(PL_MAIN) == before_ids, "the refused re-run still moved items")
    check(h.quota() - before == QUOTA_LIST, f"a refused run should cost 1 unit, cost {h.quota() - before}")
    return ["second apply refused with STALE_SNAPSHOT for 1 unit, zero writes"]


def s10_stale_remote(h: Harness) -> list[str]:
    """Phase 4: a playlist edited on YouTube behind the plan is refused before any write."""
    h.install_credentials()
    fetched = h.fetch(PL_STALE, "stale_current.json")
    check(fetched.code == 0, f"fetch failed: {fetched.last}")
    plan = h.tool("optimize", str(h.path("stale_current.json")),
                  "--target-out", str(h.path("stale_target.json")),
                  "--out", str(h.path("stale_changes.json")),
                  "--within-group-sort", "viewCount:desc")
    check(plan.code == 0, f"optimize failed: {plan.last}")

    # Someone reorders the playlist in the YouTube UI.
    state = h.state
    playlist = state["playlists"][PL_STALE]
    playlist.insert(0, playlist.pop(5))
    h.write_state(state)
    drifted = h.remote_ids(PL_STALE)

    before = h.quota()
    run = h.tool("update", PL_STALE, str(h.path("stale_changes.json")))
    check(run.code == 1, "a stale snapshot must exit non-zero")
    check(run.last.get("code") == "STALE_SNAPSHOT", f"expected STALE_SNAPSHOT, got {run.last}")
    check("expected_head_fingerprint" in run.last and "remote_head_fingerprint" in run.last,
          "STALE_SNAPSHOT should report both fingerprints for diagnosis")
    check(h.remote_ids(PL_STALE) == drifted, "items were moved despite the stale snapshot")
    check(h.quota() - before == QUOTA_LIST,
          f"detection should cost exactly 1 unit, cost {h.quota() - before}")
    return ["remote drift detected for 1 unit before any 50-unit write"]


def s11_retry_abort_resume(h: Harness) -> list[str]:
    """Phase 4: transient errors retry, fatal ones abort mid-plan, and resume finishes."""
    fetched = h.fetch(PL_RESUME, "resume_current.json")
    check(fetched.code == 0, f"fetch failed: {fetched.last}")
    plan = h.tool("optimize", str(h.path("resume_current.json")),
                  "--target-out", str(h.path("resume_target.json")),
                  "--out", str(h.path("resume_changes.json")),
                  "--within-group-sort", "viewCount:desc")
    check(plan.code == 0, f"optimize failed: {plan.last}")
    changes = read_json(h.path("resume_changes.json"))["changes"]
    target_ids = ids_of(read_json(h.path("resume_target.json")))
    check(len(changes) >= 6, f"need a plan with enough moves to interrupt, got {len(changes)}")

    abort_at = 3
    h.arm_fault("playlistItems.update", PL_RESUME, when_applied=1, status=503,
                reason="backendError", remaining=2)   # retried, then succeeds
    h.arm_fault("playlistItems.update", PL_RESUME, when_applied=abort_at, status=403,
                reason="quotaExceeded", remaining=1)  # fatal, must stop the run

    before = h.quota()
    first = h.tool("update", PL_RESUME, str(h.path("resume_changes.json")))
    check(first.code == 1, "a quota-exhausted run must exit non-zero")
    payload = first.last
    check(payload["status"] == "partial", f"expected a partial result, got {payload}")
    check(payload.get("code") == "QUOTA_EXCEEDED", f"expected QUOTA_EXCEEDED, got {payload.get('code')}")
    check(payload["successful"] == abort_at,
          f"should have landed exactly {abort_at} moves, reported {payload['successful']}")
    check(payload["resume_from"] == abort_at, f"resume_from should be {abort_at}, got {payload['resume_from']}")
    check(h.quota() - before == QUOTA_LIST + QUOTA_UPDATE * abort_at,
          f"unexpected spend on the aborted run: {h.quota() - before}")

    progress_file = REPO / "scripts" / "logs" / f"progress_{PL_RESUME}.json"
    check(progress_file.is_file(), "no resume file was written after the abort")
    progress = read_json(progress_file)
    check(progress["completed"] == abort_at, f"resume file says {progress['completed']}, expected {abort_at}")
    check(progress["fingerprint"] == read_json(h.path("resume_changes.json"))["fingerprint"],
          "the resume file is not bound to this change set's fingerprint")

    partial_ids = replay(ids_of(read_json(h.path("resume_current.json"))), changes[:abort_at])
    check(h.remote_ids(PL_RESUME) == partial_ids,
          "the half-applied playlist does not match the replay of the applied moves")

    # Tomorrow: same command, same file.
    h.clear_faults()
    before = h.quota()
    second = h.tool("update", PL_RESUME, str(h.path("resume_changes.json")))
    check(second.code == 0, f"resume failed: {second.last}")
    resumed = second.last
    check(resumed["status"] == "success", f"resume did not complete: {resumed}")
    check(resumed["successful"] == len(changes), f"resume applied {resumed['successful']}/{len(changes)}")
    remaining = len(changes) - abort_at
    check(h.quota() - before == QUOTA_LIST + QUOTA_UPDATE * remaining,
          f"resume should only pay for the remaining {remaining} moves, paid {h.quota() - before}")
    check(h.remote_ids(PL_RESUME) == target_ids, "the resumed playlist is not in the target order")
    check(not progress_file.exists(), "the resume file survived a completed run")
    return [f"503 retried twice then succeeded; quota abort at move {abort_at}",
            f"resume paid only for the remaining {remaining} moves and hit the target order"]


def s12_typescript_engine(h: Harness) -> list[str]:
    """Phase 2 path C: the cognitive engine plans, `diff` costs it out, `update` lands it."""
    h.ensure_node_build()
    fetched = h.fetch(PL_CLEAN, "clean_current.json")
    check(fetched.code == 0, f"fetch failed: {fetched.last}")
    current = read_json(h.path("clean_current.json"))

    intent = h.node("-i", str(h.path("clean_current.json")), "-o", str(h.path("clean_new.json")),
                    "--intent", "把同一個頻道的影片放在一起，觀看次數由高到低")
    check(intent.code == 0, f"the engine failed on the documented intent: {intent.last}")
    plan = intent.last
    check(plan["status"] == "success", f"engine error: {plan}")
    check("channel_title" in plan["group_dimensions"],
          f"the intent 「同一個頻道」 did not produce a channel_title grouping: {plan['group_dimensions']}")
    check("view_count:desc" in plan["sort_criteria"],
          f"the intent 「觀看次數由高到低」 did not produce view_count:desc: {plan['sort_criteria']}")
    check(plan["is_continuous"] is True, f"the engine reports {plan['gap_count']} unrepaired gaps")
    check(plan["item_count"] == len(current), "the engine changed the item count")

    nl_target = read_json(h.path("clean_new.json"))
    assert_permutation(current, nl_target)
    assert_grouped_and_sorted(nl_target, current, "channel_title", "view_count")

    explicit = h.node("-i", str(h.path("clean_current.json")), "-o", str(h.path("clean_new2.json")),
                      "--group-by", "channel_title", "--sort-by", "view_count:desc")
    check(explicit.code == 0, f"explicit flags failed: {explicit.last}")
    check(read_json(h.path("clean_new2.json")) == nl_target,
          "--group-by/--sort-by disagrees with the equivalent natural-language intent")

    bad = h.node("-i", str(h.path("clean_current.json")), "-o", str(h.path("nope.json")), "--wat")
    check(bad.code != 0 and bad.last.get("code") == "UNKNOWN_FLAG",
          f"an unknown flag should fail with UNKNOWN_FLAG, got {bad.last}")

    before = h.quota()
    diffed = h.tool("diff", str(h.path("clean_current.json")), str(h.path("clean_new.json")),
                    "--out", str(h.path("clean_changes.json")))
    check(diffed.code == 0, f"diff failed: {diffed.last}")
    check(h.quota() == before, "diff must cost 0 API units")
    change_set = read_json(h.path("clean_changes.json"))
    assert_change_set_shape(change_set, PL_CLEAN, ids_of(current))
    changes = change_set["changes"]
    check(diffed.last["changes_count"] == len(changes), "diff reported a different move count")
    check(diffed.last["estimated_quota"] == QUOTA_UPDATE * len(changes), "diff quota estimate is wrong")
    check(replay(ids_of(current), changes) == ids_of(nl_target),
          "replaying the diff plan does not reproduce the engine's ordering")

    # No pinned items here, so the plan must hit the theoretical minimum exactly.
    lower_bound = len(current) - lis_length(ids_of(current), ids_of(nl_target))
    check(len(changes) == lower_bound,
          f"plan uses {len(changes)} moves, the N-LIS minimum is {lower_bound}")

    applied = h.tool("update", PL_CLEAN, str(h.path("clean_changes.json")))
    check(applied.code == 0, f"update failed: {applied.last}")
    check(h.remote_ids(PL_CLEAN) == ids_of(nl_target),
          "the remote playlist does not match what the cognitive engine planned")
    return ["Chinese intent → channel_title grouping + view_count:desc",
            "explicit flags produce a byte-identical ordering",
            f"diff plan is exactly N-LIS minimal ({len(changes)} moves) and lands on target"]


def s13_target_mismatch(h: Harness) -> list[str]:
    """Path B guard: a target that is not a permutation is refused, not half-applied."""
    current = read_json(h.path("clean_current.json"))
    broken = h.path("broken_new.json")

    dropped = [r for r in current if r is not current[3]]
    broken.write_text(json.dumps(dropped, ensure_ascii=False), encoding="utf-8")
    run = h.tool("diff", str(h.path("clean_current.json")), str(broken),
                 "--out", str(h.path("broken_changes.json")))
    check(run.code == 1 and run.last.get("code") == "TARGET_MISMATCH",
          f"a dropped item should be TARGET_MISMATCH, got {run.last}")

    duplicated = list(current) + [current[0]]
    broken.write_text(json.dumps(duplicated, ensure_ascii=False), encoding="utf-8")
    run = h.tool("diff", str(h.path("clean_current.json")), str(broken),
                 "--out", str(h.path("broken_changes.json")))
    check(run.code == 1 and run.last.get("code") == "TARGET_MISMATCH",
          f"a duplicated item should be TARGET_MISMATCH, got {run.last}")

    garbage = h.path("garbage.json")
    garbage.write_text("{ not json at all", encoding="utf-8")
    run = h.tool("diff", str(h.path("clean_current.json")), str(garbage),
                 "--out", str(h.path("broken_changes.json")))
    check(run.code == 1 and run.last.get("code") == "PARSE_ERROR",
          f"unparseable input should be PARSE_ERROR, got {run.last}")
    check(not h.path("broken_changes.json").exists(), "a rejected diff still wrote a change set")
    return ["dropped item / duplicated item → TARGET_MISMATCH", "malformed JSON → PARSE_ERROR"]


def s14_change_set_guards(h: Harness) -> list[str]:
    """Phase 4 guards: legacy files, foreign playlists and tampered files are refused."""
    legacy = h.path("legacy_changes.json")
    legacy.write_text(json.dumps([
        {"playlist_item_id": "pli00001", "video_id": "vid00001", "old_position": 5,
         "new_position": 0, "final_position": 0}
    ]), encoding="utf-8")
    before = h.quota()
    run = h.tool("update", PL_SORTED, str(legacy))
    check(run.code == 1 and run.last.get("code") == "LEGACY_CHANGE_SET",
          f"a v1 change set must be refused, got {run.last}")

    foreign = read_json(h.path("clean_changes.json"))
    foreign_path = h.path("foreign_changes.json")
    foreign_path.write_text(json.dumps(foreign, ensure_ascii=False), encoding="utf-8")
    run = h.tool("update", PL_SORTED, str(foreign_path))
    check(run.code == 1 and run.last.get("code") == "MISMATCHED_PLAYLIST",
          f"a change set for another playlist must be refused, got {run.last}")

    tampered = read_json(h.path("clean_changes.json"))
    if len(tampered["changes"]) >= 2:
        tampered["changes"][1]["execution_order"] = 99
    tampered_path = h.path("tampered_changes.json")
    tampered_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    run = h.tool("update", PL_CLEAN, str(tampered_path))
    check(run.code == 1 and run.last.get("code") == "CORRUPT_CHANGE_SET",
          f"a hand-edited execution_order must be refused, got {run.last}")

    unsupported = {"version": 99, "playlist_id": PL_CLEAN, "changes": []}
    unsupported_path = h.path("unsupported_changes.json")
    unsupported_path.write_text(json.dumps(unsupported), encoding="utf-8")
    run = h.tool("update", PL_CLEAN, str(unsupported_path))
    check(run.code == 1 and run.last.get("code") == "UNSUPPORTED_CHANGE_SET",
          f"an unknown version must be refused, got {run.last}")

    check(h.quota() == before, "a refused change set must not touch the API at all")
    return ["v1 / foreign playlist / tampered order / unknown version all refused",
            "none of them spent a single API unit"]


def s15_no_op_playlist(h: Harness) -> list[str]:
    """The already-sorted case: nothing to do, and nothing is done."""
    fetched = h.fetch(PL_SORTED, "sorted_current.json")
    check(fetched.code == 0, f"fetch failed: {fetched.last}")
    run = h.tool("optimize", str(h.path("sorted_current.json")),
                 "--target-out", str(h.path("sorted_target.json")),
                 "--out", str(h.path("sorted_changes.json")),
                 "--within-group-sort", "viewCount:desc")
    check(run.code == 0, f"optimize failed: {run.last}")
    report = run.last
    check(report["need_to_move"] == 0, f"an already-sorted playlist needs {report['need_to_move']} moves")
    check(report["estimated_quota"] == 0, "a no-op plan should estimate 0 units")
    check(report["anchors"] == report["total_items"], "every item should be an anchor")
    check(read_json(h.path("sorted_target.json")) == read_json(h.path("sorted_current.json")),
          "the target of a sorted playlist should equal the input")

    before_ids = h.remote_ids(PL_SORTED)
    before = h.quota()
    applied = h.tool("update", PL_SORTED, str(h.path("sorted_changes.json")))
    check(applied.code == 0, f"a no-op update should succeed: {applied.last}")
    check(applied.last["quota_used"] == 0, f"a no-op update spent {applied.last['quota_used']} units")
    check(h.quota() == before, "a no-op update called the API")
    check(h.remote_ids(PL_SORTED) == before_ids, "a no-op update reordered the playlist")
    return ["0 moves, 0 estimated units, 0 API calls, playlist untouched"]


def s16_bad_arguments(h: Harness) -> list[str]:
    """Operator error is reported as structured JSON, not a traceback."""
    run = h.tool("optimize", str(h.path("current.json")),
                 "--target-out", str(h.path("x.json")), "--out", str(h.path("y.json")),
                 "--within-group-sort", "bogusField:desc")
    check(run.code == 1 and run.last.get("code") == "INVALID_SORT_FIELD",
          f"expected INVALID_SORT_FIELD, got {run.last}")
    check("viewCount" in run.last.get("message", ""), "the error should list the valid fields")

    run = h.tool("optimize", str(h.path("current.json")),
                 "--target-out", str(h.path("x.json")), "--out", str(h.path("y.json")),
                 "--within-group-sort", "viewCount:sideways")
    check(run.code == 1 and run.last.get("code") == "INVALID_SORT_ORDER",
          f"expected INVALID_SORT_ORDER, got {run.last}")

    missing = h.tool("optimize", str(h.path("does_not_exist.json")),
                     "--target-out", str(h.path("x.json")), "--out", str(h.path("y.json")))
    check(missing.code == 1 and missing.last.get("code") == "PARSE_ERROR",
          f"a missing input file should be PARSE_ERROR, got {missing.last}")

    empty = h.path("empty.json")
    empty.write_text("[]", encoding="utf-8")
    run = h.tool("optimize", str(empty), "--target-out", str(h.path("x.json")),
                 "--out", str(h.path("y.json")))
    check(run.code == 1 and run.last.get("code") == "EMPTY_PLAYLIST",
          f"an empty playlist should be EMPTY_PLAYLIST, got {run.last}")

    url = h.tool("fetch", "https://www.youtube.com/playlist?list=" + PL_SORTED,
                 "--out", str(h.path("via_url.json")), "--refresh")
    check(url.code == 0, f"a full playlist URL should be accepted: {url.last}")
    check(ids_of(read_json(h.path("via_url.json"))) == h.remote_ids(PL_SORTED),
          "the URL form fetched a different playlist")
    return ["4 argument errors return structured JSON codes", "playlist URL form accepted"]


SCENARIOS: list[Scenario] = [
    Scenario("entry_points", "Phase 0", "Agent entry points and CLI surface", s01_entry_points),
    Scenario("credentials", "Phase 0", "Credential setup handshake", s02_credentials_gate),
    Scenario("fetch", "Phase 1", "Fetch: pagination, batching, private videos", s03_fetch_pagination),
    Scenario("cache", "Phase 1", "Cache hit vs --refresh, token reuse", s04_cache_behaviour),
    Scenario("optimize", "Phase 2A", "Optimize: grouping, sorting, minimal plan", s05_optimize_is_local),
    Scenario("offline", "Phase 2A", "Optimize really is offline and deterministic", s06_optimize_without_google_stack),
    Scenario("preview", "Phase 3", "Preview payload matches reality", s07_preview_payload),
    Scenario("update", "Phase 4", "Write-back reaches the target order", s08_update_write_back),
    Scenario("double_apply", "Phase 4", "A spent change set cannot be replayed", s09_double_apply_is_refused),
    Scenario("stale", "Phase 4", "Remote drift is caught before any write", s10_stale_remote),
    Scenario("resume", "Phase 4", "Retry, fatal abort, and exact resume", s11_retry_abort_resume),
    Scenario("engine", "Phase 2C", "TypeScript cognitive engine → diff → update", s12_typescript_engine),
    Scenario("mismatch", "Phase 2B", "Invalid target orderings are refused", s13_target_mismatch),
    Scenario("guards", "Phase 4", "Change-set integrity guards", s14_change_set_guards),
    Scenario("noop", "Phase 2A", "Already-sorted playlist is a true no-op", s15_no_op_playlist),
    Scenario("arguments", "All", "Operator errors are structured, URLs accepted", s16_bad_arguments),
]


# ─── Runner ──────────────────────────────────────────────────────


def cleanup_side_effects() -> None:
    """Remove cache/progress files this suite created inside the repo."""
    for directory, pattern in (
        (REPO / "scripts" / "cache", "playlist_PLE2E*.json"),
        (REPO / "scripts" / "logs", "progress_PLE2E*.json"),
    ):
        if directory.is_dir():
            for path in directory.glob(pattern):
                path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end suite for the playlist skill")
    parser.add_argument("-k", "--filter", default="",
                        help="comma-separated scenario keys to run (substring match)")
    parser.add_argument("--keep", action="store_true", help="keep the temp workspace")
    args = parser.parse_args()

    # The report contains Chinese error text; never let a cp950 console kill the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    keys = [k.strip() for k in args.filter.split(",") if k.strip()]
    selected = [s for s in SCENARIOS if any(k in s.key for k in keys)] if keys else SCENARIOS
    if not selected:
        print(f"no scenario matches {args.filter!r}")
        return 2

    cleanup_side_effects()
    workspace = Path(tempfile.mkdtemp(prefix="yt_e2e_"))
    harness = Harness(workspace)

    print("=" * 78)
    print(" YouTube Playlist Agent-Skill — End-to-End Suite")
    print("=" * 78)
    print(f" repo      : {REPO}")
    print(f" workspace : {workspace}")
    print(f" python    : {sys.version.split()[0]}")
    print(f" scenarios : {len(selected)}")
    print("=" * 78)

    outcomes: list[Outcome] = []
    for index, scenario in enumerate(selected, start=1):
        started = time.perf_counter()
        label = f"[{index:2d}/{len(selected)}] {scenario.phase:<8} {scenario.title}"
        print(f"\n{label}")
        try:
            details = scenario.fn(harness)
            outcome = Outcome(scenario, True, time.perf_counter() - started, details)
            print(f"    PASS  ({outcome.seconds:.1f}s)")
            for line in details:
                print(f"      · {line}")
        except Exception as exc:  # noqa: BLE001 — every failure mode is a test result
            outcome = Outcome(scenario, False, time.perf_counter() - started,
                              error=f"{type(exc).__name__}: {exc}")
            print(f"    FAIL  ({outcome.seconds:.1f}s)")
            for line in str(exc).splitlines():
                print(f"      ! {line}")
        outcomes.append(outcome)

    passed = [o for o in outcomes if o.passed]
    failed = [o for o in outcomes if not o.passed]
    state = harness.state

    print("\n" + "=" * 78)
    print(" Summary")
    print("=" * 78)
    for outcome in outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        print(f" {mark}  {outcome.scenario.phase:<8} {outcome.scenario.title}")
    print("-" * 78)
    print(f" scenarios      : {len(passed)}/{len(outcomes)} passed")
    print(f" simulated quota: {state.get('quota_used', 0)} units "
          f"({len([c for c in state.get('calls', []) if c['op'] == 'playlistItems.update'])} writes, "
          f"{len([c for c in state.get('calls', []) if c['op'].endswith('.list')])} reads)")
    print(f" oauth consents : {state.get('oauth_consent_runs', 0)}")
    print(f" real API calls : 0 (network boundary faked)")
    print("=" * 78)

    if args.keep:
        print(f"workspace kept at {workspace}")
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    cleanup_side_effects()

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
