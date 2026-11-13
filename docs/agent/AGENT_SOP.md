# 播放清單 AI Agent SOP（YouTube／Spotify）

本文件定義任何 AI Agent（如 Anthropic Claude Code、Codex、GitHub Copilot Workspace、Google Antigravity 等）在此工作區整理 **YouTube 或 Spotify** 播放清單時的標準作業程序。

兩個平台用同一個 `yt_tool`、走同一套五個 Phase。平台由網址自動判斷，之後每一步的回傳值都帶有 `platform` 欄位；兩個平台只在憑證、成本單位與寫回細節上不同，本文件在各 Phase 內並列說明。當使用者要求你整理播放清單時，你**必須**嚴格遵守以下流程。

| Phase | 做什麼 | 主要指令 |
|:---|:---|:---|
| 0 需求與平台 | 盤點需求（缺什麼就問）、判斷平台、確認憑證、抓取清單 | `fetch`、`setup_credentials`／`setup_spotify` |
| 1 標註與計算 | 你標註每首歌的藝人，工具在本機算出最少搬動（0 API 呼叫） | `label-request`、`optimize`／`diff` |
| 2 檢查頁 | 使用者在瀏覽器看、拖拉、按保存 | `review start`、`review wait` |
| 3 一句確認 | 在聊天室用一句話報告成本，等使用者明確同意 | — |
| 4 寫回 | 驗證遠端、依序寫回、中斷可續傳 | `update` |

---

## 核心設計原則

1. **檢查頁＋一句確認（Human-in-the-Loop）**：在呼叫 `update` 寫入指令前，必須先讓使用者在瀏覽器檢查頁看過、（需要的話）拖拉調整並按「保存」，再在聊天室用**一句話**報告成本，取得明確同意。使用者明確表示不想在聊天室看預覽表。
2. **無 GUI 依賴**：本工具沒有任何 GUI 彈窗。憑證路徑、Client ID 等設定一律由 Agent 在聊天室與使用者以文字問答完成，並透過 CLI 指令寫入。會開的瀏覽器頁面只有平台的授權頁與 Phase 2 的檢查頁。
3. **安全第一**：憑證預設存放於 `~/.gemini/skills/yt-playlist-manager/credentials/`（可用環境變數 `YT_SKILL_HOME` 指定其他位置）：YouTube 是 `client_secret.json` 與授權 token，Spotify 是 Client ID 與授權 token。token 等同登入權限，不可貼到聊天室，也不可提交進版控。
4. **成本最小化**：所有重排都走 LIS（最長遞增子序列）錨點演算法，只搬最少的項目；所有計算都在本機完成（0 API 呼叫）。YouTube 一次只能搬一首（50 units）；Spotify 一次可以搬一整段連續的歌，呼叫數是 `S − LIS(段)`。
5. **變更集是順序相依的**：YouTube 的 `playlistItems.update` 是「移除後插入」，Spotify 的 `range_start`／`insert_before` 以搬移前的索引計算；兩者每一次呼叫都會讓其餘項目重新編號。變更檔中的位置是在模擬盤面上即時算出的，**必須依 `execution_order` 逐筆執行**。
   > 你**絕對不可以**手動編輯、重新排序、拆分或跳過變更檔中的任何一筆。需要調整就重新執行 `optimize` / `diff`。

---

## 執行流程 (Execution Flow)

### Phase 0：需求與平台

1. **需求維度盤點（必做，先於一切計算）**

   把使用者的**原話**拆成兩個維度，逐一標記「有講」或「沒講」：

   | 維度 | 例句 | 沒講的話 |
   |---|---|---|
   | **分群依據** | 「同歌手放一起」→ artist；「同專輯放一起」→ album（只限 Spotify） | 查表發問 |
   | **群內順序** | 「每組裡面熱門的排前面」→ view_count:desc | **查表發問，不可自行假設** |

   > ⚠️ **這是最容易出錯的一步。** 「把同歌手的影片整齊排序」只指定了分群，**沒有**
   > 指定群內順序。此時若自行補上觀看數排序，會對「本來就已經連在一起」的群組產生
   > 純粹為了群內重排的移動 —— YouTube 每筆 50 units、Spotify 多出好幾次呼叫，而使用者根本沒要求。
   > 真實測試中這件事已經發生過：清單開頭 7 首同一位歌手原本就相鄰，卻因為擅自加上的群內排序
   > 多花了 5 筆移動，使用者的評語是「無意義搬動」。

   任何一格是「沒講」，就**先查 `docs/agent/clarify_scenarios.json`**：依斷層種類
   （`missing_sort_field` / `missing_group_field`）找 `when` 條件吻合的變體，沒有吻合
   的就用該斷層的預設變體（`when` 是空字串的那一筆），照它的 `question` 與 `options`
   發問。查無此情境時才自己擬問法。

   模板裡的選項描述寫了各自的**成本代價**，那是使用者做決定所需要的資訊，不要省略。
   `missing_sort_field` 的第一個選項就是「維持原順序 —— 只做分組，不動群內既有順序。
   移動次數最少、配額最省」，多數「我只是想找同一個歌手時不用滑來滑去」的需求選這個
   就夠了。

   **這道閘門對兩條路徑（A／B）一律適用。** 特別注意：不要先把自己補的維度寫進
   `--within-group-sort` 參數或路徑 B 的排序腳本 —— 那樣斷層在你組參數時就被填掉，
   之後沒有任何一步會再提醒你。**判斷依據永遠是使用者的原話，不是你組出來的參數。**

2. **確認播放清單與平台**：若使用者未提供 ID 或網址，主動追問。你不需要自己用 Regex 解析，直接把整串 URL 或 ID 傳給 `yt_tool`，它有內建解析器，也會自動判斷平台：

   | 輸入 | 平台 |
   |:---|:---|
   | `open.spotify.com/playlist/…`、`spotify:playlist:…` | **Spotify** |
   | 其他網址或 ID（`youtube.com/playlist?list=…`、`music.youtube.com/…`、`PL…`） | **YouTube**（含 YouTube Music） |

   兩個平台都只能重排**使用者自己擁有或協作**的清單。

3. **抓取清單**（若 `data` 目錄不存在，請先建立）：

   ```
   python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json
   ```

   第一次使用某個平台時，這一步會先告訴你缺少什麼設定（第 4、5 點）。

4. **YouTube 憑證缺失（`CREDENTIALS_MISSING`）**：
   - 代表尚未設定 OAuth 憑證。Agent **必須**在聊天視窗向使用者詢問憑證路徑：
     > 「您尚未設定 Google OAuth 憑證。請提供您下載的 `client_secret.json` 憑證檔案的絕對路徑（例如：`C:\Users\Name\Downloads\client_secret.json`）。」
   - 取得使用者輸入的路徑（如 `<user_path>`）後，執行設定指令：
     `python -m scripts.yt_tool setup_credentials "<user_path>"`
   - 根據設定指令的回傳值進行應對：
     - `status: "success"`：設定成功，重新執行原本中斷的 `fetch` 指令。
     - `code: "FILE_NOT_FOUND"`：告知使用者該路徑找不到檔案，請其檢查後重新提供。
     - `code: "INVALID_JSON"`：告知使用者該 JSON 不是合法的 Google OAuth 桌面應用程式憑證，並引導其重新下載及提供。
     - `code: "COPY_FAILED"`：可能為權限問題，建議以管理員權限重新執行。
   - 第一次讀取時，底層庫會開啟系統瀏覽器的 Google 授權頁，請提示使用者完成授權。

5. **Spotify 設定缺失（`SPOTIFY_NOT_CONFIGURED`）**：
   - 使用者需要自己在 Spotify 開發者後台建一個 App 拿 Client ID（App 擁有者必須是 Premium）。
     **照 `docs/SPOTIFY_SETUP.md` 在聊天室一步一步帶他做**，不要只丟連結；開發者條款一律由使用者自己勾選同意，你不可代為操作。
   - 取得 Client ID 後執行 `python -m scripts.yt_tool setup_spotify <client_id>`。
     回 `INVALID_CLIENT_ID` 時，多半是貼成了 Client Secret 或少了字元，請使用者回 App 的 Settings 重新複製。
   - 第一次讀取清單時會輸出 `waiting_for_user` 並開啟瀏覽器授權頁，請使用者按「同意」。授權約 6 個月後失效，屆時會自動再開一次。
   - 想確認設定是否正確，可執行唯讀的 `python -m scripts.yt_tool spotify-check <playlist_url>`；
     `playlist.can_reorder` 為 `false` 代表這份清單不是他擁有或協作的，Spotify 不允許重排。

6. **解讀 `fetch` 的回傳值**：

   ```json
   {"status": "success", "platform": "youtube", "item_count": 200, "hidden_count": 3,
    "metadata_missing_count": 2,
    "source": "cache", "fetched_at": "2026-08-21T04:00:00+00:00",
    "head_fingerprint": "9f2c...", "file": "data/current.json"}
   ```

   - `source`：`api` 代表剛從 API 讀取；`cache` 代表來自本地快取（TTL 30 分鐘）。
     **若 `source` 是 `cache`，而使用者剛剛可能在 YouTube／Spotify 上動過這份清單，請改用 `--refresh` 重新抓取**，否則排序會以過期的順序為基準。
   - `hidden_count > 0`（只有 YouTube）：清單中含有私人／已刪除影片。它們仍佔用位置但無法移動，工具會把它們釘在原位；請在 Phase 3 主動告知使用者這會讓某些群組被切斷。Spotify 的本機檔案與無法播放的曲目一樣可以搬動，所以 Spotify 一律是 0。
   - `metadata_missing_count > 0`：有項目在清單中，但拿不到它的資料（YouTube 常見於地區限制；Spotify 多半是已下架）。這類項目仍會正常參與重排（不影響位置正確性），但沒有標題可用於藝人辨識，分組時多半會落入 `unknown`。請在 Phase 3 一併告知使用者。
   - Spotify 另外回傳 `snapshot_id`（清單版本）與 `api_calls`。

#### 選用：找出重複收錄的項目（0 API 呼叫）

使用者抱怨「同一首歌重複加進清單」時才跑，不是每次都要做：

```
python -m scripts.yt_tool duplicates data/current.json
```

```json
{"status": "success", "total_items": 192, "distinct_videos": 188,
 "duplicate_groups": 4, "redundant_items": 4,
 "groups": [{"video_id": "_lxS-x7DACQ", "title": "Unity", "occurrences": 2,
             "keep": {"playlist_item_id": "UEx0...", "position": 5},
             "removable": [{"playlist_item_id": "UEx0...", "position": 96}]}]}
```

- **只比對 `video_id` 完全相同的項目**（Spotify 是曲目 ID），所以零誤判。它**不會**把
  `aLIEz` 與 `aLIEz (Remastered)`、原版與 Live／Acoustic 版當成重複——
  那些是使用者刻意收藏的不同版本，之前用標題相似度判斷時正是在這裡誤判。
  也因此，使用者說「有重複」但這裡回報 0 筆時，請如實說明「沒有完全相同的重複項目，
  你看到的可能是同一首歌的不同版本」，不要自行放寬判準去湊出結果。
- `keep` 是清單中最前面的那一份，`removable` 是其餘的。
- **這個指令只列出，不會刪除任何東西。** 要移除得由使用者自己在 YouTube／Spotify 介面操作——
  刪除不可逆，目前沒有接上任何刪除指令。

### Phase 1：標註與計算

根據使用者的需求類型，選擇以下其中一種路徑。兩條路徑都會產出同樣格式的變更檔，也都套用 LIS 錨點最佳化，全程 **0 API 呼叫**。

#### 路徑 A：分組聚集排序（推薦）

適用於「把相同歌手／團體的歌放在一起」「同專輯放一起」等分組任務，可同時指定群內排序。這首歌屬於誰由你判斷（第 1 步，花 token，`label-request` 會先估算，標過的歌會快取），分組與最少移動由 `optimize` 計算：

1. **藝人標註：由你判斷每首歌屬於誰**（分組依據是 artist 時才做；`--group-by album` 跳過）

   ```
   python -m scripts.yt_tool label-request data/current.json
   ```

   回傳 `pending`（要你標的首數）、`est_tokens`（粗估的 token 數）、`cached`（之前標過、這次不用再標）、
   `by_formula`（超過預算、交給公式的首數）與 `instructions`（標註規則）。
   - `pending` 是 0：跳到第 2 步，不用傳 `--labels`。
   - 否則讀 `data/label_request.jsonl`，照 `instructions` 寫出 `data/labels.json`。兩個平台要你做的事不同：

     | | YouTube | Spotify |
     |:---|:---|:---|
     | 每行內容 | 一首歌：`pos`、`title`、`channel`，有平台資料時多一個 `p` | 一個藝人名稱（每首歌本來就有藝人） |
     | 你要寫的 | 每首的主要藝人＋別名 | 只找出同一位藝人的不同寫法（例如 `SawanoHiroyuki[nZk]` 與 `澤野弘之`）：`labels` 留空、只填 `aliases` |

     YouTube 的 `labels.json` 範例：
     ```json
     {"labels": {"0": {"artist": "米津玄師", "conf": "high"}, "7": {"artist": "unknown", "conf": "medium"}},
      "aliases": {"米津玄師": ["Kenshi Yonezu"]}}
     ```
     每個 `pos` 都要有答案；判斷不了就填 `unknown`（棄權，交給公式），不要亂猜。
     同一位藝人整份清單只用一種寫法，其他寫法放進 `aliases`。
   - 預設最多送 400 首（`--max-items` 可改）。超過時只送最沒把握的 400 首，其餘由公式決定，結果一樣會在檢查頁給使用者看。
   - 標註是內部步驟，**不要把標註表貼給使用者看**；使用者在檢查頁看分組結果就好。
   - 你的答案會存進標籤快取（`$YT_SKILL_HOME/label_cache.json`），同一首歌下次不用再標。
     使用者在檢查頁改過組的歌也記在這裡，而且你的答案蓋不掉使用者的更正。

2. **執行最佳化計算**：

   > ⚠️ **`--within-group-sort` 只有在 Phase 0 盤點出「使用者明確指定了群內順序」時才可加上。**
   > 它不是預設值，也不是「順便做比較好」的加值。省略它＝群內維持原順序，移動筆數最少、
   > 成本最低。以下範例刻意**不含**這個參數。

   ```
   python -m scripts.yt_tool optimize data/current.json \
       --target-out data/new.json \
       --out data/changes_optimized.json \
       --group-order first_appearance \
       --labels data/labels.json
   ```
   - `--labels`：第 1 步寫的 `data/labels.json`。工具會對照同一個資料夾的 `label_request.jsonl` 檢查：
     格式不對或 `pos` 不在清單裡回 `LABELS_INVALID`，請求裡有歌沒答到回 `LABELS_INCOMPLETE`（附 `missing`）。
     照錯誤補好 `labels.json` 再跑一次。
   - 分組公式：平台資料（YouTube 的 Art Track 描述與 `- Topic` 頻道、Spotify 的第一位藝人）與你的標註同權重；
     兩者不一致時採平台資料，並把那首標成「待確認」讓使用者在檢查頁看。沒被標註的歌改用標題與頻道名推測。
   - `--aliases <檔案>`（可選）：額外的 `{正式名稱: [其他寫法]}` 別名表，和你在 `labels.json` 給的別名合併使用。
   - `--group-order`：`first_appearance`（預設）、`alphabetical`、`count_desc`
     > 重排既有清單時請優先使用 `first_appearance`：字典序會把所有群組重新洗牌，需要移動的項目數（也就是成本）會高出好幾倍。
   - `--within-group-sort`：`<欄位>:<asc|desc>`，欄位可用 `viewCount`、`publishedAt`、`duration`、`title`、`channelTitle`、`addedAt`、`trackNumber`。省略則群內維持原順序。
     （Spotify 沒有觀看數；依專輯分組時，使用者若說「照專輯曲序」才用 `trackNumber:asc`。）
   - `--group-by`：`artist`（預設）或 `album`。**`album` 只限 Spotify**——Spotify 直接提供專輯 ID，
     不需要猜也不用標註；YouTube 沒有結構化專輯，會回 `UNSUPPORTED_GROUP_BY`。分組依據一樣要來自使用者原話（Phase 0）。

3. **解讀回傳值**（YouTube 範例）：
   ```json
   {
     "status": "success",
     "platform": "youtube",
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
     "unknown_ratio": 0.015,
     "effective_grouping_ratio": 0.72,
     "orphan_group_count": 18,
     "grouping_benefit": "ok",
     "grouping_warnings": [],
     "uncertain_count": 4,
     "agent_labels": 180,
     "user_labels": 2,
     "aliases": 5,
     "fingerprint": "07580a1cecc69364"
   }
   ```
   - **成本欄位依平台而不同**：

     | | YouTube | Spotify |
     |:---|:---|:---|
     | 成本 | `estimated_quota`（units，`need_to_move` × 50）；確認是否在單日限額（10,000 units）內 | `estimated_calls`（API 呼叫次數），沒有配額 |
     | 搬動數 | `need_to_move` | `items_moved`（實際被搬動的歌曲數）；`item_level_moves` 是一首一首搬要幾次 |

     Spotify 一次呼叫可以搬一整段連續的歌，所以呼叫數通常遠少於歌曲數。
   - `anchors`：不需移動的項目數量（LIS 錨點）
   - `pinned_count`：無法移動的私人／已刪除影片數量（只有 YouTube）
   - `grouping_benefit`：**分群效益判定，先看這個**。為 `"low"` 時代表這份清單重排完
     使用者大概感受不到差別，`grouping_warnings` 會逐條說明原因並帶好數字。
     這時**不要直接往下做**：把警告轉述給使用者，說明預估成本，問他還要不要繼續，
     或改用其他維度（路徑 B）。這是警告不是拒絕——使用者說要做就照做。
     判定發生在 `optimize`，本來就是 0 API 呼叫，所以一定在花成本之前。
   - `unknown_ratio` / `effective_grouping_ratio` / `orphan_group_count`：
     上面那個判定的三個依據。有效聚集率是「和別人聚在一起的項目 ÷ 可移動項目」，
     **`unknown` 群不算聚集**（那是垃圾桶不是群組）。門檻是 unknown ≥ 20% 或
     有效聚集率 < 30%，在 11 份真實樣本上乾淨分開、無邊界案例。
   - `unresolved_count`：完全辨識不出藝人的項目數。**這個數字是 0 不代表分群一定正確**，
     所以才要看 `grouping_benefit`：一份 40 組裝 44 首的日文清單 `unresolved_count` 正好是 0。
   - `uncertain_count`：公式沒把握的歌（平台資料和你的標註不一致，或只靠頻道名推測）。
     檢查頁會標「？待確認」；不是 0 時，開檢查頁那句話順便說「有 N 首我不太確定，標了待確認」。
   - `agent_labels` / `user_labels` / `aliases`：這次用了幾筆你的標註、幾筆使用者在檢查頁的更正、幾組別名。
   - `metadata_missing_count`：`unresolved_count` 當中，有多少是因為根本沒有資料可辨識，
     而不是辨識失敗。這些項目沒有標題或頻道名，`label_request.jsonl` 裡你也只會看到空白，填 `unknown` 即可。
     若這個數字偏高，如實告知使用者「這幾首缺少資料（地區限制或已下架），只能歸入 unknown」。

4. 直接跳至 **Phase 2**，建議順序是 `data/new.json`。

#### 路徑 B：自訂排序邏輯

適用於路徑 A 涵蓋不到的需求（依頻道或其他欄位分群、多重條件、關鍵字篩選後重排等）。需求由你直接從使用者原話解讀，不需要任何中介引擎；缺維度時照 Phase 0 查表發問：

1. 讀取 `data/current.json` 了解清單目前的狀態與資料結構（它是一個包含 `EnrichedPlaylistItem` 的 JSON 陣列，含 `title`、`channel_title`、`published_at`、`duration_seconds`、`is_available` 等欄位；YouTube 另有 `view_count`，Spotify 另有 `artists`、`album`、`track_number`）。
2. **撰寫並執行一段 Python 腳本**：
   - 讀取 `data/current.json`。
   - 套用排序／篩選邏輯，把**完整的**新順序寫入 `data/new.json`。
   - `data/new.json` 必須是 `data/current.json` 的重排（同一組項目，不可增刪）。工具會拒絕不成立的目標順序（`TARGET_MISMATCH`）。
   - 不需要特別處理 `is_available: false` 的項目，工具會自動把它們釘回原位置。
3. 執行差異計算：
   `python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json`

### Phase 2：檢查頁

1. 若 `changes_count` / `need_to_move` / `estimated_calls` 為 0，告知使用者不需變更並結束，不用開檢查頁。
2. 若 `grouping_benefit` 為 `low`，先照 Phase 1 的說明轉述警告、問使用者還要不要做。
3. **開檢查頁**（取代聊天室的預覽表）：

   ```
   python -m scripts.yt_tool review start data/current.json data/new.json
   ```

   - 第二個參數是建議順序：路徑 A 是 `--target-out` 的檔案（每一首已帶 `group`，頁面會依群組上色）；
     路徑 B 是你寫的 `data/new.json`，可以在每一筆加上 `"group": "..."`，沒有就顯示為未分組。
   - 回傳 `run_dir`、`url`、`browser_opened`。在聊天室**只說一句**，例如：
     「我開了一個檢查頁，可以拖拉調整順序，好了按『保存』。」並附上 `url`。
   - 頁面是由上到下的清單：一首歌一列（封面、歌名、藝人、時長），每組一張卡片，卡片頂端是寫著組名的標題列。
     使用者會用到的操作：
     - 拖動歌曲**只改順序**；歌放進別組中間時保留自己的組，標成「⚠ 屬於「某組」」。
     - 要改組必須明確操作：把歌拖到那組的標題列上，或點歌曲後在資訊卡的「改到這一組」選單選。
     - 拖動標題列＝搬整組（只會落在組與組之間）；標題列的「收合」把整組收成一行。
     - 「上一步」（Ctrl+Z）與「重置」（回到你的建議版）；鍵盤也能操作（Tab 選歌、Space 拿起、↑↓ 移動）。
     - 資訊卡顯示藝人、專輯、曲序、時長、加入日期、觀看數、封面與「在 YouTube／Spotify 開啟」連結。
   - 每次檢查都有自己的 `run_dir`（`runs/<平台>_<清單>_<時間>/`），整理前的快照、使用者的修改、操作紀錄、
     變更檔與寫回結果都留在裡面，不會被下一次覆蓋。
   - `browser_opened: false`：請使用者手動打開 `url`。使用者**無法使用瀏覽器**時才退回文字：
     在聊天室列出前 10 筆會搬動的歌（標題、`old_position` → `final_position`）與總數，再到 Phase 3。

4. **等使用者保存**：

   ```
   python -m scripts.yt_tool review wait <run_dir>
   ```

   預設最多等 540 秒（避開各家 Agent 主機的指令時限），依 `status` 應對：

   | status | 應對 |
   |:---|:---|
   | `saved` | 進入 Phase 3。`nothing_to_do: true` 時告知不需寫回並結束 |
   | `waiting` | 使用者還在看，再執行一次 `review wait`，不要催促 |
   | `cancelled` | 使用者按了取消：結束這次流程，不寫回 |
   | `expired` | 檢查頁閒置一小時已自動關閉：問使用者要不要重開 |

   - 使用者在聊天室說「不要了」時，執行 `review cancel <run_dir>` 關掉檢查頁。
   - `saved` 時的變更檔是 `<run_dir>/changes.json`，**已依使用者保存的順序重算**；之後一律寫回這個檔，
     不是 Phase 1 產生的 `data/changes*.json`。
   - `relabels > 0` 代表使用者明確把某些歌改到別的群組（拖到標題列上或用資訊卡選單，代表辨識錯了）。
     簡短告知「已記下你修正的 N 首，下次整理會直接採用」：`review wait` 已把它們存進標籤快取（`<run_dir>/user_labels.json` 是這次的紀錄）。
   - `edits` 是使用者改了幾處，**以狀態計算**：相對你的建議版搬了幾首（N − LIS）加上改了組的首數；
     搬一首又搬回原位是 0。`proposed_moves` / `proposed_calls` 是你原本建議的成本，可以一併提，
     例如「你調整後要搬 12 首（原建議 10 首）」。

### Phase 3：一句確認

1. **把 `summary_line` 原樣轉述**（一句話），等使用者回覆。它已經依平台寫好成本：YouTube 是搬幾首、約多少 units，Spotify 是搬幾首、幾次呼叫。
   Phase 0 提到的私人影片（`hidden_count`）或缺資料的項目（`metadata_missing_count`）不是 0 時，在同一句後面簡短補上。

2. **有用 `--within-group-sort` 時，確認句要把成本拆成兩桶**

   群內排序的成本是**可以單獨算出來**的，而且不花 API 呼叫：把同一份 `current.json`
   再跑一次 `optimize`，但**不加** `--within-group-sort`，輸出到別的檔名：

   ```
   python -m scripts.yt_tool optimize data/current.json \
       --target-out data/new_grouponly.json \
       --out data/changes_grouponly.json \
       --group-order first_appearance
   ```
   （標註已經存在標籤快取，這次不用再傳 `--labels`。）

   兩次的 `need_to_move`（Spotify 看 `estimated_calls`）相減，就是群內排序的**額外**代價。確認句要明確說出第二桶可以單獨取消，例如：

   > 「要搬 N₂ 首（約 N₂ × 50 units），其中 N₂ − N₁ 首只是為了把每組內部依觀看數排序。
   > 要省下這部分的話，我可以改用只做分組的版本。」

   使用者選擇只做分組時，改用 `data/new_grouponly.json` 回到 Phase 2 重新開檢查頁。

3. **配額警告（只適用 YouTube）**：若 `estimated_quota` > 2500，確認句加上：
   「這次會用掉今天額度的 X%（每日上限 10,000 units）。」

4. **暫停並等待使用者明確回覆**「OK」或「同意」，不可自動執行 Phase 4。

5. **（建議，只適用 YouTube）送出前先做離線演練**：變更筆數多、或這份清單是第一次重排時，先執行

   ```
   python tests/e2e/dryrun.py <run_dir>/snapshot.json <run_dir>/changes.json --target <run_dir>/edited_order.json
   ```

   它會用同一份快照跑**真正的** `update` 指令對上離線替身，驗證整份變更集會不會如預期
   落地（離開碼 0 才代表安全），全程 0 配額、不動到任何真實影片。Spotify 不需要：
   寫回前後都會比對整份清單，任何不符都會被擋下或回報。

### Phase 4：寫回

1. 收到確認後，寫回 `review wait` 產生的變更檔（同一個指令，依網址自動切換平台）：
   `python -m scripts.yt_tool update <playlist_url> <run_dir>/changes.json`
   結果也會存成 `<run_dir>/update_result.json`。
2. 寫回前都會先確認遠端仍與變更檔所依據的快照一致：

   | | YouTube | Spotify |
   |:---|:---|:---|
   | 寫回前驗證 | 花 **1 unit** 讀取清單頭部 | 重新讀取整份清單逐首比對（不只看 `snapshot_id`：實測它會延遲更新） |
   | 寫回方式 | 逐筆 `playlistItems.update`，進度寫入 `scripts/logs/progress_{playlist_id}.json` | 逐筆區段搬移，每次帶上一次回應的 `snapshot_id`；429 依 `Retry-After` 自動等待 |
   | 寫回後驗證 | — | 重新讀取整份清單，必須等於目標（`verified: true`） |

3. 依回傳結果應對。兩個平台共通的：

   | 回傳 | 意義 | 應對 |
   |:---|:---|:---|
   | `status: "success"` | 全部完成 | 簡潔回報：YouTube 報成功筆數與 `quota_used`；Spotify 報 `total` 與 `api_calls`（`verified: true` 代表已重新讀取確認） |
   | `code: "STALE_SNAPSHOT"` | 遠端清單已被改動，**尚未寫入任何一筆** | 重新執行 `fetch --refresh` → Phase 1 → Phase 2 → Phase 3，**不要**加 `--skip-verify` 硬闖 |
   | `code: "LEGACY_CHANGE_SET"` | 舊版變更檔 | 重新執行 `optimize` / `diff` 產生新檔 |

   中途停止（`status: "partial"`）時清單只是部分重排，項目沒有增減；兩個平台都是再執行同一個指令就能續傳，細節如下。

   YouTube 特有的：

   | 回傳 | 意義 | 應對 |
   |:---|:---|:---|
   | `status: "partial"` + `interrupted: true` | 被中斷 | 進度已保存，再次執行相同指令即可從斷點續傳 |
   | `code: "QUOTA_EXCEEDED"` | 今日配額用盡 | 告知已完成 `successful` 筆，明日再執行相同指令續傳 |
   | `code: "ITEM_NOT_FOUND"` / 其他錯誤 | 某一筆移動失敗 | 後續位置是以該筆完成為前提計算的，工具已停止。請重新 `fetch --refresh` 後重新計算 |

   Spotify 特有的：

   | 回傳 | 意義 | 應對 |
   |:---|:---|:---|
   | `status: "partial"`（`RATE_LIMITED`、`FORBIDDEN`、網路錯誤等） | 中途停止 | 再執行同一指令會從遠端**實際狀態**續傳；若使用者想回到原順序，`fetch --refresh` 後 `diff <新快照> <原本的 current.json>`，照一般流程確認後寫回 |
   | `code: "POST_VERIFY_MISMATCH"` | 所有搬移都送出了，但重新讀取的順序與目標不符（多半是有人同時在 Spotify 上改動） | `fetch --refresh` 後重新計算，告知使用者 |
   | `code: "FORBIDDEN"` | 這份清單不是使用者擁有或協作的 | 告知 Spotify 只允許重排自己的清單 |
   | `code: "AUTH_*"` | 授權失敗、逾時或被拒絕 | 請使用者重試並在瀏覽器按「同意」；仍失敗就回 Phase 0 檢查 Client ID 與 Redirect URI |

4. 寫回成功後本地快取會自動失效，下次 `fetch` 會取得最新順序。

---

## 成本與限制

| | YouTube | Spotify |
|:---|:---|:---|
| 計價方式 | 每日配額 **10,000 units**；每次 `playlistItems.update` **50 units**，只能搬一首 | 沒有每日配額，只有短時間的速率限制（工具會照 `Retry-After` 自動等待）；以 API 呼叫次數計 |
| 一次搬多少 | 一首 | 一整段連續的歌（呼叫數 = `S − LIS(段)` ≤ `N − LIS`） |
| 驗證成本 | 寫回前 **1 unit** 讀取清單頭部（可避免整批寫錯，非常划算） | 寫回前後各讀一次整份清單 |
| 大量作業 | 超過 180 次 update（9,000 units）建議拆成兩天執行 | 無需拆分 |
| 不能搬的項目 | 私人／已刪除影片釘在原位 | 無（本機檔案與無法播放的曲目一樣可以搬） |

- LIS 錨點演算法找出不需移動的項目，通常可節省 **25–70%** 的搬動。
- 所有計算（分組、排序、最佳化）皆在本地完成，**0 API 呼叫**。標註藝人花的是你的 token，`label-request` 會先估算，標過的歌會快取。
