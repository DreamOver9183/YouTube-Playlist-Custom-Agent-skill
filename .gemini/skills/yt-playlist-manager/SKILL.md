---
name: yt-playlist-manager
description: |
  YouTube Playlist 管理大腦。
  當使用者想要對 YouTube 或 YouTube Music 的播放清單進行排序、篩選、整理、重新排列時觸發。
  Agent 會負責前置檢查、呼叫資料抓取工具、透過 Python 腳本或內建工具計算排序，並於聊天室呈現變更預覽，經使用者同意後再呼叫 API 寫回。
---

# YouTube Playlist Agent-Skill SOP

做為 AI Agent，當使用者要求你管理 YouTube 播放清單時，你**必須**嚴格遵守以下流程。你的角色是「大腦與協調者」，底層的 API 讀寫與計算細節已封裝在 `scripts/yt_tool.py` 中。

> 完整版流程、回傳欄位與錯誤碼對照請見 **`docs/agent/AGENT_SOP.md`**。遇到本文件沒寫到的回傳值時，以該文件為準。

## Core Philosophy (核心理念)

1. **絕不盲目寫入**：在呼叫 `python -m scripts.yt_tool update` 之前，你必須先在聊天室畫出變更預覽表，並獲得使用者的明確同意。
2. **變更集順序相依**：`playlistItems.update` 是「移除後插入」，每次呼叫都會讓其餘影片重新編號。變更檔中的位置是在模擬盤面上算出來的，**必須依 `execution_order` 逐筆執行；不可手動編輯、重新排序、拆分或跳過任何一筆**。要調整就重新執行 `optimize` / `diff`。
3. **配額最小化**：所有重排都走 LIS 錨點演算法，可節省 25–70% 配額；全部計算在本地完成（0 API units）。
4. **安全第一**：憑證放在 `~/.gemini/skills/yt-playlist-manager/credentials/`（可用 `YT_SKILL_HOME` 覆寫）。

---

## 執行流程 (Execution Flow)

### Phase 0: 需求診斷與前置檢查

1. **確認 Playlist ID**：若使用者沒提供，請追問。直接將整串 URL 或 ID 傳給 `yt_tool.py` 即可，它有內建解析器。
2. **憑證檢查與設定**：
   - 直接執行 Phase 1 的 fetch 指令，`yt_tool.py` 會自動守衛並檢查憑證。
   - 若憑證不存在，工具會回傳 `{"status": "error", "code": "CREDENTIALS_MISSING", ...}`。
   - 此時你**必須**在聊天室主動向使用者詢問憑證的絕對路徑，再執行：
     `python -m scripts.yt_tool setup_credentials "<user_path>"`
   - 其他錯誤碼：`FILE_NOT_FOUND`（路徑錯誤）、`INVALID_JSON`（不是桌面應用程式類型的 OAuth 憑證）、`COPY_FAILED`（權限問題）。

### Phase 1: 獲取資料 (Data Acquisition)

1. 執行：`python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json`
   *(若 `data` 資料夾不存在，請先 `mkdir data`)*
2. 檢查回傳值：
   - `source: "cache"` 代表資料來自本地快取（TTL 30 分鐘）。若使用者可能剛動過清單，改用 `--refresh` 重抓。
   - `hidden_count > 0` 代表清單含私人／已刪除影片：它們佔位但無法移動，會被釘在原位。
3. 首次執行如果需要 OAuth 登入，會觸發系統瀏覽器視窗，請提示使用者完成授權。

### Phase 2: 本地計算 (Local Computation)

#### 路徑 A：分組聚集排序（推薦）

```
python -m scripts.yt_tool optimize data/current.json \
    --target-out data/new.json \
    --out data/changes_optimized.json \
    --group-order first_appearance \
    --within-group-sort viewCount:desc
```

- `--group-order`：`first_appearance`（預設，最省配額）、`alphabetical`、`count_desc`
- `--within-group-sort`：`<欄位>:<asc|desc>`，欄位可用 `viewCount`、`publishedAt`、`duration`、`title`、`channelTitle`、`addedAt`
- `--aliases data/artist_aliases.json`：藝人別名對照表，格式 `{"BTS": ["Bangtan Boys", "방탄소년단"]}`
- 解讀 `anchors`（不移動）、`need_to_move`、`pinned_count`、`estimated_quota`、`unresolved_count`
- 若 `unresolved_count > 0`，建議擴充 aliases 後重新執行

#### 路徑 B：自訂排序邏輯

1. 讀取 `data/current.json`，了解 `EnrichedPlaylistItem` 結構。
2. **撰寫並執行 Python 腳本**：讀取 → 排序/篩選 → 把**完整的**新順序寫出至 `data/new.json`（同一組項目，不可增刪，否則回傳 `TARGET_MISMATCH`）。
3. 執行 `python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json`

#### 路徑 C：認知排序引擎

```
npm run build
npm run plan -- --input data/current.json --output data/new.json \
    --intent "把同一個頻道的影片放在一起，觀看次數由高到低"
```
接著同路徑 B 第 3 步執行 `diff`。

### Phase 3: 差異計算與強制預覽 (Checkpoint)

1. 路徑 A 使用 `data/changes_optimized.json`；路徑 B / C 使用 `data/changes.json`。
2. 若變更數量為 0，告訴使用者不需變更並結束。
3. **強制預覽**：讀取 `changes` 陣列前 15-20 筆，畫出 **Markdown 表格**：
   - 欄位：`#`、`影片標題`、`原位置`（`old_position`）、`→`、`最終位置`（`final_position`）、`狀態`（🔄 移動 / ⚓ 錨點）
   - **不要**把 `new_position` 當成最終位置顯示，那是送給 API 的中繼位置。
   - 表格下方列出「**預估配額消耗**」、「**錨點數量**」、「**較 Naive 方法節省**」；若 `pinned_count > 0`，說明有幾支影片無法移動、可能切斷群組。
4. **配額警告**：如果 `estimated_quota` > 2500，加上醒目警告。
5. **暫停並等待使用者回覆**。不要自行接續 Phase 4。

### Phase 4: 執行寫回 (Execution)

1. 使用者回覆「OK」或「同意」後，執行：
   `python -m scripts.yt_tool update <playlist_id> data/changes_optimized.json`
2. 工具會先花 1 unit 驗證遠端清單仍與快照一致，再依序寫回，並記錄進度。
3. 依回傳結果應對：
   - `status: "success"`：回報成功筆數與 `quota_used`。
   - `code: "STALE_SNAPSHOT"`：遠端已被改動且尚未寫入任何一筆 → 重新 `fetch --refresh` 後重算，**不要**用 `--skip-verify` 硬闖。
   - `interrupted: true` 或 `code: "QUOTA_EXCEEDED"`：進度已存檔，再次執行相同指令即可續傳。
   - 其他錯誤碼：工具會在第一筆失敗處停止（後續位置以該筆完成為前提），請重新 `fetch --refresh` 後重算。
   - `code: "LEGACY_CHANGE_SET"`：舊版變更檔，重新執行 `optimize` / `diff`。
