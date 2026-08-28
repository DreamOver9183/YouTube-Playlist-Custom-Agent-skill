# YouTube Playlist AI Agent SOP (標準作業程序)

本文件定義了任何 AI Agent（如 Anthropic Claude Code、Codex、GitHub Copilot Workspace、Google Antigravity 等）在此工作區管理/排序 YouTube 播放清單時的標準作業程序。

作為 AI Agent，當使用者要求你管理 YouTube 播放清單時，你**必須**嚴格遵守以下流程。

---

## 核心設計原則

1. **強制預覽與確認（Human-in-the-Loop）**：在呼叫 `update` 寫入指令前，必須在聊天室呈現 Markdown 格式的差異預覽表，並獲得使用者的明確同意。
2. **無 GUI 依賴**：本工具已完全移除 GUI 彈窗。所有憑證路徑引導皆由 Agent 在聊天室與使用者以文字問答完成，並透過 CLI 指令寫入。
3. **安全第一**：憑證預設存放於 `~/.gemini/skills/yt-playlist-manager/credentials/client_secret.json`（可用環境變數 `YT_SKILL_HOME` 指定其他位置）。
4. **配額最小化**：所有重排都走 LIS（最長遞增子序列）錨點演算法，只移動最少的影片。所有計算在本地完成（0 API units）。
5. **變更集是順序相依的**：`playlistItems.update` 的語意是「移除後插入」，每一次呼叫都會讓其餘影片重新編號。變更檔中的位置是在模擬盤面上即時算出的，**必須依 `execution_order` 逐筆執行**。
   > 你**絕對不可以**手動編輯、重新排序、拆分或跳過變更檔中的任何一筆。需要調整就重新執行 `optimize` / `diff`。

---

## 執行流程 (Execution Flow)

### Phase 0: 需求診斷與憑證檢查

1. **確認 Playlist ID**：若使用者未提供 ID 或網址，主動追問。你不需要自己用 Regex 解析，直接把整串 URL 或 ID 傳給 `yt_tool.py`，它有內建解析器。
2. **執行 Fetch 測試**：
   直接執行 `python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json`
3. **處理憑證缺失 (`CREDENTIALS_MISSING`)**：
   - 若指令回傳 `{"status": "error", "code": "CREDENTIALS_MISSING", ...}`，代表尚未設定 OAuth 憑證。
   - Agent **必須**在聊天視窗向使用者詢問憑證路徑：
     > 「您尚未設定 Google OAuth 憑證。請提供您下載的 `client_secret.json` 憑證檔案的絕對路徑（例如：`C:\Users\Name\Downloads\client_secret.json`）。」
   - 取得使用者輸入的路徑（如 `<user_path>`）後，執行設定指令：
     `python -m scripts.yt_tool setup_credentials "<user_path>"`
   - 根據設定指令的回傳值進行應對：
     - `status: "success"`：設定成功，重新執行原本中斷的 `fetch` 指令。
     - `code: "FILE_NOT_FOUND"`：告知使用者該路徑找不到檔案，請其檢查後重新提供。
     - `code: "INVALID_JSON"`：告知使用者該 JSON 不是合法的 Google OAuth 桌面應用程式憑證，並引導其重新下載及提供。
     - `code: "COPY_FAILED"`：可能為權限問題，建議以管理員權限重新執行。

### Phase 1: 獲取資料 (Data Acquisition)

1. 執行指令：
   `python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json`
   *(若 `data` 目錄不存在，請先建立)*
2. 回傳值範例：
   ```json
   {"status": "success", "item_count": 200, "hidden_count": 3,
    "metadata_missing_count": 2,
    "source": "cache", "fetched_at": "2026-08-21T04:00:00+00:00",
    "head_fingerprint": "9f2c...", "file": "data/current.json"}
   ```
   - `source`：`api` 代表剛從 API 讀取；`cache` 代表來自本地快取（TTL 30 分鐘）。
   - **若 `source` 是 `cache`，而使用者剛剛可能在 YouTube 上動過這份清單，請改用 `--refresh` 重新抓取**，否則排序會以過期的順序為基準。
   - `hidden_count > 0`：清單中含有私人／已刪除影片。它們仍佔用位置但無法移動，工具會把它們釘在原位；請在 Phase 3 主動告知使用者這會讓某些群組被切斷。
   - `metadata_missing_count > 0`：有影片可在清單中看到，但 `videos.list` 沒有回傳它的資料（常見於地區限制）。這類影片仍會正常參與重排（不影響位置正確性），但沒有標題／頻道名可用於藝人辨識，分組時多半會落入 `unknown`。請在 Phase 3 一併告知使用者。
3. 首次執行若需要 OAuth 登入，底層庫會觸發系統瀏覽器視窗。請提示使用者注意瀏覽器彈窗並完成授權。

### Phase 2: 本地計算 (Local Computation)

根據使用者的需求類型，選擇以下其中一種路徑。三條路徑都會產出同樣格式的變更檔，也都套用 LIS 錨點最佳化。

#### 路徑 A：分組聚集排序（推薦）

適用於「把相同歌手/團體的影片放在一起」等分組任務，可同時指定群內排序。使用內建的 `optimize` 指令，**零 Token 消耗、零 API 配額**：

1. **（可選）建立藝人別名對照表**：若播放清單包含同一藝人的不同名稱變體，可建立 `data/artist_aliases.json`：
   ```json
   {
     "BTS": ["Bangtan Boys", "방탄소년단"],
     "BLACKPINK": ["블랙핑크", "BP"]
   }
   ```

2. **執行最佳化計算**：
   ```
   python -m scripts.yt_tool optimize data/current.json \
       --target-out data/new.json \
       --out data/changes_optimized.json \
       --group-order first_appearance \
       --within-group-sort viewCount:desc \
       --aliases data/artist_aliases.json
   ```
   - `--group-order`：`first_appearance`（預設）、`alphabetical`、`count_desc`
     > 重排既有清單時請優先使用 `first_appearance`：字典序會把所有群組重新洗牌，需要移動的影片數（也就是配額）會高出好幾倍。
   - `--within-group-sort`：`<欄位>:<asc|desc>`，欄位可用 `viewCount`、`publishedAt`、`duration`、`title`、`channelTitle`、`addedAt`。省略則群內維持原順序。

3. **解讀回傳值**：
   ```json
   {
     "status": "success",
     "total_items": 200,
     "anchors": 75,
     "need_to_move": 125,
     "pinned_count": 3,
     "estimated_quota": 6250,
     "quota_saved_vs_naive": 5750,
     "groups_found": ["bts", "blackpink", "yoasobi", "unknown"],
     "group_details": {"bts": 30, "blackpink": 25},
     "unresolved_count": 3,
     "metadata_missing_count": 2,
     "fingerprint": "07580a1cecc69364"
   }
   ```
   - `anchors`：不需移動的影片數量（LIS 錨點）
   - `pinned_count`：無法移動的私人／已刪除影片數量
   - `unresolved_count`：完全辨識不出藝人的影片數。**這個數字是 0 不代表分群一定正確**，
     它只計算「兩層都失敗」的情況。請一併檢查 `groups_found`：若同一位藝人出現多個相近的
     key，或群組數接近影片數（幾乎全是單曲群），代表這份清單的命名形態不適合自動分群，
     應改用路徑 B/C 明確指定欄位。`artist_aliases.json` 只在辨識失敗時才會生效，
     無法用來合併「已辨識但分錯」的群組。
   - `metadata_missing_count`：`unresolved_count` 當中，有多少是因為根本沒有資料可辨識
     （`videos.list` 沒回傳，通常是地區限制），而不是辨識演算法失敗。這些影片無法靠
     `artist_aliases.json` 救回來——沒有標題或頻道名可以比對。若這個數字偏高，如實告知
     使用者「這幾支影片因地區限制缺少資料，只能歸入 unknown」，不要嘗試用別名表修正。
   - `estimated_quota`：確認是否在單日限額（10,000 units）內

4. 直接跳至 **Phase 3** 使用 `data/changes_optimized.json`。

#### 路徑 B：自訂排序邏輯

適用於路徑 A 涵蓋不到的需求（多重條件、關鍵字篩選後重排等）：

1. 讀取 `data/current.json` 了解清單目前的狀態與資料結構（它是一個包含 `EnrichedPlaylistItem` 的 JSON 陣列，含 `view_count`、`duration_seconds`、`title`、`channel_title`、`published_at`、`is_available` 等欄位）。
2. **撰寫並執行一段 Python 腳本**：
   - 讀取 `data/current.json`。
   - 套用排序/篩選邏輯，把**完整的**新順序寫入 `data/new.json`。
   - `data/new.json` 必須是 `data/current.json` 的重排（同一組項目，不可增刪）。工具會拒絕不成立的目標順序（`TARGET_MISMATCH`）。
   - 不需要特別處理 `is_available: false` 的項目，工具會自動把它們釘回原位置。
3. 執行差異計算：
   `python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json`

#### 路徑 C：認知排序引擎（自然語言直接規劃）

當需求是「同某某放一起、群內再依某欄位排序」這類分群 + 排序的組合，且不想自己寫腳本時：

```
npm run build          # 首次使用需先建置
npm run plan -- --input data/current.json --output data/new.json \
    --intent "把同一個頻道的影片放在一起，觀看次數由高到低"
# 或明確指定欄位：
npm run plan -- -i data/current.json -o data/new.json \
    --group-by channel_title --sort-by view_count:desc
```

回傳值中的 `warnings` **必須**轉達給使用者。引擎只會採用它真正辨識得出來的欄位，
若使用者說了「由新到舊」卻沒指名欄位，`sort_criteria` 會是空的並附上警告——
此時請追問使用者要依哪個欄位排序，或改用 `--sort-by` 明確指定後重跑，
不要把「沒有排序」的結果當成完成。

接著同路徑 B 的第 3 步執行 `diff`。

### Phase 3: 差異計算與強制預覽 (Checkpoint)

1. 變更檔案：路徑 A 為 `data/changes_optimized.json`；路徑 B / C 為 `data/changes.json`。
2. 若 `changes_count` / `need_to_move` 為 0，告知使用者不需變更並結束。
3. **強制預覽 (Preview)**：讀取變更檔中 `changes` 陣列的前 15-20 筆，在聊天室中繪製 **Markdown 表格**：

| # | 影片標題 | 原位置 | → | 最終位置 | 狀態 |
|---|---------|--------|---|---------|------|
| 1 | BTS - Dynamite | 45 | → | 0 | 🔄 移動 |
| 2 | BTS - Permission to Dance | 1 | → | 1 | ⚓ 錨點 |

   - 「原位置」用 `old_position`，「最終位置」用 `final_position`。
   - **不要**把 `new_position` 當成最終位置顯示給使用者——那是送給 API 的中繼位置，會讓使用者誤解。

   表格下方明確列出：
   - **預估配額消耗：XXXX units**
   - **錨點影片（不消耗配額）：XX 支**
   - **較 Naive 方法節省：XXXX units**
   - 若 `pinned_count > 0`：**「清單中有 N 支私人／已刪除影片無法移動，將留在原位置，可能會切斷某些群組。」**

4. **配額警告**：若 `estimated_quota` > 2500，加上以下醒目警告：
   > ⚠️ 此次操作預估消耗配額超過 2,500 units。每日 API 上限為 10,000 units，請確認是否執行。

5. **暫停並等待使用者明確回覆**「OK」或「同意」，不可自動執行 Phase 4。

6. **（建議）送出前先做離線演練**：變更筆數多、或這份清單是第一次重排時，先執行

   ```
   python tests/e2e/dryrun.py data/current.json data/changes_optimized.json --target data/new.json
   ```

   它會用同一份快照跑**真正的** `update` 指令對上離線替身，驗證整份變更集會不會如預期
   落地（離開碼 0 才代表安全），全程 0 配額、不動到任何真實影片。

### Phase 4: 執行寫回 (Execution)

1. 收到確認後，執行寫回指令：
   `python -m scripts.yt_tool update <playlist_id> data/changes_optimized.json`
   *（路徑 B / C 使用 `data/changes.json`）*
2. 工具會先花 **1 unit** 讀取清單頭部，確認遠端順序仍與變更檔所依據的快照一致，再逐筆寫回，並把進度寫入 `scripts/logs/progress_{playlist_id}.json`。
3. 依回傳結果應對：

| 回傳 | 意義 | 應對 |
|:---|:---|:---|
| `status: "success"` | 全部完成 | 簡潔回報成功筆數與 `quota_used` |
| `code: "STALE_SNAPSHOT"` | 遠端清單已被改動，尚未寫入任何一筆 | 重新執行 `fetch --refresh` → Phase 2 → Phase 3，**不要**加 `--skip-verify` 硬闖 |
| `status: "partial"` + `interrupted: true` | 被中斷 | 告知進度已保存，再次執行相同指令即可續傳 |
| `code: "QUOTA_EXCEEDED"` | 今日配額用盡 | 告知已完成 `successful` 筆，明日再執行相同指令續傳 |
| `code: "ITEM_NOT_FOUND"` / 其他錯誤 | 某一筆移動失敗 | 後續位置是以該筆完成為前提計算的，工具已停止。請重新 `fetch --refresh` 後重新計算 |
| `code: "LEGACY_CHANGE_SET"` | 舊版變更檔 | 重新執行 `optimize` / `diff` 產生新檔 |

4. 寫回成功後本地快取會自動失效，下次 `fetch` 會取得最新順序。

---

## 配額管理注意事項

- 每次 `playlistItems.update` 消耗 **50 units**，每日上限 **10,000 units**
- 每次 `update` 另外花 **1 unit** 做寫回前的一致性驗證（這 1 unit 可以避免掉整批寫錯的風險，非常划算）
- LIS 錨點演算法找出不需移動的影片，通常可節省 **25–70%** 的配額
- 超過 180 次 update（9,000 units）的操作，建議拆成兩天執行
- 所有計算（分組、排序、最佳化）皆在本地完成，消耗 **0 API units**
