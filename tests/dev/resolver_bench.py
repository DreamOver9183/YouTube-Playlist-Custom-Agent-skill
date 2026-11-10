"""
resolver_bench.py — 藝人辨識的評測台（v4 M3）

**這是開發者工具，不是 skill 的一部分。** 擋門與本資料夾其他工具相同：
程式在 ``tests/dev/``、需要專用進入點、沒設 ``YT_DEV_WRITE=1`` 就完全不寫檔。

它在做什麼
----------

M3 要用「先交給 Agent、平台結構化欄位優先」的加權公式取代現行的正規式辨識層，
而且**打不過現行辨識就不替換**。這支工具負責量：

- 測試資料：``tests/fixtures/playlists/<清單>.json``，11 份樣本清單連同影片描述
  （YouTube 自動產生的 Art Track 描述第二行是「歌名 · 藝人 · 藝人」）。
- 近似真值：``tests/dev/ytm_samples/`` 的 ytmusicapi 標註，取第一位藝人、去掉冒號後的
  掛名（``SawanoHiroyuki[nZk]:mizuki`` → ``SawanoHiroyuki[nZk]``），以 video_id 對到項目。
  ytmusicapi 看不到的項目不計分。
- 人工真值：``tests/fixtures/gold/<清單>.json``（Agent 先標、使用者用檢查頁的標註模式校正）。
- 指標：B³（B-cubed）精確率、召回率、F1，每一份清單各自分群、全部項目一起平均。
  預測為 unknown 的項目各自算成單獨一群，所以「辨識失敗全丟同一桶」拿不到分數。另外回報 unknown 率。

清單分成訓練集（7 份）與保留集（4 份）。擬合只能看訓練集；保留集與人工真值只用來回報。
人工真值就標在保留集那 4 份上，所以兩者都是擬合時沒看過的資料。

用法
----

.. code-block:: bash

    python tests/dev/resolver_bench.py                       # 現行辨識在各資料集上的分數
    python tests/dev/resolver_bench.py --per-list            # 加上每一份清單的分數
    YT_DEV_WRITE=1 python tests/dev/resolver_bench.py --baseline          # 寫入 tests/fixtures/baseline.json
    YT_DEV_WRITE=1 python tests/dev/resolver_bench.py --build-fixtures DIR  # 由 fetch 輸出建立測試資料

``--build-fixtures`` 的 DIR 是一個資料夾，裡面每份清單一個 ``yt_tool fetch --out`` 的輸出檔，
檔名為 ``<清單 ID>.json``。描述只留前 3 個非空行（最多 300 字）：藝人行一定在最上面，
整份描述常有大量連結與歌詞，沒有必要進 repo。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Callable

# Windows 主控台預設 cp950，直接 print 中日文會炸。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.optimizer import group_by_artist  # noqa: E402
from scripts.schemas import EnrichedPlaylistItem  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "playlists"
GOLD_DIR = ROOT / "tests" / "fixtures" / "gold"
YTM_DIR = ROOT / "tests" / "dev" / "ytm_samples"
BASELINE_PATH = ROOT / "tests" / "fixtures" / "baseline.json"

#: 樣本代號，與 grouping_gate_bench.py、BACKLOG.md 的表格對齊。
LABELS = {
    "PLtskdo8cFkvn4yfJLBqg68ClUGVSDPfsA": "#0 舊日文",
    "PLLpKeZeMXlNY": "#1 Task3",
    "PLtskdo8cFkvmhJyaxje75uAgiUPCuPqSc": "#2 中文歌1",
    "PLtskdo8cFkvkNWh5d47epcdDs_W4SbRtg": "#3 中文歌2",
    "PLtskdo8cFkvnghsg3czSpDRLsjsfRU9nh": "#4 OST/BGM",
    "PLtskdo8cFkvmC5d2h0fv86tC8Pu9ftFFd": "#5 主題清單",
    "PL7lowGKubpuuKB2xLNf1d1Ck9koNcaRXp": "#6 電影音樂1",
    "PLHx-DNmuIJZjNh76_85SPjlOq7jsFi5ek": "#7 電影音樂2",
    "PLOHoVaTp8R7ccrQM3EpCTVDdwHhXrJhXS": "#8 K-pop",
    "PL8tHEOLOTRa7eimfOm7ZAJoHyVfQDHb2w": "#9 中文歌3",
    "PLjC0R0W6CT-zkuojEktE1m8bXHTibU6vr": "#10 日文歌單",
}

#: 保留集：類型各異（中文、混合主題、K-pop 廠牌頻道、日文），合計約 340 首。
#: 在看到任何分數之前決定，之後不要為了分數好看而調換。
HOLDOUT = frozenset({
    "PLtskdo8cFkvkNWh5d47epcdDs_W4SbRtg",
    "PLtskdo8cFkvmC5d2h0fv86tC8Pu9ftFFd",
    "PLOHoVaTp8R7ccrQM3EpCTVDdwHhXrJhXS",
    "PLjC0R0W6CT-zkuojEktE1m8bXHTibU6vr",
})

#: 測試資料保留的欄位：辨識用得到的，加上載回 EnrichedPlaylistItem 必需的。
KEEP_FIELDS = (
    "playlist_item_id", "video_id", "position", "added_at",
    "title", "channel_title", "description", "is_available",
)

Predictor = Callable[[list[EnrichedPlaylistItem]], list[str]]


# ── 測試資料 ──────────────────────────────────────────────────────────────────

def trim_description(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[:3])[:300]


def build_fixtures(raw_dir: Path) -> int:
    if os.environ.get("YT_DEV_WRITE") != "1":
        print("要寫入測試資料請設定 YT_DEV_WRITE=1。")
        return 2
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(raw_dir.glob("*.json")):
        if path.stem not in LABELS:
            print(f"略過不在樣本清單內的 {path.name}")
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw["items"] if isinstance(raw, dict) else raw
        rows = sorted(rows, key=lambda r: r["position"])
        trimmed = []
        for row in rows:
            kept = {field: row.get(field) for field in KEEP_FIELDS if field in row}
            kept["description"] = trim_description(row.get("description", ""))
            trimmed.append(kept)
        out = FIXTURES_DIR / f"{path.stem}.json"
        # One song per line: small, and a diff shows exactly which songs changed.
        lines = ",\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in trimmed)
        out.write_text(f"[\n{lines}\n]\n", encoding="utf-8")
        print(f"{LABELS[path.stem]:<12} {len(trimmed):>4} 首 → {out.relative_to(ROOT)}")
    return 0


def load_items(playlist_id: str) -> list[EnrichedPlaylistItem]:
    raw = json.loads((FIXTURES_DIR / f"{playlist_id}.json").read_text(encoding="utf-8"))
    return [EnrichedPlaylistItem.model_validate(row) for row in raw]


# ── 真值 ──────────────────────────────────────────────────────────────────────

def truth_key(name: str) -> str:
    """真值只拿來分群，所以只做最保守的正規化：NFKC、casefold、收斂空白。"""
    text = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"\s+", " ", text).strip()


def approx_truth(playlist_id: str) -> dict[str, str]:
    """video_id → 近似真值藝人鍵（ytmusicapi 第一位藝人，去掉冒號掛名）。"""
    path = YTM_DIR / f"{playlist_id}.json"
    if not path.is_file():
        return {}
    truth = {}
    for row in json.loads(path.read_text(encoding="utf-8"))["rows"]:
        artists = row.get("ytm_artists") or []
        if artists and artists[0]:
            truth[row["video_id"]] = truth_key(artists[0].split(":")[0])
    return truth


def gold_truth(playlist_id: str) -> dict[str, str]:
    """playlist_item_id → 人工真值藝人鍵；還沒有人工真值時回傳空 dict。"""
    path = GOLD_DIR / f"{playlist_id}.json"
    if not path.is_file():
        return {}
    labels = json.loads(path.read_text(encoding="utf-8"))["labels"]
    return {key: truth_key(value) for key, value in labels.items() if value and value != "unknown"}


# ── 預測與指標 ────────────────────────────────────────────────────────────────

def predict_current(items: list[EnrichedPlaylistItem]) -> list[str]:
    """現行辨識（group_by_artist）的分群結果；unknown 與釘選項目各自成一群。"""
    _, groups, _, _ = group_by_artist(items)
    labels = [f"__solo{i}" for i in range(len(items))]
    for key, indices in groups.items():
        if key == "unknown":
            continue
        for index in indices:
            labels[index] = key
    return labels


def b3_terms(pred: list[str | None], truth: list[str | None]) -> tuple[list[float], list[float], int]:
    """一份清單內，每個有真值項目的 B³ 精確率與召回率，以及預測為 unknown 的數量。"""
    scored = [i for i, t in enumerate(truth) if t is not None]
    pred_members: dict[str, set[int]] = defaultdict(set)
    true_members: dict[str, set[int]] = defaultdict(set)
    for i in scored:
        pred_members[pred[i]].add(i)
        true_members[truth[i]].add(i)
    precision, recall = [], []
    for i in scored:
        both = len(pred_members[pred[i]] & true_members[truth[i]])
        precision.append(both / len(pred_members[pred[i]]))
        recall.append(both / len(true_members[truth[i]]))
    unknown = sum(1 for i in scored if pred[i].startswith("__solo"))
    return precision, recall, unknown


def summarize(precision: list[float], recall: list[float], unknown: int) -> dict:
    n = len(precision)
    if not n:
        return {"items": 0}
    p, r = sum(precision) / n, sum(recall) / n
    return {
        "items": n,
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(2 * p * r / (p + r) if p + r else 0.0, 4),
        "unknown_rate": round(unknown / n, 4),
    }


def evaluate(predict: Predictor) -> dict:
    """回傳 {split: 指標}，split 為 train／holdout／gold，另附 per_list。"""
    logging.disable(logging.CRITICAL)
    pools = {name: ([], [], [0]) for name in ("train", "holdout", "gold")}
    per_list = {}
    for playlist_id, label in LABELS.items():
        if not (FIXTURES_DIR / f"{playlist_id}.json").is_file():
            continue
        items = load_items(playlist_id)
        pred = predict(items)
        approx = approx_truth(playlist_id)
        gold = gold_truth(playlist_id)
        sets = {
            ("holdout" if playlist_id in HOLDOUT else "train"):
                [approx.get(item.video_id) if item.is_available else None for item in items],
        }
        if gold:
            sets["gold"] = [gold.get(item.playlist_item_id) if item.is_available else None for item in items]
        per_list[label] = {}
        for split, truth in sets.items():
            precision, recall, unknown = b3_terms(pred, truth)
            pools[split][0].extend(precision)
            pools[split][1].extend(recall)
            pools[split][2][0] += unknown
            per_list[label][split] = summarize(precision, recall, unknown)
    result = {split: summarize(p, r, u[0]) for split, (p, r, u) in pools.items()}
    result["per_list"] = per_list
    return result


def print_report(result: dict, per_list: bool) -> None:
    print(f"{'資料集':<10}{'項目':>6}{'精確率':>9}{'召回率':>9}{'F1':>9}{'unknown':>10}")
    print("-" * 56)
    for split, name in (("train", "訓練集"), ("holdout", "保留集"), ("gold", "人工真值")):
        row = result[split]
        if not row.get("items"):
            print(f"{name:<10}{'（尚無資料）':>12}")
            continue
        print(f"{name:<10}{row['items']:>6}{row['precision']:>9.3f}{row['recall']:>9.3f}"
              f"{row['f1']:>9.3f}{row['unknown_rate']:>10.1%}")
    if not per_list:
        return
    print()
    for label, splits in result["per_list"].items():
        for split, row in splits.items():
            if row.get("items"):
                print(f"  {label:<12}{split:<8}{row['items']:>5}  F1 {row['f1']:.3f}"
                      f"  P {row['precision']:.3f}  R {row['recall']:.3f}  unknown {row['unknown_rate']:.1%}")


def main() -> int:
    parser = argparse.ArgumentParser(description="藝人辨識評測台（B³ 與 unknown 率）")
    parser.add_argument("--per-list", action="store_true", help="另外印出每一份清單的分數")
    parser.add_argument("--baseline", action="store_true", help="把現行辨識的分數寫進 tests/fixtures/baseline.json")
    parser.add_argument("--build-fixtures", metavar="DIR", help="由 fetch 輸出建立 tests/fixtures/playlists/")
    args = parser.parse_args()

    if args.build_fixtures:
        return build_fixtures(Path(args.build_fixtures))

    result = evaluate(predict_current)
    print_report(result, args.per_list)

    if args.baseline:
        if os.environ.get("YT_DEV_WRITE") != "1":
            print("\n要寫入 baseline.json 請設定 YT_DEV_WRITE=1。")
            return 2
        BASELINE_PATH.write_text(
            json.dumps({"resolver": "v3 group_by_artist（正規式＋頻道＋多數決）", **result},
                       ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"\n已寫入 {BASELINE_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
