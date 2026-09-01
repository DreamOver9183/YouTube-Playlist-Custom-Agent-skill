"""
build_task3_samples.py — 把 Task 3 樣本轉成 `yt-playlist-sample/v1` 並驗證

**這是開發者工具，不是 skill 的一部分。** 擋門與 ``learn_scenarios.py`` 相同：
程式放在 ``tests/dev/``、需要專用進入點、沒有設 ``YT_DEV_SAMPLE=1`` 就完全不寫檔。

格式定義見 ``docs/SAMPLE_SCHEMA.md``。這支程式是那份文件的可執行版本。

它在做什麼
----------

``UserTest_Task3-1.md`` 是這份測試集的**唯一來源**。這支工具把它解析成
``EnrichedPlaylistItem`` —— 與 ``yt_tool fetch --out`` 同一種東西 —— 任何工具都能
直接吃，不需要轉接層。

方向只有一個：``.md`` → 樣本 JSON。刻意不從舊版 JSON 讀，因為那份 JSON 本身就是
這個轉換的產物；拿產物當來源會讓整條鏈在來源被覆蓋後無法重跑（實際發生過）。

四份隨機版由基準版重新洗牌產生。``random.Random(seed).shuffle()`` 只用到索引，
與元素內容無關，所以換了格式之後**排列完全不變**，先前報告中的 Jaccard 與
Kendall 係數依然成立。

用法
----

.. code-block:: bash

    python tests/dev/build_task3_samples.py              # 唯讀試跑
    YT_DEV_SAMPLE=1 python tests/dev/build_task3_samples.py   # 實際寫檔
    python tests/dev/build_task3_samples.py --validate   # 只驗證現有樣本
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "User Testing"
SCHEMA = "yt-playlist-sample/v1"

#: 洗牌種子。random.shuffle 只用索引，與元素內容無關，換格式後排列不變。
SEEDS = {"3-2": 42, "3-3": 108, "3-4": 256, "3-5": 777}

#: .md 清冊沒有記錄的欄位。填法見 docs/SAMPLE_SCHEMA.md〈合成欄位〉。
SYNTHETIC_FIELDS = ["added_at", "comment_count", "tags"]

WRITE_ENABLED = os.environ.get("YT_DEV_SAMPLE") == "1"


def parse_duration(text: str) -> int:
    """``"03:14"`` / ``"1:02:03"`` → 秒數。"""
    parts = [int(p) for p in text.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


#: .md 曲目表的欄位數（含首尾空欄）。少一欄多一欄都代表格式變了或標題含 `|`。
_TRACK_COLUMNS = 12


def parse_md(path: Path) -> tuple[dict, list[dict]]:
    """解析 `.md` 清冊，回傳 ``(source, rows)``。

    `.md` 是這條鏈的**唯一來源**。刻意不從舊版 JSON 讀 —— 那份 JSON 本身就是
    這個轉換的產物，拿產物當來源會讓整條鏈無法重跑（實際發生過，見
    docs/BACKLOG.md 第 3 條）。
    """
    lines = path.read_text(encoding="utf-8").splitlines()

    source: dict[str, str] = {}
    for line in lines:
        if line.startswith("- **播放清單名稱**："):
            source["playlist_name"] = line.split("：", 1)[1].strip(" `")
        elif line.startswith("- **播放清單 ID**："):
            source["playlist_id"] = line.split("：", 1)[1].strip(" `")
        elif line.startswith("- **清單連結**："):
            match = re.search(r"\[YouTube\]\((https?://[^)]+)\)", line)
            if match:
                source["playlist_url"] = match.group(1)
        elif line.startswith("- **資料擷取時間**："):
            raw = line.split("：", 1)[1].strip(" `")
            # .md 的時間沒有時區，實測資料是 UTC+8。
            source["fetched_at"] = raw.replace(" ", "T") + "+08:00"

    declared: dict[str, int] = {}
    for line in lines:
        for label, key in (("累計總觀看次數", "total_views"), ("累計總按讚數", "total_likes")):
            if line.startswith(f"- **{label}**："):
                declared[key] = int(line.split("：", 1)[1].strip(" `次").replace(",", ""))

    missing = {"playlist_name", "playlist_id", "playlist_url", "fetched_at"} - source.keys()
    if missing:
        sys.exit(f"{path.name} 缺少必要的 Overview 欄位：{sorted(missing)}")

    rows: list[dict] = []
    for line in lines:
        if not line.startswith("| ") or "|---" in line:
            continue
        cells = line.split("|")
        if len(cells) != _TRACK_COLUMNS:
            continue
        cells = [c.strip() for c in cells]
        if not cells[1].isdigit():   # 表頭或其他表格
            continue
        rows.append({
            "title": cells[2],
            "channel": cells[3],
            "duration": cells[4],
            "views": int(cells[5].replace(",", "")),
            "likes": int(cells[6].replace(",", "")),
            "published_date": cells[7],
            "video_id": cells[9].strip("`"),
            "playlist_item_id": cells[10].strip("`"),
        })

    declared_tracks = next(
        (int(m.group(1)) for line in lines
         if (m := re.match(r"- \*\*總曲目數\*\*：`(\d+)`", line))),
        None,
    )
    if declared_tracks is not None and declared_tracks != len(rows):
        sys.exit(f"{path.name} 宣告 {declared_tracks} 首，實際解析出 {len(rows)} 首 —— 表格格式可能已變。")

    # Overview 的總計必須等於逐首加總。這是 .md 有沒有被解析錯的最強檢查：
    # 少解析一列、把某欄位讀錯，都會在這裡被抓出來。
    for key, actual in (("total_views", sum(r["views"] for r in rows)),
                        ("total_likes", sum(r["likes"] for r in rows))):
        if key in declared and declared[key] != actual:
            sys.exit(f"{path.name} 的 {key} 宣告 {declared[key]:,}，逐首加總為 {actual:,} —— 解析有誤。")

    return source, rows


def to_enriched(row: dict, index: int, playlist_id: str, fetched_at: str) -> dict:
    """`.md` 的一列 → EnrichedPlaylistItem 的 dict 形式。"""
    return {
        "playlist_item_id": row["playlist_item_id"],
        "video_id": row["video_id"],
        # .md 的 # 欄是 1-based；EnrichedPlaylistItem.position 與 YouTube API 一致
        # 是 0-based。這裡用陣列索引而非該欄，因為洗牌後要重編。
        "position": index,
        # 合成：.md 沒有記錄加入時間，全部填成抓取時間（常數）。
        "added_at": fetched_at,
        "playlist_id": playlist_id,
        "title": row["title"],
        "channel_title": row["channel"],
        "published_at": f'{row["published_date"]}T00:00:00+00:00',
        "duration_seconds": parse_duration(row["duration"]),
        "view_count": row["views"],
        "like_count": row["likes"],
        "comment_count": 0,       # 合成
        "tags": [],               # 合成
        "privacy_status": "public",
        "is_available": True,
        "metadata_available": True,
    }


def build() -> dict[str, dict]:
    """由 `.md` 清冊建出五份 canonical 樣本（純計算，不寫檔）。"""
    source, rows = parse_md(DATA_DIR / "UserTest_Task3-1.md")
    playlist_id, fetched_at = source["playlist_id"], source["fetched_at"]

    base_items = [
        to_enriched(row, i, playlist_id, fetched_at) for i, row in enumerate(rows)
    ]

    samples = {
        "3-1": {
            "schema": SCHEMA,
            "source": source,
            "synthetic_fields": SYNTHETIC_FIELDS,
            "item_count": len(base_items),
            "items": base_items,
        }
    }

    for suffix, seed in SEEDS.items():
        shuffled = copy.deepcopy(base_items)
        random.Random(seed).shuffle(shuffled)
        for i, item in enumerate(shuffled):
            item["position"] = i
        samples[suffix] = {
            "schema": SCHEMA,
            "source": source,
            "derivation": {
                "base": "UserTest_Task3-1",
                "method": "random.Random(seed).shuffle",
                "seed": seed,
            },
            "synthetic_fields": SYNTHETIC_FIELDS,
            "item_count": len(shuffled),
            "items": shuffled,
        }
    return samples


def validate(sample: dict, name: str) -> list[str]:
    """依 docs/SAMPLE_SCHEMA.md 檢查一份樣本，回傳問題清單。"""
    problems: list[str] = []
    if sample.get("schema") != SCHEMA:
        problems.append(f"schema 應為 {SCHEMA}，實際為 {sample.get('schema')!r}")

    for key in ("playlist_id", "playlist_name", "playlist_url", "fetched_at"):
        if not sample.get("source", {}).get(key):
            problems.append(f"source.{key} 缺失")

    if "synthetic_fields" not in sample:
        problems.append("synthetic_fields 缺失（沒有合成欄位時應為空陣列）")

    is_derived = "3-1" not in name
    if is_derived and "derivation" not in sample:
        problems.append("推導樣本缺少 derivation 區塊")
    if not is_derived and "derivation" in sample:
        problems.append("原始樣本不應有 derivation 區塊")

    items = sample.get("items", [])
    if sample.get("item_count") != len(items):
        problems.append(f"item_count {sample.get('item_count')} != len(items) {len(items)}")

    positions = [it["position"] for it in items]
    if positions != list(range(len(items))):
        problems.append("position 必須是 0-based 且等於陣列索引")

    pids = [it["playlist_item_id"] for it in items]
    if len(set(pids)) != len(pids):
        problems.append(f"playlist_item_id 不唯一（{len(set(pids))}/{len(pids)}）")

    # 能被 pydantic 接受才算真的相容。
    from scripts.schemas import EnrichedPlaylistItem

    for i, raw in enumerate(items):
        try:
            EnrichedPlaylistItem(**raw)
        except Exception as exc:  # noqa: BLE001 - 回報給人看，不需要細分
            problems.append(f"items[{i}] 無法建成 EnrichedPlaylistItem: {exc}")
            break
    return problems


def main(argv: list[str]) -> int:
    if "--validate" in argv:
        failed = False
        for suffix in ("3-1", *SEEDS):
            path = DATA_DIR / f"UserTest_Task{suffix}.json"
            sample = json.loads(path.read_text(encoding="utf-8"))
            problems = validate(sample, suffix)
            vids = {it["video_id"] for it in sample["items"]}
            print(
                f"  Task{suffix}: {len(sample['items'])} 項目 / {len(vids)} 支不重複影片"
                f"  {'✓ 通過' if not problems else '✗ ' + str(len(problems)) + ' 項問題'}"
            )
            for problem in problems:
                print(f"      - {problem}")
            failed |= bool(problems)
        return 1 if failed else 0

    samples = build()
    print(f"由 UserTest_Task3-1.md 建出 {len(samples)} 份 canonical 樣本：")
    for suffix, sample in samples.items():
        problems = validate(sample, suffix)
        vids = {it["video_id"] for it in sample["items"]}
        status = "✓" if not problems else f"✗ {len(problems)} 項問題"
        seed = sample.get("derivation", {}).get("seed", "—")
        print(f"  Task{suffix}  seed={str(seed):<5s} {len(sample['items'])} 項目"
              f" / {len(vids)} 支不重複影片  {status}")
        for problem in problems:
            print(f"      - {problem}")
        if problems:
            return 1

    if not WRITE_ENABLED:
        print("\n（唯讀模式，未寫檔。要實際寫入請設 YT_DEV_SAMPLE=1）")
        return 0

    for suffix, sample in samples.items():
        path = DATA_DIR / f"UserTest_Task{suffix}.json"
        path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → 已寫入 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
