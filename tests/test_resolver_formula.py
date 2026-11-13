"""
test_resolver_formula.py — 藝人辨識公式（scripts/resolver.py）

1. N()：後綴、全形、別名，以及發行商頻道終於比得中（v3 的 bug：「JYP Entertainment」變成藝人 jyp）。
2. 四個來源各自取到的候選：P（Art Track 描述、Topic 頻道、Spotify artists[0]）、T 的兩種樣式、C。
3. 公式本身：用手算得出的分數驗證加權、同分順序、θ、margin、corr 項與使用者更正。
4. 迴歸：用測試資料與 ``resolver_params.json`` 重算 B³，分數不可低於檔案裡記錄的值。
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "dev"))

from scripts import resolver  # noqa: E402
from scripts.resolver import Candidate, decide, normalize  # noqa: E402
from scripts.schemas import ArtistRef, EnrichedPlaylistItem  # noqa: E402

FALLBACK = {"w_P": 1.0, "w_A": 0.0, "w_T": 0.25, "w_C": 0.5, "beta": 0.0, "theta": 0.0, "tau": 0.2}
FUSED = {"w_P": 1.0, "w_A": 1.0, "w_T": 0.0, "w_C": 0.0, "beta": 0.0, "theta": 0.0, "tau": 0.05}


def item(n: int, title: str = "", channel: str = "", description: str = "", **extra) -> EnrichedPlaylistItem:
    return EnrichedPlaylistItem(
        playlist_item_id=f"p{n}", video_id=f"v{n}", position=n,
        added_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        title=title, channel_title=channel, description=description, **extra,
    )


def test_normalize():
    assert normalize("Alan Walker - Topic") == normalize("Alan Walker") == "alanwalker"
    assert normalize("AdeleVEVO") == "adele"
    assert normalize("BLACKPINK Official") == "blackpink"
    assert normalize("Different") == "different", "「ent」只在獨立成字時才算後綴"
    assert normalize("ＢＴＳ") == "bts"
    assert normalize("JYP Entertainment") in resolver._DISTRIBUTOR_KEYS
    assert normalize("HYBE LABELS") in resolver._DISTRIBUTOR_KEYS
    assert normalize("BTS") not in resolver._DISTRIBUTOR_KEYS
    lookup = resolver.alias_map({"Jay Chou": ["周杰倫", "周杰伦"]})
    assert normalize("周杰倫", lookup) == normalize("Jay Chou", lookup) == "jaychou"
    print("  ✓ N()：後綴、全形、別名，發行商頻道比得中")


def test_first_artist():
    cases = {
        "LE SSERAFIM (르세라핌) x ILLIT (아일릿)": "LE SSERAFIM",
        "Hiroyuki Sawano feat. Laco": "Hiroyuki Sawano",
        "SawanoHiroyuki[nZk]:mizuki": "SawanoHiroyuki[nZk]",
        "TXT (투모로우바이투게더), Anitta": "TXT",
        "Wu Bai & China Blue": "Wu Bai",
        "BTS (방탄소년단)": "BTS",
        "HUNTR/X": "HUNTR/X",
    }
    for raw, expected in cases.items():
        assert resolver.first_artist(raw) == expected, (raw, resolver.first_artist(raw))
    print("  ✓ 合作掛名只取第一位、去掉結尾括號")


def test_sources():
    atv = item(0, "Song", "Some Label", "Provided to YouTube by Label\nSong · Artist A · Artist B\nAlbum")
    assert resolver.platform_artist(atv) == "Artist A"
    assert resolver.platform_artist(item(1, "Faded", "Alan Walker - Topic")) == "Alan Walker"
    assert resolver.platform_artist(item(2, "Faded", "Alan Walker")) == ""
    spotify = item(3, "Song", "", platform="spotify", artists=[ArtistRef(name="IU", id="x")])
    assert resolver.platform_artist(spotify) == "IU"
    assert [c.source for c in resolver.candidates(spotify)] == ["P"], "Spotify 沒有頻道，不產生 C"

    assert resolver.title_artist("【YOASOBI】夜に駆ける") == ("YOASOBI", 0.95)
    assert resolver.title_artist("BTS (방탄소년단) - Dynamite (Official MV)") == ("BTS", 0.80)
    assert resolver.title_artist("ILLIT (아일릿) 'Magnetic' Official MV") is None

    label = resolver.candidates(item(4, 'TWICE "TT" M/V', "JYP Entertainment"))
    assert label == [], "廠牌頻道不能當藝人，標題又不合樣式，就沒有候選"
    agent = resolver.candidates(item(5, 'TWICE "TT" M/V', "JYP Entertainment"), {"artist": "TWICE", "conf": "medium"})
    assert agent == [Candidate("A", "twice", 0.5, "TWICE")]
    print("  ✓ 來源 P／A／T／C 的候選與信心")


def test_formula_by_hand():
    params = {"w_P": 1.0, "w_A": 1.0, "w_T": 0.5, "w_C": 0.5, "beta": 0.4, "theta": 0.3}
    norm = 1.0 + 1.0 + 0.5 + 0.5 + 0.4
    # P 給 alpha（1.0×1.0），T 給 beta（0.5×0.8 = 0.4）→ alpha，margin = 0.6 / 3.4
    cands = [[Candidate("P", "alpha", 1.0, "Alpha"), Candidate("T", "beta", 0.8, "Beta")]]
    [(key, margin, source)] = decide(["ch"], cands, params)
    assert (key, source) == ("alpha", "P") and abs(margin - 0.6 / norm) < 1e-9

    # 同分：T 0.5×0.8 = 0.4、C (2/3)×0.6 = 0.4 → 依 P > A > T > C 取 T
    tie = dict(params, w_C=2 / 3)
    [(key, _, source)] = decide(["ch"], [[Candidate("C", "delta", 0.6, "D"), Candidate("T", "gamma", 0.8, "G")]], tie)
    assert (key, source) == ("gamma", "T")

    # 低於 θ：只有 C，0.5×0.6 = 0.3 < 0.31 → unknown
    [(key, _, _)] = decide(["ch"], [[Candidate("C", "delta", 0.6, "D")]], dict(params, theta=0.31))
    assert key == resolver.UNKNOWN

    # 釘選項目原樣保留
    assert decide(["ch"], [None], params)[0][0] == resolver.PINNED
    print("  ✓ 加權、同分順序、θ、margin 與手算一致")


def test_corr_term():
    # 同一個頻道：三首只有 C=hiro（0.5×0.6 = 0.3），第四首標題被誤拆成 vv（0.5×0.8 = 0.4）。
    hiro = Candidate("C", "hiro", 0.6, "Hiro")
    cands = [[hiro], [hiro], [hiro], [Candidate("T", "vv", 0.8, "VV"), hiro]]
    channels = ["Hiro"] * 4
    base = {"w_P": 1.0, "w_A": 0.0, "w_T": 0.5, "w_C": 0.5, "theta": 0.0}
    assert decide(channels, cands, dict(base, beta=0.0))[3][0] == "vv"
    # β = 0.4：corr(hiro) = 3/3 → 0.3 + 0.4 = 0.7 > 0.4 → 改判 hiro
    assert decide(channels, cands, dict(base, beta=0.4))[3][:1] == ("hiro",)
    # 沒有頻道（Spotify）就沒有同頻道佐證
    assert decide([""] * 4, cands, dict(base, beta=0.4))[3][0] == "vv"
    print("  ✓ corr 項：同頻道多數改判誤拆的標題；沒有頻道時不作用")


def test_resolve_user_and_display():
    items = [
        item(0, "A", "Jay Chou - Topic"),
        item(1, "B", "Jay Chou - Topic"),
        item(2, 'TWICE "TT" M/V', "JYP Entertainment"),
        item(3, "gone", "", is_available=False),
    ]
    found = resolver.resolve(items, user={"p2": "TWICE"}, fallback=FALLBACK)
    assert [r.key for r in found] == ["jaychou", "jaychou", "twice", resolver.PINNED]
    assert found[0].name == "Jay Chou", "顯示名稱用最常見的原始寫法"
    assert (found[2].name, found[2].source) == ("TWICE", "U"), "使用者更正直接採用"
    assert resolver.resolve(items[2:3], fallback=FALLBACK)[0].key == resolver.UNKNOWN
    assert resolver.candidates(item(9, "x", "Unknown")) == [], "叫 Unknown 的頻道不能變成 unknown 群以外的同名群"
    print("  ✓ 使用者更正優先、釘選保留、顯示名稱取最常見寫法")


def test_fused_and_fallback_per_item():
    # 融合權重只有 A＋P：沒有 A 的歌若也用融合權重，只剩 P，Topic 以外的頻道全變 unknown。
    items = [
        item(0, "Song", "Some Uploader"),                       # A 答 Beyond
        item(1, "海闊天空", "Beyond"),                           # 沒送 Agent：C 只在 fallback 有權重
        item(2, "Faded", "Alan Walker - Topic"),                 # P 與 A 不一致 → 待確認
    ]
    labels = {"p0": {"artist": "Beyond", "conf": "high"}, "p2": {"artist": "K-391", "conf": "high"}}
    found = resolver.resolve(items, labels=labels, fused=FUSED, fallback=FALLBACK)
    assert [r.key for r in found] == ["beyond", "beyond", "alanwalker"]
    assert [r.source for r in found] == ["A", "C", "P"], "有 A 用 fused，沒有 A 用 fallback；同分時 P 優先"
    # 只有頻道：0.5×0.6 / 1.75 ≈ 0.17 < 0.2；P 與 A 不一致：margin 0 < 0.05 → 兩首都標待確認
    assert [r.uncertain for r in found] == [False, True, True]
    assert resolver.resolve(items[1:2], fused=FUSED, fallback=FALLBACK)[0].key == "beyond"
    print("  ✓ 有 A 的歌用 fused、沒有的用 fallback；P／A 不一致標待確認")


def test_label_protocol():
    import json
    import tempfile

    from scripts import labels

    items = [
        item(0, "Faded", "Alan Walker - Topic"),          # 有 P、有 C，最有把握
        item(1, 'TWICE "TT" M/V', "JYP Entertainment"),   # 沒有任何候選 → margin 0
        item(2, "BTS - Dynamite", "Some Uploader"),       # T 與 C 互相競爭
        item(3, "gone", "", is_available=False),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        cached = {"items": {"youtube:v0": {"artist": "Alan Walker"}}, "aliases": {}}
        summary = labels.write_request(items, out, cached, max_items=10)
        rows = [json.loads(line) for line in (out / labels.REQUEST_FILE).read_text(encoding="utf-8").splitlines()]
        assert [r["pos"] for r in rows] == [1, 2] and summary["cached"] == 1, "快取命中的歌不再送出"
        assert summary["est_tokens"] > 0 and "p" not in rows[0]

        empty = labels.load_cache(out / "none.json")
        budget = labels.write_request(items, out, empty, max_items=1)
        assert budget["pending"] == 1 and budget["by_formula"] == 2
        assert json.loads((out / labels.REQUEST_FILE).read_text(encoding="utf-8"))["pos"] == 1, "最沒把握的先送 Agent"

        request = out / labels.REQUEST_FILE
        labels.write_request(items, out, empty, max_items=10)
        good = {"labels": {"0": {"artist": "Alan Walker", "conf": "high"}, "1": {"artist": "TWICE", "conf": "high"},
                           "2": {"artist": "unknown", "conf": "medium"}, "3": None}, "aliases": {}}
        bad_cases = {
            "LABELS_INVALID": [
                dict(good, labels={**good["labels"], "3": {"artist": "x", "conf": "high"}}),  # 釘選項目
                dict(good, labels={**good["labels"], "9": {"artist": "x", "conf": "high"}}),  # 不在快照
                {"labels": {"0": {"artist": "A", "conf": "low"}}},                            # conf 不合格
                {"labels": {}, "aliases": {"A": "B"}},                                         # 別名格式
            ],
            "LABELS_INCOMPLETE": [{"labels": {"0": {"artist": "Alan Walker", "conf": "high"}}}],
        }
        for code, docs in bad_cases.items():
            for doc in docs:
                try:
                    labels.validate(doc, items, request)
                except labels.LabelError as exc:
                    assert exc.code == code, (code, exc.code, doc)
                else:
                    raise AssertionError(f"應該回 {code}：{doc}")
        good["labels"].pop("3")
        answers, _ = labels.validate(good, items, request)
        assert answers["p1"] == {"artist": "TWICE", "conf": "high"}
        found = resolver.resolve(items, labels=answers, fused=FUSED, fallback=FALLBACK)
        assert found[1].key == "twice" and found[2].source != "A", "artist=unknown 表示棄權，不產生 A 候選"

        cache = out / "cache.json"
        labels.save_cache(cache, items, {"p1": {"artist": "TWICE", "conf": "high"}}, "U")
        labels.save_cache(cache, items, {"p1": {"artist": "GOT7", "conf": "high"}, "p2": {"artist": "BTS"}}, "A",
                          {"BTS": ["방탄소년단"]})
        stored = labels.load_cache(cache)
        assert stored["items"]["youtube:v1"]["artist"] == "TWICE", "使用者的更正不會被 Agent 蓋掉"
        assert stored["aliases"] == {"BTS": ["방탄소년단"]}, "別名也存進快取"
        agent, user = labels.from_cache(items, stored)
        assert user == {"p1": "TWICE"} and agent == {"p2": {"artist": "BTS", "conf": "high"}}

        spotify = [item(n, "s", "", platform="spotify", artists=[ArtistRef(name=name)]) for n, name in
                   enumerate(["IU", "IU", "아이유"])]
        sp = labels.write_request(spotify, out, empty, max_items=10)
        assert sp["pending"] == 2 and "aliases" in sp["instructions"], "Spotify 只送不同的藝人名稱"
        assert labels.validate({"labels": {}, "aliases": {"IU": ["아이유"]}}, spotify, None)[1] == {"IU": ["아이유"]}
        merged = resolver.resolve(spotify, aliases={"IU": ["아이유"]}, fallback=FALLBACK)
        assert len({r.key for r in merged}) == 1, "Spotify 靠別名合併同一位藝人的不同寫法"
    print("  ✓ 標註協定：快取、預算模式、LABELS_INVALID／INCOMPLETE、棄權、使用者更正優先、Spotify")


def test_budget_mode_end_to_end():
    """K-pop 測試資料（廠牌頻道，只用備援公式有一半認不出來）走一次正式指令：
    label-request --max-items 50 → 錄製的 Agent 答案 → optimize --labels。"""
    import json
    import os
    import subprocess
    import tempfile

    import resolver_bench as bench

    pid = "PLOHoVaTp8R7ccrQM3EpCTVDdwHhXrJhXS"
    items = bench.load_items(pid)
    recorded, aliases = bench.agent_labels(pid)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = dict(os.environ, YT_SKILL_HOME=str(tmp / "home"), PYTHONIOENCODING="utf-8")
        current = tmp / "current.json"
        current.write_text(json.dumps([i.model_dump(mode="json") for i in items], ensure_ascii=False), encoding="utf-8")

        def run(*args: str) -> dict:
            out = subprocess.run([sys.executable, "-m", "scripts.yt_tool", *args], cwd=ROOT, env=env,
                                 capture_output=True, text=True, encoding="utf-8")
            return json.loads(out.stdout.strip().splitlines()[-1])

        summary = run("label-request", str(current), "--max-items", "50")
        movable = sum(1 for i in items if i.is_available)
        assert summary["pending"] == 50 and summary["by_formula"] == movable - 50, summary
        asked = [json.loads(line)["pos"] for line in Path(summary["request_file"]).read_text(encoding="utf-8").splitlines()]
        by_pos = {i.position: i for i in items}
        answers = {str(pos): recorded[by_pos[pos].playlist_item_id] for pos in asked}

        def optimize(doc: dict) -> dict:
            (tmp / "labels.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            return run("optimize", str(current), "--labels", str(tmp / "labels.json"),
                       "--target-out", str(tmp / "target.json"), "--out", str(tmp / "changes.json"))

        missing = dict(answers)
        missing.pop(str(asked[0]))
        assert optimize({"labels": missing, "aliases": aliases})["code"] == "LABELS_INCOMPLETE"

        not_asked = next(i for i in items if i.is_available and i.position not in asked)
        extra = {str(not_asked.position): recorded[not_asked.playlist_item_id]}
        result = optimize({"labels": {**answers, **extra}, "aliases": aliases})
        assert result["status"] == "success", result
        assert result["agent_labels"] == 51, "請求以外多給的答案也收下，並存進標籤快取"

        # 顯示名稱是「最常見的寫法」，會被別首歌的 Agent 答案影響，所以比的是正規化後的鍵。
        lookup = resolver.alias_map(aliases)
        fallback = {i.playlist_item_id: r.key for i, r in zip(items, resolver.resolve(items, aliases=aliases))}
        groups = {r["playlist_item_id"]: r["group"] for r in json.loads((tmp / "target.json").read_text(encoding="utf-8"))}
        left = [i for i in items if i.is_available and i.position not in asked and i is not not_asked]
        assert all(groups[i.playlist_item_id] != resolver.UNKNOWN for i in left), "沒送 Agent 的歌不會因為融合權重的 T、C 是 0 而變成 unknown"
        assert all(resolver.normalize(groups[i.playlist_item_id], lookup) == fallback[i.playlist_item_id] for i in left), \
            "沒送 Agent 的歌照備援公式分組"
        assert result["unknown_ratio"] < 0.05, f"送 Agent 的是最沒把握的 50 首，unknown 應幾乎清空：{result['unknown_ratio']}"
        again = run("label-request", str(current), "--max-items", "50")
        assert again["cached"] == 51, "答過的歌下一次不再送出"
    print("  ✓ 預算模式端對端：只送 50 首、其餘照備援公式、缺答案才 LABELS_INCOMPLETE、答案進快取")


def test_scores_not_below_recorded():
    import json

    import resolver_bench as bench

    stored = json.loads(resolver.PARAMS_PATH.read_text(encoding="utf-8"))
    checked = 0
    for mode, scores in stored.get("scores", {}).items():
        if mode in stored.get("unfitted", []):
            continue
        result = bench.evaluate(bench.predict_formula(stored["weights"][mode], use_agent=mode == "fused"))
        for split, recorded in scores.items():
            got = result[split]
            assert got["f1"] >= recorded["f1"] - 1e-4, (mode, split, got["f1"], recorded["f1"])
            assert got["unknown_rate"] <= recorded["unknown_rate"] + 1e-4, (mode, split, got, recorded)
            checked += 1
    assert checked, "resolver_params.json 至少要有一組擬合過的分數"
    print(f"  ✓ 測試資料重算 B³：{checked} 項不低於 resolver_params.json 的記錄")


TESTS = [
    test_normalize,
    test_first_artist,
    test_sources,
    test_formula_by_hand,
    test_corr_term,
    test_resolve_user_and_display,
    test_fused_and_fallback_per_item,
    test_label_protocol,
    test_budget_mode_end_to_end,
    test_scores_not_below_recorded,
]


def run_all_tests() -> bool:
    logging.disable(logging.ERROR)
    passed = failed = 0
    print(f"\n{'=' * 60}\n Resolver formula\n{'=' * 60}")
    for test in TESTS:
        try:
            test()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{'=' * 60}\n Results: {passed}/{passed + failed} passed, {failed} failed\n{'=' * 60}")
    return failed == 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(0 if run_all_tests() else 1)
