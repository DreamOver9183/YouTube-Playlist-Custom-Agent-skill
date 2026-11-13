"""
resolver_bench.py — 藝人辨識的評測台（v4 M3）

**這是開發者工具，不是 skill 的一部分。** 擋門與本資料夾其他工具相同：
程式在 ``tests/dev/``、需要專用進入點、沒設 ``YT_DEV_WRITE=1`` 就完全不寫檔。

它在做什麼
----------

M3 用「先交給 Agent、平台結構化欄位優先」的加權公式取代 v3 的正規式辨識層，
替換前先量 v3 的分數當基準（分群結果存在 ``baseline.json``）。這支工具負責量：

- 測試資料：``tests/fixtures/playlists/<清單>.json``，11 份樣本清單連同影片描述
  （YouTube 自動產生的 Art Track 描述第二行是「歌名 · 藝人 · 藝人」）。
- 近似真值：``tests/dev/ytm_samples/`` 的 ytmusicapi 標註，取第一位藝人、去掉冒號後的
  掛名（``SawanoHiroyuki[nZk]:mizuki`` → ``SawanoHiroyuki[nZk]``），以 video_id 對到項目。
  ytmusicapi 看不到的項目不計分。
- 人工真值：``tests/fixtures/gold/<清單>.json``（``{"labels": {playlist_item_id: 藝人}}``），
  由使用者在 Claude Doc 校正預填藝人後讀回；值是 ``unknown`` 或空字串的項目不計分。
- 指標：B³（B-cubed）精確率、召回率、F1，每一份清單各自分群、全部項目一起平均。
  預測為 unknown 的項目各自算成單獨一群，所以「辨識失敗全丟同一桶」拿不到分數。另外回報 unknown 率。

清單分成訓練集（7 份）與保留集（4 份）。擬合只能看訓練集；保留集與人工真值只用來回報。
人工真值就標在保留集那 4 份上，所以兩者都是擬合時沒看過的資料。

用法
----

.. code-block:: bash

    python tests/dev/resolver_bench.py                       # v3 辨識（基準）在各資料集上的分數
    python tests/dev/resolver_bench.py --per-list            # 加上每一份清單的分數
    python tests/dev/resolver_bench.py --report              # 消融：各來源組合與基準的比較
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

from scripts import resolver  # noqa: E402
from scripts.schemas import EnrichedPlaylistItem  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "playlists"
GOLD_DIR = ROOT / "tests" / "fixtures" / "gold"
AGENT_DIR = ROOT / "tests" / "fixtures" / "agent_labels"
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

Predictor = Callable[[list[EnrichedPlaylistItem], str], list[str]]


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


def record_labels(answers_dir: Path) -> int:
    """把 Agent 的 ``labels.json`` 驗證後存成 ``tests/fixtures/agent_labels/<清單>.json``。

    ``answers_dir/<清單>/`` 底下要有 ``label-request`` 產生的 ``label_request.jsonl`` 與 Agent 寫的
    ``labels.json``。驗證規則與 ``optimize --labels`` 相同；存檔時把 pos 換成 playlist_item_id，
    這樣錄製結果不依賴位置，CI 可以直接重算。
    """
    from scripts import labels as protocol

    if os.environ.get("YT_DEV_WRITE") != "1":
        print("要寫入錄製的 Agent 標註請設定 YT_DEV_WRITE=1。")
        return 2
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    for playlist_id, label in LABELS.items():
        folder = answers_dir / playlist_id
        if not (folder / "labels.json").is_file():
            print(f"{label:<12} 沒有 labels.json，略過")
            continue
        items = load_items(playlist_id)
        doc = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
        try:
            answers, aliases = protocol.validate(doc, items, folder / protocol.REQUEST_FILE)
        except protocol.LabelError as exc:
            print(f"{label:<12} {exc.code}: {exc} {exc.extra}")
            return 1
        lines = ",\n".join(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
                           for key, value in answers.items())
        out = AGENT_DIR / f"{playlist_id}.json"
        out.write_text(
            f'{{"labels": {{\n{lines}\n}},\n"aliases": {json.dumps(aliases, ensure_ascii=False)}}}\n',
            encoding="utf-8",
        )
        print(f"{label:<12} {len(answers):>4} 筆、別名 {len(aliases)} 組 → {out.relative_to(ROOT)}")
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

def predict_current(items: list[EnrichedPlaylistItem], playlist_id: str = "") -> list[str]:
    """v3 辨識（已刪除）在這份清單上的分群結果，取自 ``baseline.json`` 存下的 ``groups``。

    舊辨識層在 M3 換掉之前，把它對 11 份測試資料的分群結果整份存了下來，所以人工真值
    之後才標好，也能算出舊版在人工真值上的分數。unknown 與釘選項目各自成一群。
    """
    stored = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["groups"].get(playlist_id, {})
    return [stored.get(item.playlist_item_id) or f"__solo{i}" for i, item in enumerate(items)]


def agent_labels(playlist_id: str) -> tuple[dict, dict]:
    """錄製的 Agent 標註：``(labels, aliases)``；還沒錄製時兩者都是空 dict。"""
    path = AGENT_DIR / f"{playlist_id}.json"
    if not path.is_file():
        return {}, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("labels", {}), data.get("aliases", {})


def as_groups(decided: list[tuple[str, float, str]]) -> list[str]:
    """公式結果轉成評分用的群組標籤：unknown 與釘選各自成一群。"""
    return [
        f"__solo{i}" if key in (resolver.UNKNOWN, resolver.PINNED) else key
        for i, (key, _margin, _source) in enumerate(decided)
    ]


def predict_formula(params: dict, use_agent: bool) -> Predictor:
    """新公式；``use_agent`` 為 True 時帶入錄製的 Agent 標註與別名，``params`` 是 fused 參數
    （沒有 A 的歌照正式流程用檔案裡的 fallback），否則 ``params`` 就是 fallback 參數。"""
    def predict(items: list[EnrichedPlaylistItem], playlist_id: str) -> list[str]:
        labels, aliases = agent_labels(playlist_id) if use_agent else ({}, {})
        found = resolver.resolve(items, labels=labels, aliases=aliases,
                                 fused=params if use_agent else None, fallback=None if use_agent else params)
        return as_groups([(r.key, r.margin, r.source) for r in found])
    return predict


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
        pred = predict(items, playlist_id)
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


# ── 擬合與消融 ────────────────────────────────────────────────────────────────

#: 每個參數 5 階。w_P 固定為 1：公式對權重整體縮放不變（θ 一起縮放），固定一個才不會重複搜尋。
GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
GRID_A = (0.5, 0.75, 1.0, 1.25, 1.5)
GRID_THETA = (0.0, 0.15, 0.3, 0.45, 0.6)
GRID_TAU = (0.05, 0.1, 0.15, 0.2, 0.25)


def prepare(use_agent: bool) -> list[tuple[str, list[str], list, list]]:
    """與參數無關的部分先算好：每份清單的頻道、候選、近似真值。"""
    logging.disable(logging.CRITICAL)
    data = []
    for playlist_id in LABELS:
        if not (FIXTURES_DIR / f"{playlist_id}.json").is_file():
            continue
        items = load_items(playlist_id)
        labels, aliases = agent_labels(playlist_id) if use_agent else ({}, {})
        lookup = resolver.alias_map(aliases)
        cands = [
            resolver.candidates(item, labels.get(item.playlist_item_id), lookup) if item.is_available else None
            for item in items
        ]
        approx = approx_truth(playlist_id)
        truth = [approx.get(item.video_id) if item.is_available else None for item in items]
        data.append((playlist_id, [item.channel_title for item in items], cands, truth))
    return data


def decided_by(channels: list[str], cands: list, params: dict, use_agent: bool) -> list:
    """與正式流程相同：擬合 fused 時，沒有 A 的歌（Agent 棄權）用檔案裡的 fallback。"""
    if not use_agent:
        return resolver.decide(channels, cands, params)
    return [row[:3] for row in resolver.decide_mixed(channels, cands, params, resolver.load_params("fallback"))]


def train_score(data: list, params: dict, use_agent: bool = False) -> dict:
    precision, recall, unknown = [], [], 0
    for playlist_id, channels, cands, truth in data:
        if playlist_id in HOLDOUT:
            continue
        p, r, u = b3_terms(as_groups(decided_by(channels, cands, params, use_agent)), truth)
        precision += p
        recall += r
        unknown += u
    return summarize(precision, recall, unknown)


def fit_tau(data: list, params: dict, use_agent: bool = False) -> float:
    """τ 不影響分群，只決定哪些項目標「待確認」：挑讓「標記 ↔ 實際分錯」F1 最高的那一階。"""
    rows = []  # (margin, wrong)
    for playlist_id, channels, cands, truth in data:
        if playlist_id in HOLDOUT:
            continue
        decided = decided_by(channels, cands, params, use_agent)
        pred = as_groups(decided)
        scored = [i for i, t in enumerate(truth) if t is not None]
        precision, recall, _ = b3_terms(pred, truth)
        for i, p, r in zip(scored, precision, recall):
            if decided[i][0] not in (resolver.UNKNOWN, resolver.PINNED):
                rows.append((decided[i][1], p < 1 or r < 1))
    best_tau, best_f1 = GRID_TAU[0], -1.0
    for tau in GRID_TAU:
        hit = sum(1 for m, wrong in rows if m < tau and wrong)
        flagged = sum(1 for m, _ in rows if m < tau)
        wrong = sum(1 for _, w in rows if w)
        f1 = 2 * hit / (flagged + wrong) if flagged + wrong else 0.0
        if f1 > best_f1:
            best_tau, best_f1 = tau, f1
    return best_tau


def fit(use_agent: bool) -> dict:
    """在訓練集上格點搜尋；同分時取 unknown 率低、權重總和小（較簡單）的那組。"""
    data = prepare(use_agent)
    best = None
    for w_A in (GRID_A if use_agent else (0.0,)):
        for w_T in GRID:
            for w_C in GRID:
                for beta in GRID:
                    for theta in GRID_THETA:
                        params = {"w_P": 1.0, "w_A": w_A, "w_T": w_T, "w_C": w_C, "beta": beta, "theta": theta}
                        score = train_score(data, params, use_agent)
                        rank = (score["f1"], -score["unknown_rate"], -(w_A + w_T + w_C + beta + theta))
                        if best is None or rank > best[0]:
                            best = (rank, params, score)
    params = dict(best[1], tau=fit_tau(data, best[1], use_agent))
    return {"params": params, "train": best[2]}


ABLATIONS = {
    # 名稱: (以哪組擬合結果為底, 要歸零的來源)
    "只用 P": ("fallback", ("w_T", "w_C", "beta")),
    "只用 T+C": ("fallback", ("w_P",)),
    "P+T+C（備援）": ("fallback", ()),
    "只用 A": ("fused", ("w_P", "w_T", "w_C", "beta")),
    "A+P": ("fused", ("w_T", "w_C")),
    "融合": ("fused", ()),
}


def report(params_by_mode: dict, per_list: bool) -> None:
    has_agent = any(AGENT_DIR.glob("*.json"))
    rows = [("v3（基準）", evaluate(predict_current))]
    for name, (mode, zeroed) in ABLATIONS.items():
        if mode == "fused" and not has_agent:
            continue
        params = dict(params_by_mode[mode], **{key: 0.0 for key in zeroed})
        rows.append((name, evaluate(predict_formula(params, use_agent=mode == "fused"))))
    print(f"{'組合':<16}{'訓練 F1':>9}{'保留 F1':>9}{'保留 unk':>10}{'真值 F1':>9}{'真值 unk':>10}")
    print("-" * 64)
    for name, result in rows:
        gold = result["gold"]
        gold_cols = (f"{gold['f1']:>9.3f}{gold['unknown_rate']:>10.1%}" if gold.get("items")
                     else f"{'—':>9}{'—':>10}")
        print(f"{name:<16}{result['train']['f1']:>9.3f}{result['holdout']['f1']:>9.3f}"
              f"{result['holdout']['unknown_rate']:>10.1%}{gold_cols}")
    if not has_agent:
        print("\n（還沒有錄製的 Agent 標註，A、A+P、融合三列從缺）")
    if per_list:
        print()
        print_report(rows[-1][1], per_list=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="藝人辨識評測台（B³ 與 unknown 率）")
    parser.add_argument("--per-list", action="store_true", help="另外印出每一份清單的分數")
    parser.add_argument("--build-fixtures", metavar="DIR", help="由 fetch 輸出建立 tests/fixtures/playlists/")
    parser.add_argument("--fit", choices=("fallback", "fused"), help="在訓練集上格點擬合一組參數")
    parser.add_argument("--ablate", metavar="PARAMS",
                        help="消融定案：把 fused 的這些參數（逗號分隔，例如 w_T,w_C）設為 0，只重擬 τ")
    parser.add_argument("--report", action="store_true", help="消融：各來源組合與 v3 基準的比較")
    parser.add_argument("--record-labels", metavar="DIR", help="驗證並錄製 Agent 標註（DIR/<清單>/labels.json）")
    args = parser.parse_args()

    if args.build_fixtures:
        return build_fixtures(Path(args.build_fixtures))
    if args.record_labels:
        return record_labels(Path(args.record_labels))

    if args.fit or args.ablate:
        mode = args.fit or "fused"
        if mode == "fused" and not any(AGENT_DIR.glob("*.json")):
            print("還沒有錄製的 Agent 標註（tests/fixtures/agent_labels/），無法擬合 fused。")
            return 2
        if args.ablate:
            params = dict(resolver.load_params("fused"), **{name: 0.0 for name in args.ablate.split(",")})
            params["tau"] = fit_tau(prepare(True), params, True)
        else:
            params = fit(use_agent=mode == "fused")["params"]
        result = evaluate(predict_formula(params, use_agent=mode == "fused"))
        print(f"{mode} 參數：{json.dumps(params)}\n")
        print_report(result, args.per_list)
        if os.environ.get("YT_DEV_WRITE") != "1":
            print("\n要寫入 scripts/resolver_params.json 請設定 YT_DEV_WRITE=1。")
            return 0
        path = resolver.PARAMS_PATH
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["unfitted"] = [name for name in stored.get("unfitted", []) if name != mode]
        stored["weights"][mode] = params
        stored.setdefault("scores", {})[mode] = {
            split: result[split] for split in ("train", "holdout", "gold") if result[split].get("items")
        }
        if args.ablate:
            stored.setdefault("ablated", {})[mode] = args.ablate.split(",")
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\n已寫入 {path.relative_to(ROOT)}")
        return 0

    if args.report:
        weights = json.loads(resolver.PARAMS_PATH.read_text(encoding="utf-8"))["weights"]
        report(weights, args.per_list)
        return 0

    print_report(evaluate(predict_current), args.per_list)
    return 0


if __name__ == "__main__":
    sys.exit(main())
