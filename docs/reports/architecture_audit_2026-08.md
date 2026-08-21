# 架構與演算法稽核報告

**日期**：2026-08-21
**範圍**：`scripts/`（Python API 子系統）、`src/`（TypeScript 認知排序引擎）、Agent SOP 與工程設定
**方法**：逐檔靜態閱讀 + 對核心演算法做暴力窮舉實測（n=2..7 全排列，共 5912 組）
**結論**：發現 3 個 P0 級正確性缺陷、3 個 P1 級架構缺陷、2 個 P2 級工程缺口，皆已修復並補上回歸測試。

---

## 0. 摘要

最嚴重的發現是：**重排寫回演算法本身是錯的**。

把 `scripts/optimizer.py` 產生的變更集拿去實際套用（模擬 `playlistItems.update` 的真實語意），在 n=2..7 的全排列中，**4556/5912（77.1%）會得到錯誤的最終順序**。這不是效能問題——每一次錯誤的移動仍然照收 50 units 配額，而且結果是使用者的播放清單被打亂。

同一類「位置算錯」的問題還有兩個獨立來源：私人／已刪除影片在抓取階段被丟棄（索引空間被壓縮），以及斷點續傳的進度檔沒有繫結變更集（舊進度會讓新任務被靜默跳過並回報成功）。

三者的共同根因是：**測試只驗證輸出的結構性質，從未把變更集重播一次比對最終順序**。原本的 `test_position_drift_simulation` 甚至在註解裡寫下「本地模擬無法重現 YouTube 的伺服器行為，正確性只能靠實際 API 驗證」——這個前提不成立（見 §2.1），而它正是讓錯誤存活下來的原因。

| 嚴重度 | 問題 | 狀態 |
|:---|:---|:---|
| P0-1 | 重排演算法產生錯誤順序（77.1% 失敗率） | ✅ 已修復 |
| P0-2 | 私人／已刪除影片造成整段位置偏移 | ✅ 已修復 |
| P0-3 | 斷點續傳的 progress 檔沒有繫結變更集 | ✅ 已修復 |
| P1-4 | 快取可能讓寫回建立在過期順序上 | ✅ 已修復 |
| P1-5 | 「雙引擎」沒有橋接，且 `diff` 路徑放棄了 LIS | ✅ 已修復 |
| P1-6 | Evaluator 的自我檢驗迴圈結構上不可能觸發 | ✅ 已修復 |
| P2-7 | Extractor 的關鍵字比對被意圖文字污染 | ✅ 已修復 |
| P2-8 | 工程與驗證缺口（npm test、CI、進入點、錯誤處理） | ✅ 已修復 |

---

## 1. 架構概觀（稽核當下）

```
Agent (SKILL.md / AGENT_SOP.md)
  └─ Phase 0 憑證 → Phase 1 fetch → Phase 2 計算 → Phase 3 預覽 → Phase 4 update

scripts/                                  src/
  yt_tool.py      CLI 進入點                index.ts          cognitiveSort()
  youtube_api.py  API v3 封裝 + OAuth       core/cognitive/   Extractor / Planner / Evaluator
  optimizer.py    藝人辨識 + LIS + 變更集   core/engine/      ComparatorBuilder / SortingEngine
  executor.py     排序 / 篩選 / diff        adapters/         TrackList / ECommerce
  cache_manager.py 30 分鐘 TTL JSON 快取
  schemas.py      Pydantic v2 資料模型
```

架構分層本身是清楚的：Agent 只負責協調與確認，計算與 API 各自封裝，資料模型集中在 `schemas.py`。三層藝人辨識（標題正則 → 頻道名 → 模糊比對）設計合理，交叉驗證加信心分數的作法也恰當。

問題出在**兩處介面假設**與**一個結構性的驗證盲點**：

1. 位置運算全程使用「列表索引」，但 API 的位置是「當下清單中的實際位置」——這兩者在有隱藏影片、或在寫回過程中會分歧。
2. `src/` 與 `scripts/` 之間沒有任何呼叫關係，Agent SOP 也從未提及 `src/`，所謂「雙引擎」實際上只有一具引擎在運轉。
3. 測試涵蓋了辨識、LIS 長度、輸出結構，唯獨沒有涵蓋「把變更集套用後是否等於目標順序」這個唯一真正重要的性質。

---

## 2. 演算法邏輯稽核

### 2.1 `playlistItems.update` 的真實語意

API 的 `position` 參數語意是：**把該項目從目前位置移除，再插入到 `position`**，其餘項目重新編號。這與本地的 `list.remove()` + `list.insert()` 完全等價，因此本地模擬**可以**精確重現伺服器行為——原測試「無法在本地驗證」的前提不成立。

由此推得兩個必要條件：

- **條件 A**：送出的 position 必須是「執行當下」的位置，不能是目標陣列的靜態索引（第一筆之後就會失效）。
- **條件 B**：不動的項目（錨點）在目標順序中，其原始位置必須是遞增的——否則不存在可行的移動計畫。

### 2.2 原實作的兩個缺陷

`build_optimized_changes()`（`optimizer.py:483-529`）：

```python
old_pos = old_positions.get(item.playlist_item_id, new_pos)
if old_pos != new_pos:                       # 缺陷 1
    changes.append(PositionChange(..., new_position=new_pos))
changes.sort(key=lambda c: c.new_position, reverse=True)   # 缺陷 2
```

- **缺陷 1：移動集不完整。** 套用 LIS 錨點後，必須移動的集合**恰好是 LIS 的補集**，與「最終索引是否等於原索引」無關。
  反例：`[A,B,C] → [C,B,A]`，錨點是 A，B 的 `old_pos == new_pos == 1` 因此被跳過；但只移動 C 會得到 `[C,A,B]`，B 必須被移動。
- **缺陷 2：尾端優先 + 靜態位置。** 依 `new_position` 降冪套用，並把目標索引直接當成 position。所謂「Tail-First 漂移防護」在實測中完全無法防護漂移。
  反例：`[A,B,C,D,E] → [C,A,D,B,E]`，錨點 {A,B,E}，需移動 {C→0, D→2}。尾端優先先移 D 到 2 得 `[A,B,D,C,E]`，再移 C 到 0 得 `[C,A,B,D,E]` ≠ 目標。

### 2.3 窮舉實測

把 `optimizer.py` 的邏輯逐行移植後，對 n=2..7 的全部 5912 組排列各自產生變更集、依實作指定的順序重播，比對最終順序：

| 套用策略 | 錯誤率 | update 呼叫總數 |
|:---|---:|---:|
| **原實作**（LIS + 跳過 old==new + 尾端優先 + 靜態位置） | **4556/5912 (77.1%)** | 18,629 |
| LIS + 不跳過 + 頭端優先（仍用靜態位置） | 2383/5912 (40.3%) | 20,184 |
| `diff` 路徑（完整 diff + 頭端優先，靜態位置） | 1777/5912 (30.1%) | 34,406 |
| 貪婪頭端優先（每步比對模擬盤面，非最小） | 0/5912 (0%) | 25,148 |
| **修復後**（LIS + 頭端優先 + 模擬即時位置） | **0/5912 (0%)** | **20,184** |

兩個關鍵結論：

1. 只修「跳過」或只修「順序」都不夠（仍有 30–40% 錯誤），因為靜態位置本身就不成立。
2. 修復後的移動次數在全部 5912 組中**都等於理論下界 `N − LIS`**。也就是說，「LIS 可省 25–70% 配額」這個宣稱在修復之後才真正成立——原實作省下來的呼叫，是靠漏掉必要的移動換來的。

### 2.4 修復後的演算法

```
anchors = 受約束的 LIS（釘選項目強制入選，並依其位置切段設定上下界）
live    = 目前順序的副本
for slot, item in enumerate(target):          # 頭端優先
    if item in anchors: continue
    live.remove(item)
    position = 0 if slot == 0 else live.index(target[slot-1]) + 1   # 模擬盤面即時計算
    live.insert(position, item)
    emit(PositionChange(new_position=position, final_position=slot, execution_order=n))
```

正確性論證：錨點是遞增子序列，因此任一被移動項目的前一個目標項目，必定是「錨點」或「已放置完成的項目」，把它插到該項目正後方即可保持正確的相對順序；歸納到最後一格即得到完整目標順序。移動次數 = 非錨點數 = `N − LIS`，即下界。

實作位置：`scripts/optimizer.py` 的 `build_move_plan()` / `compute_lis_anchors()` / `plan_reorder()`。

---

## 3. 問題清單

### P0-1　重排演算法產生錯誤順序

- **證據**：§2.2、§2.3。
- **根因**：`tests/test_optimizer.py` 的 `test_changes_sorted_tail_first` 只斷言「`new_position` 是降冪排列」，`test_position_drift_simulation` 只檢查結構性質並在註解中主動排除了本地驗證的可能性。24 項測試沒有任何一項驗證端到端重排正確性。
- **修復**：以 `build_move_plan()` 取代 `build_optimized_changes()`；`PositionChange` 新增 `api_position` / `final_position` / `execution_order`；變更檔升為 v2 版本化物件，v1（舊格式）一律拒絕執行。
- **驗證**：`tests/test_reorder_property.py` 窮舉 5912 組重播 + 最小性、隨機 n=50/200、局部洗牌成本上界。

### P0-2　私人／已刪除影片造成整段位置偏移

- **證據**：`youtube_api.py:_parse_playlist_item` 對 `privacyStatus in ("private", "privacyStatusUnspecified")` 回傳 `None`（丟棄），但 `executor.py` / `optimizer.py` 的位置全部來自 `enumerate()`。API 回傳的真實位置雖然存進 `PlaylistItemData.position`，卻**全專案從未被讀取**（`grep` 確認只有 `schemas.py` 寫入）。
- **後果**：清單中只要有一支隱藏影片排在第 k 位，第 k 位之後每一次寫回都至少偏移一格。
- **修復**：改為保留為 `is_available=False` 的項目並釘在原索引（`pinned_item_ids` / `weave_pinned`）。錨點集合改用**受約束的分段 LIS**：釘選項目強制入選，並以其位置為界限計算各段 LIS，確保錨點仍是遞增子序列。
  > 直接把「無約束 LIS ∪ 釘選項目」聯集起來是錯的：例如 `current=[A,P,B]`（P 釘選）、`target=[B,P,A]`，聯集後的錨點順序為 (P:1, A:0) 並非遞增，計畫不可行。這個反例已寫成 `test_pinned_anchor_set_stays_increasing`。
- **驗證**：`test_pinned_items_never_move`（50 組隨機釘選情境）。

### P0-3　斷點續傳的 progress 檔沒有繫結變更集

- **證據**：`yt_tool.py` 的 `progress_{playlist_id}.json` 只以 playlist_id 命名，內容是無序的 `playlist_item_id` 集合；`remaining = [c for c in changes if c.playlist_item_id not in completed_ids]`，且被跳過的筆數仍計入 `successful` 與 `quota_used`。檔案只在「全部成功」時刪除。
- **後果**：前一次中斷留下的檔案，會讓下一次**完全不同**的變更集被靜默跳過並回報成功。
- **修復**：progress 檔改存 `{fingerprint, completed, updated_at}`；指紋不符即視為新任務重新開始。修復 P0-1 後變更集是順序相依的，因此進度也改記索引而非集合。
- **驗證**：`test_stale_progress_file_is_ignored`、`test_abort_then_resume`。

### P1-4　快取可能讓寫回建立在過期順序上

- **證據**：`cmd_fetch` 快取命中時回傳的 JSON 沒有任何來源標記，也沒有略過快取的選項；快取只在「全部成功」時失效。
- **後果**：部分失敗後、或使用者在 30 分鐘內於 YouTube 端手動動過清單，下一輪計算都會以過期基準進行——而位置型演算法用錯基準必然產生錯誤且昂貴的寫回。
- **修復**：
  - `fetch --refresh`，並在回傳中加上 `source` / `fetched_at` / `head_fingerprint` / `hidden_count`。
  - 變更檔內嵌來源快照（完整 id 順序）。
  - `update` 寫回前花 **1 unit** 讀取清單頭部比對；續傳時比對的是「快照 + 已完成移動」的預期狀態。不符則以 `STALE_SNAPSHOT` 中止，**尚未寫入任何一筆**。
  - 任何一次成功寫入後即失效快取。
- **驗證**：`test_stale_playlist_is_refused`（斷言 `client.calls == 0`）。

### P1-5　「雙引擎」沒有橋接，且 `diff` 路徑放棄了 LIS

- **證據**：
  - `src/` 在 `SKILL.md` 與 `AGENT_SOP.md` 中一次都沒被提到，也沒有任何 CLI 能把 `current.json` 轉成 `new.json`。
  - `ComparatorBuilder.buildGroupComparator` 只支援群組鍵字典序——而字典序正是最大化移動次數、最浪費配額的排法。
  - `optimize` 無法做「按歌手歸類、同歌手按觀看次數由高到低」（**README 首頁的示範需求**），Agent 只能退回路徑 B；而 `cmd_diff` 呼叫 `compute_diff` 時從不傳 `anchor_ids`，即使該函式本身支援。路徑 B 因此付全額配額（實測 34,406 vs 20,184 次呼叫，多出 70%）。
- **修復**：
  - 新增 `optimizer.plan_reorder()` 作為單一入口，`optimize` 與 `diff` 都走它。
  - `optimize --within-group-sort viewCount:desc`，群內排序直接重用 `executor._sort_key_factory`。
  - `GroupDimension.groupOrder`（預設 `first_appearance`）。
  - 新增 `src/cli.ts`（`npm run plan`）：`current.json` → 認知引擎 → `new.json` → `yt_tool diff`。
- **驗證**：對同一份清單，`optimize --within-group-sort viewCount:desc` 與 `npm run plan --intent "…觀看次數由高到低"` 產出**完全相同的變更集指紋** `07580a1cecc69364`。

### P1-6　Evaluator 的自我檢驗迴圈結構上不可能觸發

- **證據**：`detectContinuityGaps` 只檢查 `plan.groupDimensions[0]`，而 `ComparatorBuilder` 合成的比較器第一順位就是該欄位，同鍵項目必然相鄰。因此 `isContinuous` 恆為 true、`iterationsUsed` 恆為 0、`Planner.strengthenPlan` 是死碼，而 `tests/test_cognitive_engine.ts` 的「修復迴圈測試」斷言的是恆真命題。README 把它當賣點宣傳，屬於假保證。
- **修復**：
  - 掃描**全部**分群維度（真正會被打散的是次要維度，例如以 album 為主鍵時 artist 被拆開）。
  - 偵測器改用與 `ComparatorBuilder` 完全相同的正規化（含 `aliasMap`），避免兩套規則產生假斷層。
  - 新增群內排序單調性與空值位置檢查；多重排序鍵依字典序逐層細分子區段，不會對第二順位鍵誤報。
  - 修復迴圈保留「斷層最少」的版本，避免在互斥的分群目標之間來回震盪。
- **驗證**：`testRepairLoopActuallyRuns` 斷言 `iterationsUsed > 0` 且最終收斂；`testOrderingViolationDetected` 斷言違規可被偵測且正常結果不誤報。

### P2-7　Extractor 的關鍵字比對被意圖文字污染

- **證據**：`Extractor.ts` 原本寫成
  ```ts
  highOrderKeywords.some((kw) => lowerField.includes(kw) || (intentText.length > 0 && intentText.includes(kw)))
  ```
  第二個條件與 `field` 無關：只要意圖文字出現任一關鍵字（例如 "artist"），樣本物件的**每一個欄位**都會變成分群維度。此外關鍵字表只有英文，方向偵測卻只認中文；`isDesc` 是全域單一值，無法表達「A 降冪、B 升冪」；`cognitiveSort` 只用 `items[0]` 推導欄位宇宙。
- **修復**：意圖文字改為只能「挑選」實際符合詞彙表的欄位；新增中英雙語詞彙表；方向改為逐欄位解析（在關鍵字後方的視窗內尋找方向標記）；欄位宇宙改由前 20 筆聯集推導。
- **驗證**：`testIntentDoesNotPolluteFields`、`testPerFieldSortDirection`。

### P2-8　工程與驗證缺口

| 缺口 | 修復 |
|:---|:---|
| `npm test` import `../dist` 但不含 build，且 `dist/` 已 gitignore → 全新 clone 必失敗 | `pretest` 先 build，測試改 import 原始碼 |
| `tsconfig.include` 只有 `src/**/*`，typecheck 從不檢查測試 | 新增 `tsconfig.test.json`，`typecheck` 涵蓋兩者 |
| 無 CI | `.github/workflows/ci.yml`：型別檢查 + 四組測試 |
| `ensure_credentials()` 忽略 `--credentials`；該旗標只能放在子指令前 | 兩處都支援（子解析器用 `SUPPRESS` 避免覆寫父層值） |
| update 迴圈對 HttpError 不分類，403 quotaExceeded 後仍打完剩餘每一筆 | `classify_http_error()`：quota/401 立即中止、429/5xx 退避重試（2/4/8/16s）；任一筆失敗即停止（後續位置以該筆完成為前提） |
| 憑證路徑寫死 `~/.gemini/`；只有 `.gemini/skills/` 進入點 | `$YT_SKILL_HOME` 可覆寫；新增 `.claude/skills/` 與 `AGENTS.md` |
| `optimize` / `diff` 宣稱 0 API units 卻需要整套 Google 用戶端函式庫 | 改為延遲載入，純本地指令不再依賴它 |

---

## 4. 修復後的驗證結果

```
python tests/test_optimizer.py          24/24 passed
python tests/test_reorder_property.py   12/12 passed   （含 5912 組窮舉重播與最小性）
python tests/test_update_flow.py         7/7  passed
npm run typecheck                        0 errors      （src + tests）
npm test                                28/28 passed
```

`tests/test_reorder_property.py` 的關鍵斷言：

- 每一組排列重播後 `== target`
- 每一組排列的移動次數 `== N − LIS`（理論下界）
- 釘選項目最終仍在原索引，且完全不產生 API 呼叫
- 任何中斷點續傳後仍落在目標順序
- 非重排的目標（增刪項目）以 `TARGET_MISMATCH` 拒絕

`tests/test_update_flow.py` 以模擬 API 語意的假用戶端覆蓋：正常寫回、過期快照、中途失敗與續傳、進度污染、舊版變更檔、退避重試、配額耗盡。

**未實跑的部分**：端到端（真實 OAuth + 真實播放清單）未執行，本次稽核環境沒有 Google 憑證。不過寫回路徑的語意已由假用戶端逐筆比對，且該假用戶端實作的正是 §2.1 論證過的 API 語意。

---

## 5. 剩餘風險與後續建議

1. **端到端實跑**：建議在一份測試用播放清單上跑一次完整流程（fetch → optimize → 預覽 → update → 重新 fetch 比對），確認 API 語意假設在真實環境成立。這是目前唯一沒有被自動化涵蓋的環節。
2. **`videos.list` 的缺漏**：若某支影片在 `playlistItems` 可見但 `videos.list` 沒回傳（區域限制等），該項目的 metadata 會是空的，藝人辨識會落到 `unknown`。目前不影響位置正確性，但會影響分組品質，可考慮另外標記。
3. **超大清單的效能**：`build_move_plan` 是 O(N²)（`list.index` / `list.remove` 皆為 C 層線性掃描）。實測 5000 首（YouTube 單一清單上限）完全洗牌的最壞情況為 **0.39 秒**，1000 首為 0.02 秒，目前無須最佳化；若未來要支援更大的資料量，可改用平衡樹或索引結構。
4. **`unknown` 群組**：所有辨識失敗的影片會被歸到同一個 `unknown` 群並被視為一個群組移動。當 `unresolved_count` 偏高時，這個群組的移動可能反而增加配額；SOP 已要求 Agent 提示擴充別名表。
5. **群組被釘選項目切斷**：私人影片釘在原位會讓某些群組不連續。這是 API 的硬限制（無法移動私人項目），SOP 已要求 Agent 在預覽階段主動說明。
