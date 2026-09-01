"""
album_bench.py — 專輯抽取器的準確度評測台

**這是開發者工具，不是 skill 的一部分。** 擋門與 ``learn_scenarios.py`` 相同：
程式放在 ``tests/dev/``（不在任何 skill 載入路徑）、需要專用進入點、且沒有設
``YT_DEV_ALBUM=1`` 就完全不寫檔。

它在做什麼
----------

YouTube Data API v3 的 ``videos.list`` **不回傳專輯**，但 Topic 頻道（自動產生的
art track）的 ``tags`` 帶有結構化資訊：

.. code-block:: text

    ['Avicii', 'アビーチー', 'アヴィーチー', 'True', 'Wake Me Up', 'ウェイクミーアップ']
      藝人      假名          假名          專輯    曲名          曲名假名

實測 11 份樣本共 623 首，98% 的專輯名確實出現在 tags 裡。但**「可還原」不等於
「抽得出來」** —— 前者是知道答案去比對，後者才是要解的問題。

這支工具用 ``tests/dev/ytm_samples/`` 裡 ytmusicapi 標註的專輯當真值，量測任一
抽取器的精確率與召回率。**它是專輯聚集功能的出貨判準。**

為什麼精確率是唯一的硬指標
--------------------------

抽不出專輯時的退路是「照現行藝人分群」，而那條路本來就能用。所以召回率低只是
功能價值打折，精確率低卻會**把歌搬到錯的地方**——每筆 50 units，而且使用者得
手動修回來。因此：

- **精確率 ≥ 90%** 才可以預設啟用（硬門檻）。
- 召回率不設下限，但要回報；太低代表這個功能沒有價值。

真值的兩種缺席
--------------

ytmusicapi 沒有給專輯的曲目（例如 #8 K-pop 全部 100 首）**不是「不用管」**，
而是最重要的負向測試：在那些曲目上抽出任何東西都算假陽性。共現頻率法曾在該份
清單上憑空生出 11 個假群組，這種錯誤必須被指標抓到。

用法
----

.. code-block:: bash

    python tests/dev/album_bench.py              # 跑所有已註冊的抽取器
    python tests/dev/album_bench.py --baseline   # 只跑基準線，當回歸比較
    python tests/dev/album_bench.py -v combined  # 印出某個抽取器的失敗案例
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.optimizer import _loose, _normalize_name  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parent / "ytm_samples"
CACHE_DIR = ROOT / "scripts" / "cache"

WRITE_ENABLED = os.environ.get("YT_DEV_ALBUM") == "1"

#: 出貨門檻。低於此值不可預設啟用專輯聚集。
PRECISION_GATE = 0.90

#: 樣本的可讀名稱。純粹為了報表好看，缺的就用 playlist id。
LABELS = {
    "PLLpKeZeMXlNY": "1 Task3",
    "PLtskdo8cFkvmhJyaxje75uAgiUPCuPqSc": "2 中文歌1",
    "PLtskdo8cFkvkNWh5d47epcdDs_W4SbRtg": "3 中文歌2",
    "PLtskdo8cFkvnghsg3czSpDRLsjsfRU9nh": "4 OST/BGM",
    "PLtskdo8cFkvmC5d2h0fv86tC8Pu9ftFFd": "5 主題清單",
    "PL7lowGKubpuuKB2xLNf1d1Ck9koNcaRXp": "6 電影音樂1",
    "PLHx-DNmuIJZjNh76_85SPjlOq7jsFi5ek": "7 電影音樂2",
    "PLOHoVaTp8R7ccrQM3EpCTVDdwHhXrJhXS": "8 K-pop",
    "PL8tHEOLOTRa7eimfOm7ZAJoHyVfQDHb2w": "9 中文歌3",
    "PLjC0R0W6CT-zkuojEktE1m8bXHTibU6vr": "10 日文歌單",
    "PLtskdo8cFkvn4yfJLBqg68ClUGVSDPfsA": "0 舊日文歌單",
}

#: 版本／版次後綴。比對時必須去掉，否則 "True" 與 "True (Bonus Edition)" 會被
#: 誤判成抽錯 —— 研究階段實際發生過，一度低估了位置法的精確率。
_EDITION = re.compile(
    r"\s*[\(\[]\s*"
    r"(ep|single|deluxe|bonus|remaster|remastered|expanded|special|complete"
    r"|original|edition|ver|version|feat|anniversary|reissue|explicit)"
    r"[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)


def canon(name: str | None) -> str:
    """把專輯名收斂成可比較的形式（去版次後綴 → 只留字母數字）。"""
    if not name:
        return ""
    previous = None
    while previous != name:          # 後綴可能疊加："X (Deluxe) (Remastered)"
        previous = name
        name = _EDITION.sub("", name)
    return _loose(name)


# ─────────────────────────────────────────────
# 載入
# ─────────────────────────────────────────────


def load_cases() -> list[tuple[str, dict[str, str | None], dict[str, dict]]]:
    """回傳 ``[(playlist_id, truth, items_by_video_id), ...]``。

    ``truth`` 涵蓋樣本裡的**全部**曲目，沒有專輯的其值為 None —— 那些是負向
    測試案例，不可以被過濾掉。
    """
    cases = []
    for path in sorted(SAMPLE_DIR.glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if not sample.get("rows"):
            continue
        cache_path = CACHE_DIR / f"playlist_{sample['playlist_id']}.json"
        if not cache_path.exists():
            print(f"  略過 {sample['playlist_id']}：沒有本地快取，先跑 fetch --refresh")
            continue
        items = {i["video_id"]: i for i in json.loads(cache_path.read_text(encoding="utf-8"))["items"]}
        truth = {r["video_id"]: r["ytm_album"] for r in sample["rows"] if r["video_id"] in items}
        cases.append((sample["playlist_id"], truth, items))
    return cases


# ─────────────────────────────────────────────
# 抽取器：基準線
# ─────────────────────────────────────────────
#
# 每個抽取器的介面都是 ``fn(items) -> {video_id: album or None}``，
# 其中 items 是**同一個頻道**的 EnrichedPlaylistItem dict list。
# 分頻道呼叫是刻意的：tags 的結構是以頻道為單位形成的。


def _artistish_tags(items: list[dict]) -> set[str]:
    """該頻道中看起來是藝人名（而非專輯）的 tag。

    判準有二：與頻道名相符，或出現在該頻道 ≥80% 的曲目上。後者需要至少 3 首
    才有統計意義，否則 2 首歌共有的任何 tag 都會被誤殺。
    """
    n = len(items)
    counts = collections.Counter(t for it in items for t in set(it.get("tags") or []))
    channel = _loose(_normalize_name(items[0]["channel_title"]))
    return {
        tag for tag, c in counts.items()
        if (n >= 3 and c >= 0.8 * n) or _loose(tag) == channel
    }


def _title_tag_index(tags: list[str], title: str) -> int | None:
    """曲名 tag 在 tags 中的位置。完全相符優先，其次包含關係。"""
    title_l = _loose(title)
    if not title_l:
        return None
    for i, tag in enumerate(tags):
        if _loose(tag) == title_l:
            return i
    for i, tag in enumerate(tags):
        tag_l = _loose(tag)
        if tag_l and (tag_l in title_l or title_l in tag_l):
            return i
    return None


def extract_frequency(items: list[dict]) -> dict[str, str | None]:
    """基準線 1：同頻道內出現在 2..n-1 首的 tag 視為專輯候選，取最長者。

    實測精確率 42%。在沒有專輯的廠牌頻道（#8 K-pop）會憑空生出假群組。
    """
    n = len(items)
    out: dict[str, str | None] = {it["video_id"]: None for it in items}
    if n < 2:
        return out
    counts = collections.Counter(t for it in items for t in set(it.get("tags") or []))
    for it in items:
        cands = [t for t in (it.get("tags") or []) if 2 <= counts[t] < n]
        if cands:
            out[it["video_id"]] = max(cands, key=len)
    return out


def extract_positional(items: list[dict]) -> dict[str, str | None]:
    """基準線 2：Topic 頻道中，曲名 tag 的前一個即專輯。

    實測精確率 58%。主要失敗是把合作藝人當成專輯。
    """
    out: dict[str, str | None] = {}
    topic = items[0]["channel_title"].strip().lower().endswith("- topic")
    for it in items:
        tags = it.get("tags") or []
        idx = _title_tag_index(tags, it["title"]) if topic else None
        out[it["video_id"]] = tags[idx - 1] if (idx is not None and idx > 0) else None
    return out


def extract_combined(items: list[dict]) -> dict[str, str | None]:
    """基準線 3：定位曲名 tag，排除藝人 tag，取剩下的最後一個。

    實測精確率 67%、召回率 54% —— 目前最好的規則，但仍低於 90% 門檻。
    剩餘的失敗集中在「合作藝人 vs 專輯」：兩者都緊鄰曲名 tag 之前。
    """
    out: dict[str, str | None] = {}
    artistish = _artistish_tags(items)
    for it in items:
        tags = it.get("tags") or []
        idx = _title_tag_index(tags, it["title"])
        if idx is None:
            out[it["video_id"]] = None
            continue
        cands = [t for t in tags[:idx] if t not in artistish and _loose(t)]
        out[it["video_id"]] = cands[-1] if cands else None
    return out


#: 只由假名／片假名組成的 tag。Topic 頻道會在每個**人名**後面附上轉寫，
#: 專輯名則沒有 —— 這是目前唯一能把「合作藝人」與「專輯」分開的結構訊號。
_KANA_ONLY = re.compile(r"^[぀-ヿー\s]+$")


def _tag_runs(tags: list[str]) -> list[tuple[int, list[str]]]:
    """把 tags 切成 ``[(起始索引, [名稱, 假名...]), ...]``。

    Topic 的 tags 是一串「實體」，每個實體由本名加零到多個假名轉寫組成::

        ['Avicii', 'アビーチー', 'アヴィーチー', 'True', 'Wake Me Up', 'ウェイクミーアップ']
         └────── 藝人（3 元素）──────────────┘  └專輯┘  └───── 曲名（2 元素）─────┘

    藝人與曲名幾乎都帶轉寫，專輯不帶。所以**單元素的 run 才可能是專輯**。
    """
    runs: list[tuple[int, list[str]]] = []
    for i, tag in enumerate(tags):
        if _KANA_ONLY.match(tag) and runs:
            runs[-1][1].append(tag)
        else:
            runs.append((i, [tag]))
    return runs


def extract_structured(items: list[dict]) -> dict[str, str | None]:
    """候選抽取器：用 Topic tags 的實體結構定位專輯。

    實測 **89.9% 精確率、25.8% 召回率**（160/178）。設計上刻意犧牲召回換精確率
    —— 抽不出來的曲目會退回藝人分群，那條路本來就能用，所以只有抽錯才有代價。

    五道條件，任何一道不過就不輸出：

    1. 頻道必須是 ``- Topic``（自動產生的 art track 才有結構化 tags）。
    2. 該曲目的 tags 必須含有至少一個純假名 tag。沒有假名就沒有判別訊號 ——
       中文／西洋內容因此完全沉默，這是已知限制而非缺陷。
    3. 曲名 tag 必須與標題**完全相符**（不接受包含關係，那條路徑誤判率高）。
    4. 候選是曲名之前、非藝人的最後一個 tag。藝人的判準是與頻道名相符，或
       出現在該頻道 ≥95% 的曲目上（需 ≥5 首才有統計意義）。
    5. 候選所在的 run 必須只有一個元素（沒有假名轉寫）。

    逐清單表現差異極大，這比合計數字更能說明它適用於什麼：專輯型的日文清單
    （#5 主題清單）91.4%／74.6%，而 #8 K-pop 一次都不出手（重要的負向測試）。
    """
    out: dict[str, str | None] = {it["video_id"]: None for it in items}
    if not items[0]["channel_title"].strip().lower().endswith("- topic"):
        return out

    n = len(items)
    counts = collections.Counter(t for it in items for t in set(it.get("tags") or []))
    channel = _loose(_normalize_name(items[0]["channel_title"]))
    # 95% / n>=5 比 combined 用的 80% / n>=3 保守：整份都是同一張原聲帶時，
    # 專輯 tag 會出現在幾乎每首歌上，過鬆的門檻會把專輯本身當成藝人濾掉。
    artistish = {
        tag for tag, c in counts.items()
        if (n >= 5 and c >= 0.95 * n) or _loose(tag) == channel
    }

    for it in items:
        tags = it.get("tags") or []
        if not any(_KANA_ONLY.match(t) for t in tags):
            continue
        title_l = _loose(it["title"])
        exact = [i for i, t in enumerate(tags) if _loose(t) == title_l]
        if not exact:
            continue
        idx = exact[0]
        cands = [t for t in tags[:idx] if t not in artistish and _loose(t)]
        if not cands:
            continue
        candidate = cands[-1]
        run = next((r for r in _tag_runs(tags) if r[0] < idx and r[1][0] == candidate), None)
        if run is None or len(run[1]) != 1:
            continue
        out[it["video_id"]] = candidate
    return out


#: 已註冊的抽取器。新的實作加在這裡就會自動進入評測。
EXTRACTORS = {
    "frequency": extract_frequency,
    "positional": extract_positional,
    "combined": extract_combined,
    "structured": extract_structured,
}

#: 基準線不受出貨門檻約束——它們留著當回歸比較，不是候選實作。
BASELINES = {"frequency", "positional", "combined"}


# ─────────────────────────────────────────────
# 評測
# ─────────────────────────────────────────────


class Score:
    """一個抽取器在一份或多份清單上的成績。"""

    def __init__(self) -> None:
        self.tp = 0          # 抽出且正確
        self.fp = 0          # 抽出但錯誤（含真值無專輯卻抽出東西）
        self.fp_no_album = 0 # 上者當中，真值根本沒有專輯的
        self.total = 0       # 有真值專輯的曲目數（召回率的分母）

    @property
    def emitted(self) -> int:
        return self.tp + self.fp

    @property
    def precision(self) -> float:
        return self.tp / self.emitted if self.emitted else 0.0

    @property
    def recall(self) -> float:
        return self.tp / self.total if self.total else 0.0

    def add(self, other: "Score") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fp_no_album += other.fp_no_album
        self.total += other.total


def evaluate(extractor, truth: dict[str, str | None], items: dict[str, dict],
             misses: list | None = None) -> Score:
    """在一份清單上評測。依頻道分組呼叫抽取器。"""
    score = Score()
    by_channel: dict[str, list[dict]] = collections.defaultdict(list)
    for vid in truth:
        by_channel[items[vid]["channel_title"]].append(items[vid])

    predictions: dict[str, str | None] = {}
    for channel_items in by_channel.values():
        predictions.update(extractor(channel_items))

    for vid, actual in truth.items():
        if actual:
            score.total += 1
        got = predictions.get(vid)
        if not got:
            continue
        if actual and canon(got) == canon(actual):
            score.tp += 1
        else:
            score.fp += 1
            if not actual:
                score.fp_no_album += 1
            if misses is not None:
                misses.append((items[vid], actual, got))
    return score


def run(names: list[str], verbose: str | None) -> int:
    cases = load_cases()
    if not cases:
        sys.exit("沒有可用的樣本。先跑 tests/dev/ytm_probe.py 收樣本，並確認 scripts/cache/ 有對應快取。")

    totals: dict[str, Score] = {n: Score() for n in names}
    header = "".join(f"{n[:11]:>13s}" for n in names)
    print(f"{'清單':13s} | {'曲目':>4s} | {'有專輯':>5s} |{header}")
    print("-" * (28 + 13 * len(names)))

    for pid, truth, items in cases:
        with_album = sum(1 for a in truth.values() if a)
        cells = ""
        for name in names:
            s = evaluate(EXTRACTORS[name], truth, items)
            totals[name].add(s)
            cells += f"{s.precision:>6.0%}/{s.recall:<6.0%}" if s.emitted or with_album else f"{'—':>13s}"
        print(f"{LABELS.get(pid, pid[:13]):13s} | {len(truth):>4d} | {with_album:>5d} |{cells}")

    print("-" * (28 + 13 * len(names)))
    cells = "".join(f"{totals[n].precision:>6.0%}/{totals[n].recall:<6.0%}" for n in names)
    print(f"{'合計 (精確/召回)':13s} | {'':>4s} | {'':>5s} |{cells}")

    print(f"\n出貨門檻：精確率 ≥ {PRECISION_GATE:.0%}（召回率不設限）")
    failed = False
    for name in names:
        s = totals[name]
        ok = s.precision >= PRECISION_GATE
        failed |= (name not in BASELINES) and not ok
        note = ""
        if s.fp_no_album:
            note = f"，其中 {s.fp_no_album} 筆是在「真值無專輯」的曲目上憑空抽出"
        print(
            f"  {'✓' if ok else '✗'} {name:12s} 精確率 {s.precision:.1%} "
            f"({s.tp}/{s.emitted})、召回率 {s.recall:.1%} ({s.tp}/{s.total}){note}"
        )

    if verbose:
        print(f"\n=== {verbose} 的失敗案例（前 15 筆）===")
        misses: list = []
        for pid, truth, items in cases:
            evaluate(EXTRACTORS[verbose], truth, items, misses)
        for item, actual, got in misses[:15]:
            print(f"  {item['title'][:34]:34s} | 真值 {str(actual)[:22]:22s} | 抽到 {got[:22]}")
            print(f"      tags {item.get('tags')}")

    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--baseline", action="store_true", help="只跑基準線抽取器")
    parser.add_argument("-v", "--verbose", metavar="NAME", default=None,
                        help="印出指定抽取器的失敗案例")
    args = parser.parse_args(argv)

    names = sorted(BASELINES) if args.baseline else list(EXTRACTORS)
    if args.verbose and args.verbose not in EXTRACTORS:
        sys.exit(f"未知的抽取器 {args.verbose!r}。可用：{', '.join(EXTRACTORS)}")
    return run(names, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
