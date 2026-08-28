# 滾動式學習（開發者工具）

**這裡的東西不是 skill 的一部分。** 一般使用 skill 時不會、也不該執行它們。

## 為什麼有這個東西

使用者說「把同一個歌手的歌放在一起」時，並沒有交代群內順序要怎麼排。Agent 只能
停下來反問。這種「需求不完整、只能發問」的斷層，以前每次都要重新發現一次，
沒有任何地方累積 —— 換一個 session、換一個 Agent，就得重來。

這個工具把那些斷層變成可累積的資產：刻意餵一批不完整的 Prompt 給**真正的**
認知引擎，把它卡住的地方記下來，交給人判定，再分流到兩個去處。

## 工具是開發者的，產物是 Agent 的

```
tests/dev/probes.jsonl          ← 不完整 Prompt 語料，滾動累積
        │
        ▼
tests/dev/learn_scenarios.py    ← 跑真正的 node dist/cli.js，不 mock
        │
        ▼
tests/dev/scenarios/pending/    ← 待審區，verdict 是 null
        │
   人工判定 verdict
        │
        ├── "ask" ───────────► docs/agent/clarify_scenarios.json
        │                       問題模板庫。Agent 在 Phase 2 查表發問。
        │
        └── "vocabulary" ────► src/core/cognitive/learned-vocabulary.ts
                                引擎詞彙。Extractor 合併後直接看懂，不必發問。
```

Agent 在正常流程中只會讀到 `clarify_scenarios.json` 這份**被動資料**，
永遠不會碰到 `tests/dev/` 底下的任何程式。

## 三道擋門

1. 程式放在 `tests/dev/`，不在任何 skill 載入路徑內。
2. 專用進入點 `python tests/dev/learn_scenarios.py`，沒有掛進 `npm test`
   或任何預設測試流程。
3. **沒有設 `YT_DEV_LEARN=1` 就完全不寫檔**，只把發現的斷層印出來。

`SKILL.md`（Claude 版與 Gemini 版）與 `AGENTS.md` 都不提這支程式。

## 用法

```bash
# 1. 看看目前有哪些探針會讓引擎卡住（唯讀，不寫任何檔案）
python tests/dev/learn_scenarios.py

# 2. 收錄進待審區
YT_DEV_LEARN=1 python tests/dev/learn_scenarios.py

# 3. 人工編輯 tests/dev/scenarios/pending/*.json 的 verdict 與 resolution

# 4. 升級
YT_DEV_LEARN=1 python tests/dev/learn_scenarios.py --promote

# 5. 詞彙異動需重新建置才會生效
npm run build

# 6. 回歸檢查（CI 會跑這個）
python tests/dev/learn_scenarios.py --check
```

全程本地計算：不碰 YouTube API、不需要憑證、0 配額。

## verdict 怎麼判

這是整個流程唯一需要人判斷的地方，工具刻意不自動化它。

| verdict | 什麼時候用 | 判準 |
|---|---|---|
| `ask` | 本質模糊 | 補再多詞彙也猜不出來。「把同歌手的歌放在一起」根本沒提到群內順序，任何猜測都是替使用者做決定。 |
| `vocabulary` | 只是沒收錄的講法 | 需求其實講得很完整。「依熱門程度由高到低」意思明確，引擎聽不懂純粹是詞彙表沒有「熱門程度」。 |

判錯的代價不對稱：把 `ask` 誤判成 `vocabulary`，等於讓引擎去猜使用者的意圖，
猜錯了會直接反映成播放清單被排成他不想要的樣子。**不確定就填 `ask`。**

### 填 `vocabulary`

```json
"verdict": "vocabulary",
"resolution": {"vocabulary": {"sort": {"view": ["熱門", "熱門程度"]}}}
```

正規詞（這裡的 `view`）請用 `Extractor.ts` 既有的鍵：分群用
`artist` / `album` / `channel` / `category` / `group` / `series` / `brand` /
`project` / `owner`，排序用 `track` / `number` / `index` / `position` / `order` /
`date` / `time` / `price` / `view` / `score` / `like`。方向說法則放
`descMarkers` / `ascMarkers`。

學來的詞彙**不享有任何特權**：它一樣只能用來「挑選實際存在的欄位」，
不會讓不相關的欄位入選 —— 這條防線寫在 `Extractor.ts` 檔頭，是舊 bug 的教訓。

### 填 `ask`

把 `resolution` 的 `question` 與 `options` 改寫成你真的會問使用者的話。
每個選項的 `description` 請寫出它對使用者的**實際意義與代價**（例如配額差異），
不要只寫欄位名 —— 使用者要靠它做決定。

同一種斷層底下如果有需要分開問的變體，另外填 `scenario_key` 與 `when`：

```json
"scenario_key": "missing_sort_field/which-date",
"when": "使用者講了新舊、前後這類時間方向，卻沒指明是發布日期還是加入清單的日期"
```

沒填 `scenario_key` 的會以斷層種類為鍵，同型探針自動併成同一個模板（只累積探針，
不覆寫已經寫好的問法）。

## `--check` 在檢查什麼

它重跑所有已升級情境的探針，驗證行為與當初記錄的一致：

- 判為 `vocabulary` 的 → 現在**不該**再有斷層。有的話代表學到的詞彙沒生效
  （通常是忘了 `npm run build`）。
- 判為 `ask` 的 → 現在**仍該**有斷層。沒有的話代表引擎開始自行猜測本來該問
  使用者的東西，可能是某次詞彙擴充擴過頭了。

第二條是這個工具最重要的防線：它擋住「為了讓引擎少問一點，結果讓它開始亂猜」
這種退化。

## 已知限制

引擎在使用者完全沒提到分群時，仍會退而選用所有候選分群欄位（實務上就是
`channel_title`），所以 `missing_group_field` 這種斷層在真實的播放清單 schema 下
幾乎不會出現，`no_criteria` 也一樣。情境記錄裡保留了完整的 `group_dimensions`，
審查時請自己看一眼引擎到底假設了什麼 —— 工具不會替你判斷那個假設合不合理。
