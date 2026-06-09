---
name: yt-playlist-manager
description: |
  YouTube Playlist 管理大腦。
  當使用者想要對 YouTube 或 YouTube Music 的播放清單進行排序、篩選、整理、重新排列時觸發。
  Agent 會負責前置檢查、呼叫資料抓取工具、透過 Python 腳本或內建工具計算排序，並於聊天室呈現變更預覽，經使用者同意後再呼叫 API 寫回。
---

# YouTube Playlist Agent-Skill SOP

做為 Antigravity Agent，當使用者要求你管理 YouTube 播放清單時，你**必須**嚴格遵守以下流程。你的角色是「大腦與協調者」，底層的 API 讀寫與計算細節已封裝在 `scripts/yt_tool.py` 中。

## Core Philosophy (核心理念)
1. **絕不盲目寫入**：在呼叫 `python -m scripts.yt_tool update` 之前，你必須先在聊天室畫出變更預覽表，並獲得使用者的明確同意。
2. **善用你的程式能力**：遇到複雜的排序/篩選需求時，你可以在工作區寫一個簡單的 `temp_sort.py` 來讀取 JSON、計算新順序、寫出 JSON，展現你處理邊角案例的強大能力。
3. **安全第一**：確保憑證放在 `~/.gemini/skills/yt-playlist-manager/credentials/`。

---

## 執行流程 (Execution Flow)

### Phase 0: 需求診斷與前置檢查

1. **確認 Playlist ID**：若使用者沒提供，請追問。你不需要自己用 Regex 解析，直接將整串 URL 或 ID 傳給 `yt_tool.py` 即可，它有內建解析器。
2. **憑證自動處理**：你**不需要**主動檢查憑證是否存在。`yt_tool.py` 內建了 `ensure_credentials()` 守衛函式，在任何指令執行前會自動偵測憑證。若憑證不存在，工具會**自動彈出系統原生的檔案選擇視窗**（只顯示 `.json` 檔案），讓使用者直接選取 OAuth 憑證。
   - 若工具回傳錯誤碼，你需要在聊天室引導使用者：
     - `USER_CANCELLED`：使用者關閉了選擇視窗。詢問是否要重試，若同意則提醒「請在即將彈出的視窗中選取您的 OAuth JSON 憑證」，然後再次呼叫相同指令。
     - `INVALID_JSON`：使用者選到的 JSON 不是 OAuth 憑證。提示他需要在 Google Cloud Console 建立「桌面應用程式」類型的 OAuth 2.0 Client ID 並下載 JSON，然後再次呼叫相同指令重新選取。
     - `COPY_FAILED`：複製失敗，可能是權限問題。建議以系統管理員身份執行。
   - 若工具回傳正常結果（`status: "success"`），代表憑證已自動設定完成，直接進入後續流程。

### Phase 1: 獲取資料 (Data Acquisition)

1. 執行指令：
   `python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json`
   *(若 `data` 資料夾不存在，請先 `mkdir data`)*
2. 該指令會回傳一段 JSON (例如 `{"status": "success", "item_count": 50, ...}`)。
3. 首次執行如果需要 OAuth 登入，`yt_tool.py` 可能會觸發瀏覽器視窗。請告知使用者注意彈出視窗並完成授權。

### Phase 2: 本地計算 (Local Computation)

1. 閱讀 `data/current.json` 了解清單目前的狀態與資料結構（它是一個包含 `EnrichedPlaylistItem` 的 JSON 陣列）。
2. 根據使用者的自然語言需求（例如「觀看次數高的放前面，但把超過十分鐘的放到最後面」），**撰寫並執行一段 Python 腳本**：
   - 腳本讀取 `data/current.json`。
   - 套用你的自訂邏輯排序/過濾陣列。
   - 寫出至 `data/new.json`。
3. *技巧*：`EnrichedPlaylistItem` 包含 `view_count`, `duration_seconds`, `title`, `channel_title`, `published_at` 等欄位。

### Phase 3: 差異計算與強制預覽 (Checkpoint)

1. 執行指令計算變更：
   `python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json`
2. 工具會回傳 JSON，包含 `changes_count` 與 `estimated_quota`。如果為 0，告訴使用者不需變更並結束。
3. **強制預覽 (Preview)**：讀取 `data/changes.json` 中的前 15-20 筆異動，在聊天室中**畫出 Markdown 表格**：
   - 欄位包含：`#`、`影片標題`、`舊位置`、`->`、`新位置`。
   - 在表格下方，明確列出「**預估配額消耗：XXXX units**」。
4. **配額警告**：如果 `estimated_quota` > 2500，請在表格下方加上這段醒目警告：
   `> [!WARNING]`
   `> 此次操作預估消耗配額超過 2,500 units。每日 API 上限為 10,000 units，請確認是否執行。`
5. **暫停並等待使用者回覆**。不要自行接續 Phase 4。

### Phase 4: 執行寫回 (Execution)

1. 使用者回覆「OK」或「同意」後，執行指令：
   `python -m scripts.yt_tool update <playlist_id> data/changes.json`
2. 工具會循序更新並回傳 JSON 結果（包含 `successful`, `failed`, `quota_used` 等）。
3. 根據結果，在聊天室給予使用者簡潔的回報。如果發生中斷 (`interrupted: true`)，請告訴使用者進度已存檔，隨時可以再次執行以續傳。
