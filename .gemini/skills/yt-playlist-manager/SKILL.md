---
name: yt-playlist-manager
description: |
  管理與重排 YouTube / YouTube Music 與 Spotify 播放清單。
  當使用者想要對播放清單排序、分組（例如「同歌手放一起」「同專輯放一起」）、篩選或重新排列時使用。
  流程為：fetch 抓取 → 本地計算最小變更集（0 API 配額）→ 瀏覽器檢查頁確認 → 聊天室一句同意 → 寫回。
---

# YouTube Playlist Agent-Skill

你的角色是「大腦與協調者」：需求解讀與自訂順序由你負責，API 讀寫與最小移動演算法已封裝在 `scripts/yt_tool.py`。

**完整流程請讀 `docs/agent/AGENT_SOP.md`**（五個 Phase、所有指令參數、回傳欄位與錯誤碼對照）。開始任何操作前先讀它。

## 不可違反的五條規則

0. **不可自行補上使用者沒說的排序維度**：使用者說「同歌手放一起」時，他**只**要求了分組，沒有要求群內順序。擅自加上觀看數排序會對已經聚在一起的群組產生額外移動（每筆 50 units），使用者會視之為「無意義搬動」。需求缺哪個維度就查 `docs/agent/clarify_scenarios.json` 發問，不要猜。詳見 AGENT_SOP.md Phase 0。
1. **寫回前一定要取得同意，而且用檢查頁、不用預覽表**：先用 `review start` 開檢查頁讓使用者看、拖拉、按保存；`review wait` 回 `saved` 後，把 `summary_line` 用一句話告訴使用者，等他明確回覆同意才 `update`。不要在聊天室貼預覽表（使用者明確表示不想看）；只有無法使用瀏覽器時，才在聊天室列出前 10 筆移動與總數。
2. **變更集順序相依**：`playlistItems.update` 是「移除後插入」，每次呼叫都會讓其餘影片重新編號。變更檔中的 `new_position` 是在模擬盤面上算出的中繼位置，**必須依 `execution_order` 逐筆執行；不可手動編輯、重新排序、拆分或跳過任何一筆**。要調整就重新執行 `optimize` / `diff`。
3. **不要給使用者看中繼位置**：`new_position`、`range_start`、`insert_before` 都是送 API 的中繼索引。檢查頁已經顯示每首歌的最終位置；需要在聊天室提到位置時一律用 `final_position`。
4. **`STALE_SNAPSHOT` 代表遠端已被改動**：重新 `fetch --refresh` 後重算，絕不用 `--skip-verify` 硬闖。

## 指令速查

```bash
# 0. 憑證（預設 ~/.gemini/skills/yt-playlist-manager/credentials，可用 $YT_SKILL_HOME 覆寫）
python -m scripts.yt_tool setup_credentials <path_to_client_secret.json>   # YouTube
python -m scripts.yt_tool setup_spotify <client_id>   # Spotify；沒有 Client ID 就照 docs/SPOTIFY_SETUP.md 帶使用者建 App
python -m scripts.yt_tool spotify-check [<spotify_playlist_url>]   # 唯讀檢查 Spotify 連線

# 1. 抓取（--refresh 可略過 30 分鐘快取）。平台由網址自動判斷：open.spotify.com / spotify: 為 Spotify，其餘為 YouTube
python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json

# 1b. 找重複（0 API units，只在使用者抱怨重複時才跑）。只認 video_id 完全相同，只列出不刪除。
python -m scripts.yt_tool duplicates data/current.json

# 2A. 分組（0 API units，推薦）。先由你標註藝人，再算最少移動。
python -m scripts.yt_tool label-request data/current.json
#    ↑ pending > 0 時讀 data/label_request.jsonl，照回傳的 instructions 寫 data/labels.json
#      （每首的主要藝人＋別名）。標過的歌存在快取，下次不用再標。
python -m scripts.yt_tool optimize data/current.json \
    --target-out data/new.json --out data/changes.json \
    --group-order first_appearance --labels data/labels.json
#    ↑ 只做分組，群內維持原順序 —— 這是預設，移動最少。僅在使用者「明確指定」群內順序時，
#      才加上 --within-group-sort viewCount:desc（見規則 0）；確認時必須分開說出這筆成本。
#    ↑ 回傳的 grouping_benefit 為 "low" 時，先把 grouping_warnings 轉述給使用者，
#      問他還要不要花這筆配額，不要直接往下做。
#    ↑ Spotify：可加 --group-by album（只限 Spotify，不用標註）。成本看 estimated_calls，沒有配額。

# 2B. 自訂順序：自己寫腳本產生 data/new.json（必須是 current 的重排），再算差異
python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json

# 3. 檢查頁（見 AGENT_SOP.md Phase 3）：開頁 → 等保存 → 把 summary_line 一句話轉述 → 等使用者同意
python -m scripts.yt_tool review start data/current.json data/new.json   # 回傳 run_dir 與 url
python -m scripts.yt_tool review wait <run_dir>     # saved / waiting（再跑一次）/ cancelled / expired
python -m scripts.yt_tool review cancel <run_dir>   # 使用者在聊天室說不要了

# 4. 寫回 review wait 產生的變更檔（YouTube 先花 1 unit 驗證遠端；中斷後重跑同指令即續傳）
python -m scripts.yt_tool update <playlist_url> <run_dir>/changes.json
```
