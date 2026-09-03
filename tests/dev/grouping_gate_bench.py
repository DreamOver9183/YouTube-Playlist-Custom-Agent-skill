"""
grouping_gate_bench.py — 分群效益閘門的校準台

**這是開發者工具，不是 skill 的一部分。** 擋門與 ``ytm_probe.py`` /
``album_bench.py`` 相同：程式放在 ``tests/dev/``（不在任何 skill 載入路徑）、
需要專用進入點、且完全不寫檔。

它在做什麼
----------

``scripts/optimizer.evaluate_grouping_benefit`` 的兩道門檻是用真實清單校出來的，
不是拍腦袋定的。這支工具把 ``scripts/cache/`` 裡收錄的清單全部跑一遍，印出每一份
的 unknown 佔比與有效聚集率，並驗證閘門**恰好**攔到該攔的那幾份。

門檻改動之後一定要重跑這支：單元測試（``tests/test_optimizer.py``）用的是合成
fixture，只能證明計算邏輯對，證明不了門檻定在正確的位置。**只有真實清單能。**

為什麼不放進 CI
----------------

它依賴 ``scripts/cache/playlist_*.json`` 這些收錄下來的真實快取。CI 的測試一律是
無網路、無外部檔案依賴的合成資料；把校準資料混進去會讓 CI 的失敗訊息變得難以判讀
（到底是程式壞了，還是樣本換了）。

用法
----

.. code-block:: bash

    python tests/dev/grouping_gate_bench.py           # 印出全部樣本
    python tests/dev/grouping_gate_bench.py --check   # 驗證攔截集合，不符就非零離開

預期結果
--------

11 份樣本中恰好 4 份被攔下。最接近的未攔件是 #2 中文歌1（45.5%），距 30% 界線有
15pp 餘裕，沒有邊界爭議——這是這組門檻可信的主要理由。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# 與 yt_tool.py 同一道保險：Windows 主控台預設 cp950，直接 print 中日文會炸。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.optimizer import (  # noqa: E402
    _EFFECTIVE_GROUPING_GATE,
    _UNKNOWN_RATIO_GATE,
    evaluate_grouping_benefit,
    group_by_artist,
)
from scripts.schemas import EnrichedPlaylistItem  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "cache"

#: 樣本代號，與 album_bench.py / BACKLOG.md 的表格對齊。
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

#: 校準當下應該被攔下的樣本。門檻調動而這組跟著變，代表校準基準也變了，
#: 要連同 BACKLOG.md 的表一起更新，不要只改這裡讓 --check 變綠。
EXPECTED_FLAGGED = {"#6 電影音樂1", "#8 K-pop", "#9 中文歌3", "#10 日文歌單"}


def measure() -> list[tuple[str, int, object]]:
    """對每一份收錄的快取跑一次分群，回傳 (代號, 項目數, GroupingBenefit)。"""
    logging.disable(logging.CRITICAL)
    rows = []
    for path in sorted(CACHE_DIR.glob("playlist_*.json")):
        playlist_id = path.stem.removeprefix("playlist_")
        label = LABELS.get(playlist_id)
        if label is None:
            continue  # 未納入校準集的快取
        raw = json.loads(path.read_text(encoding="utf-8"))["items"]
        items = [EnrichedPlaylistItem.model_validate(r) for r in raw]
        _, groups, resolutions, _ = group_by_artist(items)
        rows.append((label, len(items), evaluate_grouping_benefit(groups, resolutions)))
    return sorted(rows, key=lambda r: r[2].effective_grouping_ratio)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="驗證攔截集合是否與校準基準一致，不一致就以非零離開碼失敗",
    )
    args = parser.parse_args()

    rows = measure()
    if not rows:
        print(f"找不到任何校準樣本。預期在 {CACHE_DIR} 下有 playlist_*.json。")
        return 1

    print(
        f"門檻：unknown 佔比 >= {_UNKNOWN_RATIO_GATE:.0%}"
        f" 或 有效聚集率 < {_EFFECTIVE_GROUPING_GATE:.0%}"
    )
    print("=" * 78)
    print(f"{'清單':<14}{'項目':>5}{'unknown':>10}{'有效聚集':>10}{'孤兒群':>8}  判定")
    print("-" * 78)
    for label, count, benefit in rows:
        mark = "低效益" if benefit.verdict == "low" else "ok"
        print(
            f"{label:<14}{count:>5}"
            f"{benefit.unknown_ratio:>9.1%}"
            f"{benefit.effective_grouping_ratio:>10.1%}"
            f"{benefit.orphan_group_count:>8}  {mark}"
        )
    print("-" * 78)

    flagged = {label for label, _, benefit in rows if benefit.verdict == "low"}
    passing = [b.effective_grouping_ratio for _, _, b in rows if b.verdict == "ok"]
    if passing:
        margin = min(passing) - _EFFECTIVE_GROUPING_GATE
        print(
            f"攔下 {len(flagged)}/{len(rows)} 份。最接近的未攔件有效聚集率"
            f" {min(passing):.1%}，距門檻 {margin * 100:.1f}pp。"
        )

    if not args.check:
        return 0

    if flagged == EXPECTED_FLAGGED:
        print(f"\n[OK] 攔截集合與校準基準一致：{sorted(flagged)}")
        return 0

    print("\n[FAIL] 攔截集合與校準基準不符。")
    print(f"  預期：{sorted(EXPECTED_FLAGGED)}")
    print(f"  實際：{sorted(flagged)}")
    print(f"  多攔：{sorted(flagged - EXPECTED_FLAGGED) or '無'}")
    print(f"  漏攔：{sorted(EXPECTED_FLAGGED - flagged) or '無'}")
    print(
        "  門檻是用這 11 份真實清單校出來的。若這是刻意的調整，請連同"
        " docs/BACKLOG.md 的表一起更新 EXPECTED_FLAGGED。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
