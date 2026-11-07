---
name: yt-playlist-manager
description: |
  管理與重排 YouTube / YouTube Music 播放清單。
  當使用者想要對播放清單排序、分組（例如「同歌手放一起」）、篩選或重新排列時使用。
  流程為：fetch 抓取 → 本地計算最小變更集（0 API 配額）→ 聊天室預覽並取得同意 → 寫回。
---

# YouTube Playlist Agent-Skill

你的角色是「大腦與協調者」，API 讀寫與演算法細節已封裝在 `scripts/yt_tool.py` 與 `src/`（TypeScript 認知排序引擎）中。

**完整流程請讀 `docs/agent/AGENT_SOP.md`**（五個 Phase、所有指令參數、回傳欄位與錯誤碼對照）。開始任何操作前先讀它。

## 不可違反的五條規則

0. **不可自行補上使用者沒說的排序維度**：使用者說「同歌手放一起」時，他**只**要求了分組，沒有要求群內順序。擅自加上觀看數排序會對已經聚在一起的群組產生額外移動（每筆 50 units），使用者會視之為「無意義搬動」。需求缺哪個維度就查 `docs/agent/clarify_scenarios.json` 發問，不要猜。詳見 AGENT_SOP.md Phase 0。
1. **絕不盲目寫入**：呼叫 `update` 前，必須在聊天室畫出變更預覽表並取得使用者明確同意。
2. **變更集順序相依**：`playlistItems.update` 是「移除後插入」，每次呼叫都會讓其餘影片重新編號。變更檔中的 `new_position` 是在模擬盤面上算出的中繼位置，**必須依 `execution_order` 逐筆執行；不可手動編輯、重新排序、拆分或跳過任何一筆**。要調整就重新執行 `optimize` / `diff`。
3. **預覽表用 `final_position`**：`new_position` 是送 API 的中繼位置，直接顯示給使用者會造成誤解。
4. **`STALE_SNAPSHOT` 代表遠端已被改動**：重新 `fetch --refresh` 後重算，絕不用 `--skip-verify` 硬闖。

## 指令速查

```bash
# 0. 憑證（預設 ~/.gemini/skills/yt-playlist-manager/credentials，可用 $YT_SKILL_HOME 覆寫）
python -m scripts.yt_tool setup_credentials <path_to_client_secret.json>

# 1. 抓取（--refresh 可略過 30 分鐘快取）
python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json

# 1b. 找重複（0 API units，只在使用者抱怨重複時才跑）。只認 video_id 完全相同，只列出不刪除。
python -m scripts.yt_tool duplicates data/current.json

# 2A. 分組（0 API units，推薦）。只做分組，群內維持原順序 —— 這是預設，移動最少。
python -m scripts.yt_tool optimize data/current.json \
    --target-out data/new.json --out data/changes.json \
    --group-order first_appearance --aliases data/artist_aliases.json
#    ↑ 僅在使用者「明確指定」群內順序時，才加上 --within-group-sort viewCount:desc
#      （見規則 0）。加上它會增加移動筆數，預覽時必須分開列出這筆成本。
#    ↑ --aliases 會把同一位藝人的不同拼法併成一組（跨語言別名、頻道贅字、歌名被誤判成藝人）。
#      回傳的 grouping_benefit 為 "low" 時，先把 grouping_warnings 轉述給使用者，
#      問他還要不要花這筆配額，不要直接往下做。

# 2B. 自訂順序：自己寫腳本產生 data/new.json（必須是 current 的重排），再算差異
python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json

# 2C. 自然語言規劃（TypeScript 認知引擎）
npm run build && npm run plan -- -i data/current.json -o data/new.json --intent "..."

# 3. 預覽並取得同意（見 AGENT_SOP.md Phase 3）

# 4. 寫回（會先花 1 unit 驗證遠端一致性；中斷後重跑同指令即續傳）
python -m scripts.yt_tool update <playlist_id> data/changes.json
```
