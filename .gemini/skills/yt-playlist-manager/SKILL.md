---
name: yt-playlist-manager
description: |
  YouTube Playlist 管理大腦。
  當使用者想要對 YouTube 或 YouTube Music 的播放清單進行排序、篩選、整理、重新排列時觸發。
  Agent 會負責前置檢查、呼叫資料抓取工具、透過 Python 腳本或內建工具計算排序，並於聊天室呈現變更預覽，經使用者同意後再呼叫 API 寫回。
---

# YouTube Playlist Agent-Skill SOP

做為 AI Agent，當使用者要求你管理 YouTube 播放清單時，你**必須**嚴格遵守以下流程。你的角色是「大腦與協調者」，底層的 API 讀寫與計算細節已封裝在 `scripts/yt_tool.py` 中。

## Core Philosophy (核心理念)
1. **絕不盲目寫入**：在呼叫 `python -m scripts.yt_tool update` 之前，你必須先在聊天室畫出變更預覽表，並獲得使用者的明確同意。
2. **配額最小化**：大量分組/排序操作優先使用 `optimize` 指令（LIS 錨點演算法），可節省 25–70% 配額。
3. **善用你的程式能力**：遇到複雜的排序/篩選需求時，你可以寫一個簡單的 Python 腳本來計算新順序。
4. **安全第一**：確保憑證放在 `~/.gemini/skills/yt-playlist-manager/credentials/`。

---

## 執行流程 (Execution Flow)

### Phase 0: 需求診斷與前置檢查

1. **確認 Playlist ID**：若使用者沒提供，請追問。你不需要自己用 Regex 解析，直接將整串 URL 或 ID 傳給 `yt_tool.py` 即可，它有內建解析器。
2. **憑證檢查與設定**：
   - 直接執行 Phase 1 的 fetch 指令，`yt_tool.py` 會自動守衛並檢查憑證。
   - 若憑證不存在，工具會回傳包含 `CREDENTIALS_MISSING` 錯誤碼的 JSON：
     `{"status": "error", "code": "CREDENTIALS_MISSING", ...}`
   - 此時，你**必須**在聊天室主動向使用者詢問憑證路徑：
     > "您尚未設定 Google OAuth 憑證。請提供您下載的 `client_secret.json` 憑證檔案的絕對路徑（例如：`C:\Users\Name\Downloads\client_secret.json`）。"
   - 收到使用者輸入的路徑（如 `<user_path>`）後，執行設定指令：
     `python -m scripts.yt_tool setup_credentials "<user_path>"`
   - 根據回傳結果進行應對：
     - 若回傳 `{"status": "success", ...}`：表示設定成功，重新執行 Phase 1。
     - 若回傳 `{"code": "FILE_NOT_FOUND"}`：告知使用者該路徑找不到檔案，請其檢查後重新提供。
     - 若回傳 `{"code": "INVALID_JSON"}`：說明該 JSON 不是合法的 Google OAuth 桌面應用程式憑證，引導重新提供。
     - 若回傳 `{"code": "COPY_FAILED"}`：說明複製檔案失敗，建議以系統管理員身份重新執行。

### Phase 1: 獲取資料 (Data Acquisition)

1. 執行指令：
   `python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json`
   *(若 `data` 資料夾不存在，請先 `mkdir data`)*
2. 該指令會回傳一段 JSON (例如 `{"status": "success", "item_count": 50, ...}`)。
3. 首次執行如果需要 OAuth 登入，`yt_tool.py` 會觸發系統瀏覽器視窗。請提示使用者注意瀏覽器彈窗並完成授權。

### Phase 2: 本地計算 (Local Computation)

根據使用者的需求類型，選擇以下路徑：

#### 路徑 A：分組聚集排序（推薦用於「相同歌手放一起」等分組任務）

使用 `optimize` 指令，**零 Token 消耗**：

1. （可選）建立 `data/artist_aliases.json` 藝人別名對照表：
   ```json
   {"BTS": ["Bangtan Boys", "방탄소년단"], "BLACKPINK": ["블랙핑크"]}
   ```
2. 執行最佳化計算：
   ```
   python -m scripts.yt_tool optimize data/current.json \
       --target-out data/new.json \
       --out data/changes_optimized.json \
       --group-order first_appearance
   ```
   `--group-order` 可選值：`first_appearance`（預設）、`alphabetical`、`count_desc`
3. 解讀回傳值中的 `anchors`（不移動）、`need_to_move`、`estimated_quota`、`unresolved_count`。
4. 若 `unresolved_count > 0`，建議擴充 aliases 後重新執行。

#### 路徑 B：自訂排序邏輯

適用於「觀看次數排序」、「按發布日期排列」等自訂排序需求：

1. 閱讀 `data/current.json`，了解 `EnrichedPlaylistItem` 結構（含 `view_count`, `duration_seconds`, `title`, `channel_title`, `published_at` 等欄位）。
2. **撰寫並執行 Python 腳本**：讀取 → 排序/篩選 → 寫出至 `data/new.json`。
3. 執行差異計算：
   `python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json`

### Phase 3: 差異計算與強制預覽 (Checkpoint)

1. 路徑 A 使用 `data/changes_optimized.json`；路徑 B 使用 `data/changes.json`。
2. 若變更數量為 0，告訴使用者不需變更並結束。
3. **強制預覽 (Preview)**：讀取前 15-20 筆異動，畫出 **Markdown 表格**：
   - 欄位：`#`、`影片標題`、`舊位置`、`→`、`新位置`、`狀態`（🔄 移動 / ⚓ 錨點）
   - 表格下方列出「**預估配額消耗**」、「**錨點數量**」、「**較 Naive 方法節省**」
4. **配額警告**：如果 `estimated_quota` > 2500，加上醒目警告。
5. **暫停並等待使用者回覆**。不要自行接續 Phase 4。

### Phase 4: 執行寫回 (Execution)

1. 使用者回覆「OK」或「同意」後，執行指令：
   `python -m scripts.yt_tool update <playlist_id> data/changes_optimized.json`
   *（路徑 B 使用 `data/changes.json`）*
2. 工具會循序更新並回傳 JSON 結果（包含 `successful`, `failed`, `quota_used` 等）。
3. 根據結果，在聊天室給予使用者簡潔的回報。如果發生中斷 (`interrupted: true`)，請告訴使用者進度已存檔，隨時可以再次執行以續傳。
