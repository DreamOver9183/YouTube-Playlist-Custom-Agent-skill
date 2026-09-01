"""
ytm_probe.py — ytmusicapi 取樣：用真實清單比對「標題猜藝人」與「音樂目錄實體」

**這是開發者工具，不是 skill 的一部分。** 一般使用 skill 時不會、也不該執行它。
擋門與 ``learn_scenarios.py`` 相同：程式放在 ``tests/dev/``（不在任何 skill 載入
路徑）、需要專用進入點、且沒有設 ``YT_DEV_YTM=1`` 就完全不寫檔。

它在做什麼
----------

現行的藝人辨識是三層 pipeline（標題 regex → 頻道名 → 模糊比對），本質上是在
**猜**。猜錯的樣子在真實清單上長這樣：

- 「晩餐歌 - Bansanka」→ 藝人被判成 ``晩餐歌``（歌名當成藝人）
- 「EGOIST『BANG!!!』Music Video（TVアニメ「ビルディバイド…」→ 藝人被判成整串標題
- 同一位藝人被切成 ``kenshi yonezu`` 與 ``米津玄師`` 兩組（跨語言別名沒合併）

YouTube Music 的後端本來就把每首曲子掛在一個**藝人實體**上（帶 browse id），
專輯也是。這支工具把兩邊擺在一起，量化「換成音樂目錄實體能修掉多少猜測」。

它**只讀不寫 YouTube**：``YTMusic()`` 不帶認證即可讀公開清單，不需要 OAuth、
不需要 Google Cloud 專案、不消耗任何 API 配額。

為什麼要累積樣本
----------------

單一清單的比對說服力不足 —— 日文歌單與器樂/原聲帶清單的失敗模式完全不同
（前者卡在跨語言別名，後者卡在專輯歸屬）。``YT_DEV_YTM=1`` 會把每次取樣落檔到
``tests/dev/ytm_samples/``，``--summary`` 再把累積下來的樣本彙整成一張表。
決定要不要換掉辨識層之前，先讓樣本數說話。

用法
----

.. code-block:: bash

    # 1. 唯讀比對（不寫任何檔案）
    python tests/dev/ytm_probe.py https://youtube.com/playlist?list=PL...

    # 2. 收錄樣本
    YT_DEV_YTM=1 python tests/dev/ytm_probe.py PL... PL...

    # 3. 彙整已收錄的所有樣本
    python tests/dev/ytm_probe.py --summary

比對需要該清單的本地 API 快取（``scripts/cache/playlist_<id>.json``）才能跑
現行 resolver。沒有快取時只會印 ytmusicapi 這一側，並提示先跑 ``fetch``。

已知限制
--------

ytmusicapi 看到的是 **YouTube Music 目錄**，不是 YouTube 清單本身。實測兩份清單
都少了幾首（192→188、83→82），少掉的是目錄裡沒有對應曲目的一般 YouTube 影片。
重排要求目標順序是原清單的完整排列，所以**這一側的資料不能直接拿來當排序輸入**，
只能當辨識層的參考來源；缺漏的項目必須退回現行 pipeline。這是換掉辨識層之前
必須先解決的問題，工具會把缺漏清單完整印出來。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

# 與 yt_tool.py 同一道保險：Windows 主控台預設 cp950，直接 print 中日文會炸。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.youtube_api import extract_playlist_id  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "cache"
SAMPLE_DIR = Path(__file__).resolve().parent / "ytm_samples"

#: 沒設這個就完全不寫檔。與 learn_scenarios.py 的 YT_DEV_LEARN 同一種擋門。
WRITE_ENABLED = os.environ.get("YT_DEV_YTM") == "1"


# ─────────────────────────────────────────────
# 取樣
# ─────────────────────────────────────────────


def fetch_ytm(playlist_id: str) -> list[dict]:
    """從 YouTube Music 讀取整份清單（不帶認證、0 配額）。"""
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        sys.exit(
            "需要 ytmusicapi。這是開發者工具的相依套件，不在 requirements.txt 裡：\n"
            "    pip install ytmusicapi"
        )
    return YTMusic().get_playlist(playlist_id, limit=None).get("tracks", [])


def load_api_cache(playlist_id: str) -> list[dict] | None:
    """讀取 ``fetch`` 留下的本地快取；沒有就回傳 None。"""
    path = CACHE_DIR / f"playlist_{playlist_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def resolve_with_current_pipeline(api_items: list[dict]) -> list[str]:
    """跑現行的三層藝人辨識，回傳與輸入同序的 artist_key 清單。"""
    import logging

    logging.disable(logging.CRITICAL)
    from scripts.optimizer import group_by_artist
    from scripts.schemas import EnrichedPlaylistItem

    items = [EnrichedPlaylistItem(**raw) for raw in api_items]
    aliases = ROOT / "data" / "artist_aliases.json"
    _, _, resolutions, _ = group_by_artist(
        items, aliases if aliases.exists() else None, "first_appearance", None
    )
    return [r.artist_key for r in resolutions]


def build_sample(playlist_id: str) -> dict:
    """取一份清單的比對樣本。純計算，不寫檔。"""
    tracks = fetch_ytm(playlist_id)
    ytm_by_vid = {t["videoId"]: t for t in tracks}
    api_items = load_api_cache(playlist_id)

    sample: dict = {
        "playlist_id": playlist_id,
        "ytm_track_count": len(tracks),
        "video_types": dict(Counter(t.get("videoType") for t in tracks)),
        "album_coverage": sum(1 for t in tracks if t.get("album")),
        "album_distinct": len({(t.get("album") or {}).get("name") for t in tracks if t.get("album")}),
        "artist_entity_coverage": sum(
            1 for t in tracks if (t.get("artists") or [{}])[0].get("id")
        ),
        "api_item_count": len(api_items) if api_items else None,
        "invisible_to_ytm": [],
        "rows": [],
    }

    # 掛名冒號比例：ytmusicapi 的藝人是「發行掛名」不是「藝人本人」，
    # "SawanoHiroyuki[nZk]:mizuki" 這種會把同一位藝人切成很多組。
    primary = [(t.get("artists") or [{}])[0].get("name", "") for t in tracks]
    sample["colon_credit_ratio"] = (
        sum(1 for a in primary if ":" in a) / len(primary) if primary else 0.0
    )

    if api_items is None:
        return sample

    keys = resolve_with_current_pipeline(api_items)

    # 群數比是這支工具最有判斷力的單一數字，而且不需要認證就算得出來：
    #   < 1  ytmusicapi 併得比現行 pipeline 多 → 換過去是淨賺
    #   > 1  ytmusicapi 切得比現行 pipeline 細 → 換過去會打散群組
    # 實測：日文歌單 0.86（賺），澤野弘之 1.40（虧）。分層取樣就照這個數字分。
    ytm_keys = {a.split(":")[0].strip().lower() for a in primary if a}
    cur_keys = {k.lower() for k in keys}
    sample["group_count_ratio"] = round(len(ytm_keys) / len(cur_keys), 2) if cur_keys else None
    for item, key in zip(api_items, keys):
        track = ytm_by_vid.get(item["video_id"])
        if track is None:
            sample["invisible_to_ytm"].append(
                {
                    "video_id": item["video_id"],
                    "title": item["title"],
                    "channel_title": item["channel_title"],
                }
            )
            continue
        artists = [a["name"] for a in (track.get("artists") or [])]
        sample["rows"].append(
            {
                "video_id": item["video_id"],
                "title": item["title"],
                "channel_title": item["channel_title"],
                "current_key": key,
                "ytm_artists": artists,
                "ytm_album": (track.get("album") or {}).get("name"),
                "video_type": track.get("videoType"),
                # 現行 key 若不是 ytmusicapi 藝人名的子字串，就是兩邊真的不同意。
                # 大小寫與空白差異不算分歧（"eir aoi" vs "Eir Aoi"）。
                "agrees": any(
                    key.lower().replace(" ", "") in a.lower().replace(" ", "")
                    or a.lower().replace(" ", "") in key.lower().replace(" ", "")
                    for a in artists
                ),
            }
        )
    return sample


# ─────────────────────────────────────────────
# 呈現
# ─────────────────────────────────────────────


def print_sample(sample: dict) -> None:
    pid = sample["playlist_id"]
    print("=" * 96)
    print(f"{pid}")

    api_n, ytm_n = sample["api_item_count"], sample["ytm_track_count"]
    if api_n is None:
        print(f"  ytmusicapi: {ytm_n} 首。沒有本地 API 快取，無法比對現行 resolver。")
        print(f"  先跑：python -m scripts.yt_tool fetch {pid} --out data/current.json --refresh")
        return

    invisible = sample["invisible_to_ytm"]
    print(f"  API {api_n} 首 / ytmusicapi {ytm_n} 首 — 音樂目錄看不到 {len(invisible)} 首")
    for miss in invisible:
        print(f"     ✗ {miss['channel_title'][:26]:26s} | {miss['title'][:52]}")

    rows = sample["rows"]
    if not rows:
        return
    agree = sum(1 for r in rows if r["agrees"])
    print(
        f"  藝人辨識一致: {agree}/{len(rows)} ({agree / len(rows):.0%})"
        f" — 分歧 {len(rows) - agree} 筆"
    )
    print(f"  videoType: {sample['video_types']}")
    print(
        f"  album 有值: {sample['album_coverage']}/{ytm_n}"
        f"（{sample['album_distinct']} 張不同專輯）"
        f" | 藝人帶 browse id: {sample['artist_entity_coverage']}/{ytm_n}"
    )
    ratio = sample.get("group_count_ratio")
    if ratio is not None:
        verdict = "換過去是淨賺" if ratio < 1 else "換過去會打散群組"
        print(
            f"  群數比 YTM/現行: {ratio}（{verdict}）"
            f" | 掛名冒號比例: {sample['colon_credit_ratio']:.1%}"
        )

    disagreements = [r for r in rows if not r["agrees"]]
    if disagreements:
        print(f"\n  {'現行 resolver 判定':32s} | {'ytmusicapi 藝人':22s} | 標題")
        print(f"  {'-' * 92}")
        for row in disagreements[:25]:
            arts = " / ".join(row["ytm_artists"]) or "(無)"
            print(f"  {row['current_key'][:32]:32s} | {arts[:22]:22s} | {row['title'][:34]}")
        if len(disagreements) > 25:
            print(f"  … 另有 {len(disagreements) - 25} 筆，完整內容見樣本檔")


def print_summary() -> None:
    """彙整 ytm_samples/ 底下所有累積的樣本。"""
    if not SAMPLE_DIR.exists() or not any(SAMPLE_DIR.glob("*.json")):
        sys.exit(f"還沒有任何樣本。先跑：YT_DEV_YTM=1 python {Path(__file__).name} <playlist_id>")

    samples = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SAMPLE_DIR.glob("*.json"))]
    print(
        f"{'playlist':24s} | {'API':>5s} | {'YTM':>5s} | {'漏':>3s} | {'一致率':>7s}"
        f" | {'album':>7s} | {'群數比':>7s}"
    )
    print("-" * 88)
    tot_rows = tot_agree = tot_missing = 0
    gain = loss = 0
    for s in samples:
        rows = s["rows"]
        if not rows:
            continue
        agree = sum(1 for r in rows if r["agrees"])
        tot_rows += len(rows)
        tot_agree += agree
        tot_missing += len(s["invisible_to_ytm"])
        album_pct = s["album_coverage"] / s["ytm_track_count"] if s["ytm_track_count"] else 0
        ratio = s.get("group_count_ratio")
        if ratio is not None:
            (gain, loss) = (gain + 1, loss) if ratio < 1 else (gain, loss + 1)
        print(
            f"{s['playlist_id'][:24]:24s} | {str(s['api_item_count']):>5s} | {s['ytm_track_count']:>5d}"
            f" | {len(s['invisible_to_ytm']):>3d} | {agree / len(rows):>6.0%} | {album_pct:>6.0%}"
            f" | {'-' if ratio is None else f'{ratio:.2f}':>7s}"
        )
    print("-" * 88)
    if tot_rows:
        print(
            f"{'合計':24s} | {'':>5s} | {tot_rows:>5d} | {tot_missing:>3d}"
            f" | {tot_agree / tot_rows:>6.0%} |"
        )
    print(f"\n樣本數 {len(samples)} 份（淨賺 {gain} / 打散 {loss}）。樣本檔：{SAMPLE_DIR}")
    print(
        "取樣目標 12 份：要有 95% 把握撞見「影響 ≥25% 清單」的失敗模式，"
        "需要 1-0.75^n ≥ 0.95，也就是 n ≥ 11。"
    )


# ─────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--summary":
        print_summary()
        return 0

    for raw in argv:
        playlist_id = extract_playlist_id(raw)
        sample = build_sample(playlist_id)
        print_sample(sample)
        if WRITE_ENABLED:
            SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
            out = SAMPLE_DIR / f"{playlist_id}.json"
            out.write_text(
                json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n  → 已收錄樣本：{out.relative_to(ROOT)}")
        else:
            print("\n  （唯讀模式。要收錄樣本請設 YT_DEV_YTM=1）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
