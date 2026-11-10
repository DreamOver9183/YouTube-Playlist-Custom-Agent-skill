"""
review_parity.py — Fixtures and a cost cross-check for the bookshelf review page.

**Developer tool, not part of the skill.**  It writes only under ``--out``.

The review page computes its live cost (moves / Spotify calls) in JavaScript.
This tool makes that number checkable against the Python planner, which is the
one that actually writes to the API:

1. Builds two playlists in the shared item format:
   - ``yt_current.json``: the first 60 items of ``tests/fixtures/UserTest_Task3-1.json``,
     item 6 turned into a private (pinned) video, plus an item with no title or
     channel (lands in ``unknown``) and one with duration 0;
   - ``sp_current.json``: Spotify entries built with ``tests/e2e/fake_spotify.track``,
     plus a song with no artist (``unknown``), a 0-second track and a 2-hour episode.
   Every item gets an https cover URL, so the info card has something to show.
2. Generates permutations (49 Spotify, 12 YouTube with the pin kept in place) and
   records what ``plan_reorder`` / ``plan_range_reorder`` say they cost.
3. Writes ``parity_sp.js`` / ``parity_yt.js``: paste one into the matching review
   page (or run it through the browser tool); it evaluates ``window.__review.costs``
   on every permutation and returns ``{checked, mismatches}``.

Usage::

    python tests/dev/review_parity.py --out <dir>
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "tests" / "e2e"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import fake_spotify  # noqa: E402
from scripts.optimizer import normalize_target, plan_range_reorder, plan_reorder  # noqa: E402
from scripts.schemas import EnrichedPlaylistItem  # noqa: E402
from scripts.spotify_api import entries_to_items  # noqa: E402

ARTISTS = [("ar_aimer", "Aimer"), ("ar_yoasobi", "YOASOBI"), ("ar_kinggnu", "King Gnu"), ("ar_ado", "Ado")]
ALBUMS = {"ar_aimer": ("al_aimer", "Walpurgis"), "ar_yoasobi": ("al_yoa", "THE BOOK"),
          "ar_kinggnu": ("al_kg", "CEREMONY"), "ar_ado": ("al_ado", "Kyogen")}


def youtube_items() -> list[EnrichedPlaylistItem]:
    raw = json.loads((ROOT / "tests" / "fixtures" / "UserTest_Task3-1.json").read_text(encoding="utf-8"))["items"][:60]
    for i, record in enumerate(raw):
        record["position"] = i
        record["image_url"] = f"https://i.ytimg.com/vi/{record['video_id']}/mqdefault.jpg"
    raw[5]["is_available"] = False
    raw[5]["title"] = ""
    raw[11].update(title="", channel_title="", metadata_available=False)  # nothing to identify → unknown
    raw[17]["duration_seconds"] = 0
    return [EnrichedPlaylistItem.model_validate(r) for r in raw]


def spotify_items() -> list[EnrichedPlaylistItem]:
    rng = random.Random(42)
    entries = []
    for serial in range(24):
        artist = rng.choice(ARTISTS)
        entry = fake_spotify.track(serial, artist, ALBUMS[artist[0]], number=rng.randint(1, 12))
        entry["item"]["album"]["images"] = [
            {"url": f"https://i.scdn.co/image/fake-{serial}-640", "width": 640},
            {"url": f"https://i.scdn.co/image/fake-{serial}-300", "width": 300},
        ]
        entries.append(entry)
    entries[3]["item"]["artists"] = []          # no artist → unknown group
    entries[7]["item"]["duration_ms"] = 0       # duration 0 → default spine width, "—"
    entries.append({"added_at": "2026-02-02T00:00:00Z", "item": {
        "id": "ep1", "uri": "spotify:episode:ep1", "name": "A Two-Hour Episode", "type": "episode",
        "duration_ms": 7_200_000, "release_date": "2026-01-15",
        "show": {"id": "sh1", "name": "Some Podcast"},
        "images": [{"url": "https://i.scdn.co/image/fake-episode", "width": 300}]}})
    return entries_to_items(entries, "PLparity")


def permutations(items: list[EnrichedPlaylistItem], count: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    movable = [i for i, item in enumerate(items) if item.is_available]
    pinned = [i for i, item in enumerate(items) if not item.is_available]
    out = []
    while len(out) < count:
        order = list(movable)
        if len(out) % 3 == 0:
            rng.shuffle(order)
        else:
            cuts = sorted(rng.sample(range(1, len(order)), k=rng.randint(1, 5)))
            blocks = [order[a:b] for a, b in zip([0] + cuts, cuts + [len(order)])]
            rng.shuffle(blocks)
            order = [i for block in blocks for i in block]
        target = normalize_target(items, [items[i] for i in order] + [items[i] for i in pinned])
        index = {item.playlist_item_id: i for i, item in enumerate(items)}
        perm = [index[item.playlist_item_id] for item in target]
        if perm not in out:
            out.append(perm)
    return out


def parity_js(cases: list[list], with_calls: bool) -> str:
    return (
        "(() => {\n"
        f"  const cases = {json.dumps(cases, separators=(',', ':'))};\n"
        "  const snap = JSON.parse(document.getElementById('review-data').textContent).items.map((i) => i.key);\n"
        "  const bad = [];\n"
        "  for (const c of cases) {\n"
        "    const got = window.__review.costs(c[0].map((i) => snap[i]));\n"
        f"    if (got.moves !== c[1]{' || got.calls !== c[2]' if with_calls else ''}) "
        "bad.push({ perm: c[0].join(','), expected: c.slice(1), got: [got.moves, got.calls] });\n"
        "  }\n"
        "  return JSON.stringify({ checked: cases.length, mismatches: bad.length, bad: bad.slice(0, 3) });\n"
        "})()\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    yt, sp = youtube_items(), spotify_items()
    for name, items in (("yt_current.json", yt), ("sp_current.json", sp)):
        (out / name).write_text(json.dumps([i.model_dump(mode="json") for i in items], ensure_ascii=False, indent=1),
                                encoding="utf-8")

    sp_cases = []
    for perm in permutations(sp, 49, seed=7):
        target = [sp[i] for i in perm]
        sp_cases.append([perm, len(plan_reorder(sp, target)[0]), plan_range_reorder(sp, target)[1]["calls"]])
    yt_cases = [[perm, len(plan_reorder(yt, [yt[i] for i in perm])[0])] for perm in permutations(yt, 12, seed=3)]

    (out / "cases.json").write_text(json.dumps({"spotify": sp_cases, "youtube": yt_cases}), encoding="utf-8")
    (out / "parity_sp.js").write_text(parity_js(sp_cases, with_calls=True), encoding="utf-8")
    (out / "parity_yt.js").write_text(parity_js(yt_cases, with_calls=False), encoding="utf-8")
    print(json.dumps({"status": "success", "out": str(out), "spotify_items": len(sp), "youtube_items": len(yt),
                      "spotify_cases": len(sp_cases), "youtube_cases": len(yt_cases)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
