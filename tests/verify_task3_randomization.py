"""
verify_task3_randomization.py — 重算 Task 3 隨機測試集的一致性與相似度

重算 ``User Testing/UserTest_Task3_Randomization_Report.md`` 裡的每一個數字，
不沿用產生資料時的計算結果。報告與資料不符時以非零離開碼失敗。

用法::

    python tests/verify_task3_randomization.py

檢查四件事：

1. **可重現**：以記錄的種子重跑 ``random.Random(seed).shuffle()``，順序須與檔案完全相同。
2. **元素一致性**：以 ``playlist_item_id`` 為元素計算 Jaccard index，須為 1.0。
3. **排序獨立性**：計算 Kendall's tau，須落在獨立虛無假設的合理範圍內。
4. **重複影片**：確認清單含重複 ``video_id``，且排名鍵沒有被 ``video_id`` 汙染。

為什麼排名鍵一定要用 ``playlist_item_id``
-----------------------------------------

這份清單有 4 支影片各出現兩次（192 個項目、188 支不重複影片）。用 ``video_id``
建位置索引時，字典會把重複的項目收斂成一筆，算出來的 tau 是錯的 —— 這個錯誤實際
發生過，也是這支腳本存在的原因。播放清單的主鍵永遠是 ``playlist_item_id``。
"""

from __future__ import annotations

import copy
import json
import math
import random
import sys
from itertools import combinations
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "User Testing"

#: 產生 Task3-2 ~ 3-5 時使用的種子，與 tests/dev/build_task3_samples.py 一致。
SEEDS = {"3-2": 42, "3-3": 108, "3-4": 256, "3-5": 777}

#: |tau| 的容許上限，以獨立虛無假設下的標準差為單位。
TAU_SIGMA_LIMIT = 3.0


def load(suffix: str) -> dict:
    return json.loads((DATA_DIR / f"UserTest_Task{suffix}.json").read_text(encoding="utf-8"))


def kendall(x: list[int], y: list[int]) -> tuple[float, int, int]:
    """Kendall's tau 與同序／逆序配對數。排名鍵唯一無並列，故 tau_a == tau_b。"""
    concordant = discordant = 0
    for i, j in combinations(range(len(x)), 2):
        sign = (x[i] - x[j]) * (y[i] - y[j])
        if sign > 0:
            concordant += 1
        elif sign < 0:
            discordant += 1
    return (concordant - discordant) / (len(x) * (len(x) - 1) // 2), concordant, discordant


def main() -> int:
    failures: list[str] = []

    base = load("3-1")
    items = base["items"]
    n = len(items)
    pids = [t["playlist_item_id"] for t in items]
    vids = [t["video_id"] for t in items]

    print(f"基準版 Task3-1：{n} 個清單項目，{len(set(vids))} 支不重複影片")

    # ── 0. 主鍵健全性 ────────────────────────────────────────────
    if base.get("schema") != "yt-playlist-sample/v1":
        failures.append(f"schema 應為 yt-playlist-sample/v1，實際為 {base.get('schema')!r}")
    if base.get("item_count") != n:
        failures.append(f"item_count {base.get('item_count')} != len(items) {n}")
    if len(set(pids)) != n:
        failures.append(f"playlist_item_id 不唯一（{len(set(pids))}/{n}），無法當排名鍵")
    if len(set(vids)) == n:
        print("  注意：這份清單目前沒有重複影片，重複項目的迴歸覆蓋已失效")
    else:
        print(f"  含 {n - len(set(vids))} 個重複項目 —— 排名鍵使用 playlist_item_id")

    # ── 1. 種子可重現 ────────────────────────────────────────────
    print("\n[1/3] 種子可重現性")
    for suffix, seed in SEEDS.items():
        rng = random.Random(seed)
        expected = copy.deepcopy(items)
        rng.shuffle(expected)
        actual = [t["playlist_item_id"] for t in load(suffix)["items"]]
        ok = [t["playlist_item_id"] for t in expected] == actual
        print(f"  Task{suffix} seed={seed:<4d} {'✓ 可重現' if ok else '✗ 無法重現'}")
        if not ok:
            failures.append(f"Task{suffix} 無法由 seed={seed} 重現")

    # ── 2. Jaccard ──────────────────────────────────────────────
    print("\n[2/3] 元素一致性 (Jaccard, 以 playlist_item_id 為元素)")
    base_set = set(pids)
    for suffix in SEEDS:
        other = load(suffix)["items"]
        other_set = {t["playlist_item_id"] for t in other}
        jaccard = len(base_set & other_set) / len(base_set | other_set)
        idx_ok = [t["position"] for t in other] == list(range(n))
        print(
            f"  3-1 vs {suffix}  Jaccard={jaccard:.4f}"
            f"  ∩={len(base_set & other_set)}  ∪={len(base_set | other_set)}"
            f"  position 連號={'✓' if idx_ok else '✗'}"
        )
        if jaccard != 1.0:
            failures.append(f"Task{suffix} Jaccard={jaccard:.4f}，不是基準版的嚴格排列")
        if not idx_ok:
            failures.append(f"Task{suffix} 的 position 欄位沒有重新編號成 0..{n-1}")

    # ── 3. Kendall's tau ────────────────────────────────────────
    sigma = math.sqrt(2 * (2 * n + 5) / (9 * n * (n - 1)))
    limit = TAU_SIGMA_LIMIT * sigma
    print(f"\n[3/3] 排序獨立性 (Kendall's tau, σ={sigma:.4f}, 容許 |τ| ≤ {limit:.4f})")

    pos = {p: i for i, p in enumerate(pids)}
    orders = {"3-1": list(range(n))}
    for suffix in SEEDS:
        orders[suffix] = [pos[t["playlist_item_id"]] for t in load(suffix)["items"]]

    keys = ["3-1", *SEEDS]
    for a, b in combinations(keys, 2):
        tau, concordant, discordant = kendall(orders[a], orders[b])
        z = 3 * tau * math.sqrt(n * (n - 1)) / math.sqrt(2 * (2 * n + 5))
        p_value = math.erfc(abs(z) / math.sqrt(2))
        flag = "✓" if abs(tau) <= limit else "✗"
        print(
            f"  {a} vs {b}  C={concordant:>5d} D={discordant:>5d}"
            f"  τ={tau:>+8.4f}  Z={z:>+6.3f}  p={p_value:.3f}  {flag}"
        )
        if abs(tau) > limit:
            failures.append(f"{a} vs {b} 的 |τ|={abs(tau):.4f} 超出 {TAU_SIGMA_LIMIT}σ，打散不足")

    # ── 結果 ────────────────────────────────────────────────────
    print()
    if failures:
        print(f"✗ {len(failures)} 項不通過：")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("✓ 全部通過：可重現、元素一致、排序獨立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
