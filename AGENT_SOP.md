# YouTube Playlist AI Agent SOP (標準作業程序)

本文件定義了任何 AI Agent（如 Anthropic Claude Code、Codex、GitHub Copilot Workspace、Google Antigravity 等）在此工作區管理/排序 YouTube 播放清單時的標準作業程序。

作為 AI Agent，當使用者要求你管理 YouTube 播放清單時，你**必須**嚴格遵守以下流程。

---

## 核心設計原則

1. **強制預覽與確認（Human-in-the-Loop）**：在呼叫 `update` 寫入指令前，必須在聊天室呈現 Markdown 格式的差異預覽表，並獲得使用者的明確同意。
2. **無 GUI 依賴**：本工具已完全移除 GUI 彈窗。所有憑證路徑引導皆由 Agent 在聊天室與使用者以文字問答完成，並透過 CLI 指令寫入。
3. **安全第一**：憑證預設存放於 `~/.gemini/skills/yt-playlist-manager/credentials/client_secret.json`。

---

## 執行流程 (Execution Flow)

### Phase 0: 需求診斷與憑證檢查
1. **確認 Playlist ID**：若使用者未提供 ID 或網址，主動追問。
2. **執行 Fetch 測試**：
   直接執行 `python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json`
3. **處理憑證缺失 (`CREDENTIALS_MISSING`)**：
   - 若指令回傳 `{"status": "error", "code": "CREDENTIALS_MISSING", ...}`，代表尚未設定 OAuth 憑證。
   - Agent **必須**在聊天視窗向使用者詢問憑證路徑：
     > "您尚未設定 Google OAuth 憑證。請提供您下載的 `client_secret.json` 憑證檔案的絕對路徑（例如：`C:\Users\Name\Downloads\client_secret.json`）。"
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
2. 本指令會優先查詢本地快取（TTL 30分鐘）。若過期或未快取，會自動呼叫 API 抓取。
3. 首次執行若需要 OAuth 登入，底層庫會觸發系統瀏覽器視窗。請提示使用者注意瀏覽器彈窗並完成授權。

### Phase 2: 本地計算 (Local Computation)
1. 讀取 `data/current.json` 了解清單目前的狀態與資料結構（它是一個包含 `EnrichedPlaylistItem` 的 JSON 陣列）。
2. 根據使用者的自然語言需求（例如「觀看次數高的放前面，但把超過十分鐘的放到最後面」），利用你自身的程式碼生成能力，**撰寫並執行一段 Python 腳本**：
   - 讀取 `data/current.json`。
   - 套用排序/篩選邏輯，計算新位置（列表索引值即為新位置）。
   - 將計算後的新列表完整寫入 `data/new.json`。

### Phase 3: 差異計算與強制預覽 (Checkpoint)
1. 執行比對指令：
   `python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json`
2. 若回傳 `changes_count` 為 0，告知使用者不需變更並結束。
3. **強制預覽 (Preview)**：讀取 `data/changes.json` 中的前 15-20 筆異動，在聊天室中繪製 **Markdown 表格**：
   - 欄位：`#`、`影片標題`、`舊位置`、`->`、`新位置`。
   - 表格下方明確列出「**預估配額消耗：XXXX units**」。
4. **配額警告**：若 `estimated_quota` > 2500，在表格下方加上以下醒目警告：
   > [!WARNING]
   > 此次操作預估消耗配額超過 2,500 units。每日 API 上限為 10,000 units，請確認是否執行。
5. **暫停並等待使用者明確回覆**「OK」或「同意」，不可自動執行 Phase 4。

### Phase 4: 執行寫回 (Execution)
1. 收到確認後，執行寫回指令：
   `python -m scripts.yt_tool update <playlist_id> data/changes.json`
2. 本指令會逐筆更新位置，並將進度寫入 `scripts/logs/progress_{playlist_id}.json`。
3. 根據回傳 JSON 進行簡潔回報。若回傳 `interrupted: true`（被中斷），告知使用者進度已保存，再次呼叫相同指令即可進行**斷點續傳**。
