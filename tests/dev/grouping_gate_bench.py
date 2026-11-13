"""
grouping_gate_bench.py — 分群效益閘門的校準台

**這是開發者工具，不是 skill 的一部分。** 擋門與 ``ytm_probe.py``
相同：程式放在 ``tests/dev/``（不在任何 skill 載入路徑）、
需要專用進入點、且完全不寫檔。

它在做什麼
----------

``scripts/optimizer.evaluate_grouping_benefit`` 的兩道門檻是用真實清單校出來的，
不是拍腦袋定的。這支工具把 ``tests/fixtures/playlists/`` 的 11 份樣本清單全部跑一遍
（預設帶入 ``tests/fixtures/agent_labels/`` 錄製的 Agent 標註，與正式流程相同），印出每一份
的 unknown 佔比與有效聚集率，並驗證閘門**恰好**攔到該攔的那幾份。

門檻或辨識公式改動之後一定要重跑這支：單元測試（``tests/test_optimizer.py``）用的是合成
fixture，只能證明計算邏輯對，證明不了門檻定在正確的位置。**只有真實清單能。**
樣本與 Agent 標註都在 repo 裡，所以 ``--check`` 也在 CI 上跑。

用法
----

.. code-block:: bash

    python tests/dev/grouping_gate_bench.py              # 印出全部樣本
    python tests/dev/grouping_gate_bench.py --no-agent   # 不帶 Agent 標註（只用備援公式）
    python tests/dev/grouping_gate_bench.py --check      # 驗證攔截集合，不符就非零離開

預期結果
--------

v3.10.0 校準時 11 份裡攔下 4 份：#6、#9、#10 當時以為是結構上不值得分群（孤兒群多），
#8 K-pop 則是因為廠牌頻道有 39% 辨識不出來。

v4 M3 換上新公式與 Agent 標註後只攔 #9、#10。用近似真值（ytmusicapi）直接算「真正同藝人的
比例」，#9 是 13%、#10 是 16%，確實不值得分群；#6 卻是 39%（高於 30% 門檻），當年的 23%
是 v3 認不出配樂作曲家、把同一人拆成很多孤兒群造成的，所以 #6 不再被攔是辨識改善，不是門檻
失準。#8 的 unknown 從 39% 降到 0%，同樣是預期的改善；先前通過的清單沒有變成被攔。
"""

from __future__ import annotations

import argparse
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

from resolver_bench import FIXTURES_DIR, LABELS, agent_labels, load_items  # noqa: E402
from scripts.optimizer import (  # noqa: E402
    _EFFECTIVE_GROUPING_GATE,
    _UNKNOWN_RATIO_GATE,
    evaluate_grouping_benefit,
    group_by_artist,
)

#: 應該被攔下的樣本。門檻或辨識改動而這組跟著變，代表校準基準也變了，
#: 要連同 BACKLOG.md 的表一起更新，不要只改這裡讓 --check 變綠。
#: v4 M3：#6、#8 不再被攔，原因見模組說明（真值的同藝人比例 39%、unknown 39% → 0%）。
EXPECTED_FLAGGED = {"#9 中文歌3", "#10 日文歌單"}


def measure(use_agent: bool = True) -> list[tuple[str, int, object]]:
    """對每一份樣本清單跑一次分群，回傳 (代號, 項目數, GroupingBenefit)。"""
    logging.disable(logging.CRITICAL)
    rows = []
    for playlist_id, label in LABELS.items():
        if not (FIXTURES_DIR / f"{playlist_id}.json").is_file():
            continue
        items = load_items(playlist_id)
        labels, aliases = agent_labels(playlist_id) if use_agent else ({}, {})
        _, groups, resolutions = group_by_artist(items, labels, aliases)
        rows.append((label, len(items), evaluate_grouping_benefit(groups, resolutions)))
    return sorted(rows, key=lambda r: r[2].effective_grouping_ratio)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="驗證攔截集合是否與校準基準一致，不一致就以非零離開碼失敗",
    )
    parser.add_argument("--no-agent", action="store_true", help="不帶錄製的 Agent 標註（只用備援公式）")
    args = parser.parse_args()

    rows = measure(use_agent=not args.no_agent)
    if not rows:
        print(f"找不到任何校準樣本。預期在 {FIXTURES_DIR} 下有樣本清單。")
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
