# 開發者工具

**這裡的東西不是 skill 的一部分。** 一般使用 skill 時不會、也不該執行它們。
`SKILL.md`（Claude 版與 Gemini 版）與 `AGENTS.md` 都不提這個資料夾。

共同的擋門：

1. 程式放在 `tests/dev/`，不在任何 skill 載入路徑內。
2. 每支都有自己的專用進入點，沒有掛進 CI 或任何預設測試流程。
3. 會寫檔的工具，沒有設對應的環境變數就完全不寫檔。

## 工具清單

| 工具 | 用途 | 寫檔擋門 |
|---|---|---|
| `grouping_gate_bench.py` | 用 `scripts/cache/` 收錄的真實清單校準分群效益閘門的兩道門檻（`_UNKNOWN_RATIO_GATE`、`_EFFECTIVE_GROUPING_GATE`）。門檻改動後一定要重跑 `--check` | 不寫檔 |
| `resolver_bench.py` | M3 的藝人辨識評測台：以 B³（精確率／召回率／F1）與 unknown 率，比較辨識結果與近似真值、人工真值；也負責建立 `tests/fixtures/playlists/` 與寫入 `tests/fixtures/baseline.json` | `YT_DEV_WRITE=1` |
| `ytm_probe.py` | 用 ytmusicapi 取得音樂目錄的藝人／專輯標註，產生 `ytm_samples/`，作為開發期的近似真值 | `YT_DEV_YTM=1` |
| `ytm_samples/*.json` | 11 份清單、928 列 ytmusicapi 標註。藝人是「發行掛名」（例如 `SawanoHiroyuki[nZk]:mizuki`），不是一人一 key，當真值使用時要先正規化 | — |

ytmusicapi 只用在開發期，刻意不放進 `requirements.txt`（執行期引進已於 2026-09-03 否決，
見 `docs/BACKLOG.md` §1）。

## 用法

```bash
python tests/dev/grouping_gate_bench.py           # 印出全部樣本的閘門指標
python tests/dev/grouping_gate_bench.py --check   # 驗證閘門恰好攔到該攔的清單

python tests/dev/resolver_bench.py --per-list     # 現行辨識在訓練集／保留集／人工真值上的 B³ 分數
YT_DEV_WRITE=1 python tests/dev/resolver_bench.py --baseline   # 寫入 tests/fixtures/baseline.json

pip install ytmusicapi
YT_DEV_YTM=1 python tests/dev/ytm_probe.py <playlist_id_or_url>
```

`grouping_gate_bench.py` 依賴被 gitignore 的 `scripts/cache/`，所以全新 clone 無法直接執行。

## 已移除的工具

以下工具在 v4.0 瘦身時移除，git 歷史可取回；它們的實測結論已記錄在 `docs/BACKLOG.md`：

- `learn_scenarios.py`、`probes.jsonl`、`scenarios/`：依附 TypeScript 認知引擎的滾動學習工具。
  引擎移除後，`docs/agent/clarify_scenarios.json` 改為人工維護的靜態資料。
- `album_bench.py`：專輯抽取器評測台（專輯聚集已於 v3.10.0 決定不做）。
- `build_task3_samples.py`：Task 3 樣本的轉換與隨機重排工具。
