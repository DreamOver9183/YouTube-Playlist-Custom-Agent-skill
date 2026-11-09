# Playlist Manager — 用一句話整理你的 YouTube／Spotify 播放清單

清單越收越長，同一位歌手的歌散落各處，想聽的時候要一直往下滑？
這個工具讓你**用聊天的方式**請 AI 助手幫你整理播放清單：

- 「幫我把**同一個歌手**的歌放在一起」
- 「幫我把**同一張專輯**的歌放在一起」（Spotify）
- 「幫我找找看有沒有**重複加入**的歌」

整理時有三個保證：
- **你點頭才會動手**：AI 會先告訴你要搬哪些歌，你說 OK 才會真的改你的清單。
- **只搬必要的歌**：已經在對的位置的歌不會被動到。
- **改完會自己檢查**：寫回後重新讀一次清單，確認結果跟預定的一樣；中途斷線也能從斷掉的地方接著做。

工具本身免費，也不需要會寫程式（Spotify 依官方規定需要 Premium 帳號）。

---

## 你需要準備什麼

| 項目 | 說明 |
|---|---|
| 一個 AI 助手 | 能在你電腦上執行指令的 AI，例如 [Claude Code](https://claude.com/claude-code)（有桌面版）、Gemini CLI、Codex。它會替你下所有指令。 |
| Python 3.11 以上 | AI 助手需要用到。沒裝的話，請 AI 助手幫你檢查與安裝。 |
| **整理 YouTube 清單時** | 一個 Google 帳號，以及一份「OAuth 憑證檔」（一次性設定，約 10 分鐘，見下方〈如何開始〉第 2 步）。 |
| **整理 Spotify 清單時** | Spotify **Premium** 帳號，以及在 Spotify 開發者後台建立的一個免費 App（約 5 分鐘，見 [`docs/SPOTIFY_SETUP.md`](docs/SPOTIFY_SETUP.md)）。 |

> 只能整理**你自己的**播放清單。別人公開的清單沒辦法重排，這是 YouTube 與 Spotify 的規定。

---

## 如何開始

### 第 1 步：把這個工具交給你的 AI 助手

打開 AI 助手的聊天視窗，貼上這一句：

```
請幫我使用 https://github.com/DreamOver9183/YouTube-Playlist-Custom-Agent-skill 這個 skill
```

AI 會自己下載、安裝需要的套件，並讀懂這個工具的使用說明。

### 第 2 步：第一次使用時，完成帳號授權（只需要做一次）

直接跟 AI 說你想整理哪一份清單（貼上網址）。它發現還沒授權時，會在聊天室**一步一步帶你做**：

- **YouTube**：AI 會請你提供一個「OAuth 憑證檔」的位置。這個檔案要到 Google Cloud Console 申請，步驟如下：

  <details>
  <summary>YouTube 憑證怎麼申請（逐步說明，點開）</summary>

  > Google 偶爾會改版後台，按鈕名稱可能略有不同；照意思找最接近的即可，或把畫面截圖給 AI。

  1. 到 <https://console.cloud.google.com>，用你的 Google 帳號登入，建立一個新專案（名稱隨意）。
  2. 左側選單「API 和服務」→「程式庫」，搜尋 **YouTube Data API v3**，按「啟用」。
  3. 設定「OAuth 同意畫面」（新版介面叫 **Google Auth Platform**）：使用者類型選「外部」，
     填 App 名稱與你的 email；在「測試使用者」（新版在 **Audience**）加入**你自己的 Google 帳號**。
  4. 「憑證」（新版在 **Clients**）→ 建立 **OAuth 用戶端 ID** → 應用程式類型選 **電腦版應用程式** → 建立 → **下載 JSON**。
  5. 把下載的 JSON 檔案位置告訴 AI（例如 `C:\Users\你的名字\Downloads\client_secret_xxx.json`）。

  第一次讀取清單時會開啟瀏覽器請你登入 Google。若看到「Google 尚未驗證這個應用程式」，
  那是因為這個 App 是你自己建的、還在測試狀態，按「繼續」即可。

  </details>

- **Spotify**：AI 會帶你照 [`docs/SPOTIFY_SETUP.md`](docs/SPOTIFY_SETUP.md) 建立一個 Spotify App，
  拿到一串 Client ID 貼給它。第一次讀取清單時瀏覽器會打開 Spotify 授權頁，按「同意」即可。

憑證與登入資料只存在你自己的電腦上，不會上傳到任何地方。

### 第 3 步：用一句話說你想怎麼排

例如：

```
幫我把 https://www.youtube.com/playlist?list=PLxxxxxxxx 裡同一個歌手的歌放在一起
把我的 Spotify 清單 https://open.spotify.com/playlist/xxxxxxxx 照專輯排好
這份清單有沒有重複加入的歌？
```

接下來 AI 會：

1. 讀取你的清單（不會改任何東西）。
2. 在你的電腦上算出「最少要搬哪幾首」。
3. 告訴你結果與成本，例如「要搬 12 首，約用掉今天 6% 的 YouTube 額度」。
4. **等你回覆 OK 才寫回**，寫完再檢查一次結果。

如果你的需求沒講清楚（例如沒說同一個歌手的歌之間要怎麼排），AI 會先問你，不會自己亂猜。

---

## 一個簡單的使用案例

下面是 AI 替你整理「同歌手放一起」時，實際在背後執行的三個指令。
**你不需要自己輸入**，只是讓你知道它在做什麼：

```bash
# 情境：把 YouTube 清單裡「同一位歌手的歌」排在一起

# 1. 讀取清單（只讀，存成 data/current.json）
python -m scripts.yt_tool fetch "https://www.youtube.com/playlist?list=PLxxxxxxxx" --out data/current.json

# 2. 在你的電腦上算出最少的搬法（不花任何額度，清單還沒被動到）
python -m scripts.yt_tool optimize data/current.json --target-out data/new.json --out data/changes.json
#    → 回報例如 need_to_move: 12、estimated_quota: 600，AI 會把這些告訴你並問你要不要繼續

# 3. 你說 OK 之後才寫回（會先確認清單沒被別人改過；中斷了再跑一次會接著做）
python -m scripts.yt_tool update "https://www.youtube.com/playlist?list=PLxxxxxxxx" data/changes.json

# Spotify 也是同樣三步：網址換成 https://open.spotify.com/playlist/…
# 想「同專輯放一起」，就在第 2 步最後加上 --group-by album
```

---

## 常見問題

**會不會把我的清單弄亂？**
寫回前 AI 一定會先讓你確認。工具在寫入前會檢查清單有沒有被其他人改過，有的話就停止，不會硬寫。
寫完之後也會重新讀一次，確認結果跟預定的一樣。萬一中途斷線，清單只是排到一半，歌曲不會增加或消失；
再執行一次就會接著做完。

**要花錢嗎？**
工具和 API 都不收費；只有 Spotify 規定 App 擁有者必須是 Premium 會員。
YouTube 有每日免費額度（10,000 units），每搬一首歌用 50 units，所以一天大約可以搬 200 首。
這個工具只搬必要的歌，通常比一首一首重排省下 25%～70% 的額度。
Spotify 沒有每日額度，而且一次可以搬一整段連在一起的歌。

**想恢復原本的順序？**
跟 AI 說「把清單恢復成整理前的順序」。整理前讀到的清單存在 `data/current.json`，AI 可以照它排回去。
注意下一次整理會覆蓋這個檔案，想保留的話請先請 AI 另存一份。

**可以刪掉重複的歌嗎？**
工具只會**找出**重複的歌並告訴你，不會幫你刪除。刪除無法復原，所以請你自己在 YouTube／Spotify 上操作。

---

## 技術細節

以下內容給開發者或想深入了解的人，預設摺疊。

<details>
<summary><b>系統架構與流程</b></summary>

架構是「**AI 當大腦、Python 當手臂**」：AI 負責理解你的需求、需求不清楚時發問、必要時自己寫出目標順序；
Python 工具負責和 YouTube／Spotify 溝通，以及算出最少的搬動方式。

```
你（用一句話說想怎麼排）
  │
  ▼
AI 助手（Claude Code／Gemini CLI／Codex…）
  讀 SKILL.md → 照 docs/agent/AGENT_SOP.md 的五個 Phase 做：
  │  Phase 0  需求盤點（缺什麼就問）＋ 憑證檢查
  │  Phase 1  fetch     讀取清單
  │  Phase 2  optimize／diff  在本機算出最少搬動（0 額度）
  │  Phase 3  預覽成本 → 等使用者說 OK
  │  Phase 4  update    寫回：寫入前驗證、順序執行、中斷可續傳
  ▼
scripts/yt_tool.py（單一 CLI，依網址自動判斷平台）
  ├── optimizer.py    分組、LIS 最少移動、Spotify 區段搬移規劃
  ├── youtube_api.py  → YouTube Data API v3（OAuth 2.0）
  └── spotify_api.py  → Spotify Web API（PKCE）
```

設計原則：

- **預設走最省的路**：使用者沒說群內要怎麼排，就維持原順序，避免「無意義的搬動」。
- **變更集順序相依**：每次搬動都會讓其他項目重新編號，所以位置是在本機模擬盤面上算出來的，
  必須依 `execution_order` 逐筆執行，不可編輯、重排或跳過。
- **安全優先**：寫入前比對遠端快照（`STALE_SNAPSHOT` 就停），寫入進度存檔可續傳，憑證檔權限 600。

</details>

<details>
<summary><b>檔案結構</b></summary>

```
.
├── README.md / LICENSE
├── AGENTS.md                         # Codex / Copilot 等 Agent 的入口說明
├── .claude/skills/yt-playlist-manager/SKILL.md   # Claude Code 進入點
├── .gemini/skills/yt-playlist-manager/SKILL.md   # Gemini CLI 進入點（必須與上面逐位元組相同）
├── docs/
│   ├── agent/AGENT_SOP.md            # Agent 標準作業程序（單一真實來源）
│   ├── agent/clarify_scenarios.json  # 需求不完整時的追問模板
│   ├── SPOTIFY_SETUP.md              # Spotify App 建立引導
│   ├── SAMPLE_SCHEMA.md              # 測試樣本格式
│   ├── BACKLOG.md                    # 待辦與決策紀錄（含已否決的方案與理由）
│   └── reports/                      # 歷史稽核報告
├── scripts/
│   ├── yt_tool.py                    # CLI 進入點（兩個平台共用）
│   ├── optimizer.py                  # 分組、LIS 錨點、移動與區段搬移規劃
│   ├── youtube_api.py                # YouTube Data API v3 封裝與錯誤分類
│   ├── spotify_api.py                # Spotify Web API：PKCE 登入、讀取、區段搬移
│   ├── schemas.py                    # Pydantic v2 資料模型與指紋工具
│   ├── executor.py                   # 群內排序鍵與配額估算
│   └── cache_manager.py              # 本機 JSON 快取（30 分鐘）
├── tests/
│   ├── test_optimizer.py             # 分組與辨識（53 項）
│   ├── test_reorder_property.py      # 重排正確性窮舉驗證（14 項）
│   ├── test_update_flow.py           # 寫回安全性（7 項）
│   ├── test_spotify_api.py           # Spotify 用戶端（5 項）
│   ├── fixtures/                     # 真實清單樣本
│   ├── dev/                          # 開發者工具（不屬於 skill）
│   └── e2e/                          # 端對端測試（20 個情境，離線）
├── .github/workflows/ci.yml          # CI：單元測試、E2E、SKILL.md 一致性
└── requirements.txt
```

</details>

<details>
<summary><b>完整 CLI 指令</b></summary>

所有指令都輸出一行 JSON 給 Agent 解析。平台由網址自動判斷：`open.spotify.com/playlist/…` 或
`spotify:playlist:…` 是 Spotify，其餘一律當成 YouTube。

```bash
# 憑證（預設存在 ~/.gemini/skills/yt-playlist-manager/credentials，可用 $YT_SKILL_HOME 改位置）
python -m scripts.yt_tool setup_credentials <path_to_client_secret.json>   # YouTube
python -m scripts.yt_tool setup_spotify <client_id>                        # Spotify
python -m scripts.yt_tool spotify-check [<spotify_playlist_url>]           # Spotify 唯讀連線檢查

# 讀取（30 分鐘快取；--refresh 強制重抓）
python -m scripts.yt_tool fetch <playlist_url_or_id> --out data/current.json [--refresh]

# 找重複（0 額度，只列出不刪除；只認完全相同的影片／曲目）
python -m scripts.yt_tool duplicates data/current.json

# 路徑 A：分組（0 額度）
python -m scripts.yt_tool optimize data/current.json \
    --target-out data/new.json --out data/changes.json \
    [--group-by artist|album]            # album 只限 Spotify
    [--group-order first_appearance|alphabetical|count_desc]
    [--within-group-sort <欄位>:<asc|desc>]   # 只在使用者明確要求群內順序時才加
    [--aliases data/artist_aliases.json]      # 只對 YouTube 有效

# 路徑 B：Agent 自己寫好目標順序 data/new.json，再算差異（0 額度）
python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json

# 寫回（寫入前驗證遠端；中斷後重跑同一指令即續傳）
python -m scripts.yt_tool update <playlist_url_or_id> data/changes.json
```

`--within-group-sort` 可用欄位：`viewCount`、`publishedAt`、`duration`、`title`、`channelTitle`、`addedAt`、`trackNumber`。
群組順序請維持預設的 `first_appearance`：字典序會把所有群組重新洗牌，需要搬動的歌會多好幾倍。

</details>

<details>
<summary><b>演算法：為什麼只搬必要的歌</b></summary>

**YouTube**：每次 `playlistItems.update` 只能搬一首、花 50 units，語意是「先移除、再插入」。
工具找出原順序與目標順序之間的**最長遞增子序列（LIS，Patience Sorting，O(N log N)）**，
這些歌當錨點不動，其餘逐首搬到它在目標中的前一首之後。搬動次數等於理論下界 `N − LIS`。
私人／已刪除影片無法移動，會被釘在原位，並把清單切成區段各自計算。

**Spotify**：一次 `PUT /playlists/{id}/items` 可以搬一整段連續的歌。工具先把清單切成
「現在連續、在目標中也連續」的段，對「段」做 LIS，其餘每段各搬一次：
呼叫數 `S − LIS(段)`，永遠不超過逐首搬的 `N − LIS`。

| | YouTube | Spotify |
|:---|:---|:---|
| 分組依據 | 從標題／頻道辨識藝人 | Spotify 直接提供的藝人 ID；另可 `--group-by album` |
| 一次 API 呼叫 | 搬 1 首（50 units） | 搬一整段連續的歌（無每日額度） |
| 最少呼叫數 | `N − LIS` | `S − LIS(段)` |
| 寫回前驗證 | 比對前 50 筆 | 比對整份清單 |
| 寫回後驗證 | — | 重新讀取整份清單，必須等於目標 |

`tests/test_reorder_property.py` 窮舉 n = 2..7 的全部 5,912 種排列，驗證兩種平台的變更集重播後都**精準等於**目標順序，
且呼叫數分別等於 `N − LIS` 與 `S − LIS(段)`。任何改動重排邏輯的變更都必須通過它。

</details>

<details>
<summary><b>YouTube 的藝人辨識規則</b></summary>

YouTube 沒有結構化的藝人資料，所以要從標題與頻道名辨識，兩個訊號會交叉驗證：

| 情境 | 例子 | 判定 |
|:---|:---|:---|
| 兩邊一致（忽略空格） | `Imagine Dragons - Believer` @ `ImagineDragonsVEVO` | 交叉確認，取較易讀的拼法 |
| 反向標題「曲名 - 藝人」 | `One More Light [MV] - Linkin Park` @ `Linkin Park` | 以**頻道**為準 |
| 破折號後是版本標記 | `Valhalla Calling - Duet Version` @ `Miracle Of Sound` | 以**頻道**為準 |
| 合作曲 | `Alan Walker & Torine - Hello World` @ `Alan Walker` | 歸給**上傳藝人** |
| 唱片公司／廠牌頻道 | `Starship - …` @ `RHINO` | 以**標題**為準（廠牌不能吞掉藝人） |
| 本地化譯名前綴 | `紅髮艾德 Ed Sheeran - Perfect` | 併入 `Ed Sheeran` 群 |

`<藝人> - Topic` 與 `<藝人>VEVO` 會正規化到同一群。同一位藝人有不同拼法時，可用 `--aliases` 合併。
`optimize` 也會回報 `grouping_benefit`：分群效益太低（辨識不出的歌太多，或幾乎每首都自成一群）時，
Agent 會先提醒你，不會直接花額度。

Spotify 不需要這一層：它直接給藝人與專輯的 ID。

</details>

<details>
<summary><b>API 額度</b></summary>

| 操作 | YouTube | Spotify |
|:---|:---|:---|
| 讀取清單 | 1 unit / 50 首 | 1 次呼叫 / 50 首 |
| 讀取影片資料 | 1 unit / 50 支 | — |
| 寫回前驗證 | 1 unit | 讀取整份清單 |
| 搬動 | **50 units / 首** | 1 次呼叫 / 段 |
| 每日上限 | 10,000 units | 無每日額度，只有短時間速率限制（自動依 `Retry-After` 等待） |

YouTube 預估超過 2,500 units 時，Agent 會特別提醒；超過 9,000 units 的工作建議分兩天做。

</details>

<details>
<summary><b>測試與驗證</b></summary>

```bash
python tests/test_optimizer.py          # 分組與辨識
python tests/test_reorder_property.py   # 重排正確性窮舉驗證（必跑）
python tests/test_update_flow.py        # 寫回安全性：過期快照、續傳、配額耗盡、重試
python tests/test_spotify_api.py        # Spotify：PKCE 登入、資料對應、401／429／5xx
python tests/e2e/run_e2e.py             # 端對端：真實 CLI 子行程 × 離線替身，20 個情境

# 送出前演練：用真實快照跑真正的 update，對象換成離線替身（0 額度、清單零異動）
python tests/e2e/dryrun.py data/current.json data/changes.json --target data/new.json
```

端對端測試只把網路邊界換掉：YouTube 用行程內替身（`fake_youtube.py`），Spotify 用本機 HTTP 伺服器
（`fake_spotify.py`），測試檔本身不 import `scripts/`，預期結果都獨立算出。詳見 [`tests/e2e/README.md`](tests/e2e/README.md)。

</details>

<details>
<summary><b>支援的 AI 助手與進入點</b></summary>

| AI 助手 | 自動讀取的檔案 |
|:---|:---|
| Claude Code | `.claude/skills/yt-playlist-manager/SKILL.md` |
| Gemini CLI / Antigravity | `.gemini/skills/yt-playlist-manager/SKILL.md` |
| Codex / GitHub Copilot Workspace 等 | 根目錄 `AGENTS.md` |

三者都指向同一份流程 `docs/agent/AGENT_SOP.md`。兩份 `SKILL.md` 必須逐位元組相同，CI 會檢查。

</details>

---

## 授權條款

本專案的程式碼以 [MIT License](LICENSE) 授權：你可以自由使用、修改與散布，只要保留版權聲明；
本軟體依「現狀」提供，不附任何擔保。

使用本工具存取第三方服務時，你需要**自行**遵守該服務的條款：

- YouTube：[YouTube API 服務條款](https://developers.google.com/youtube/terms/api-services-terms-of-service)
  與 [Google 隱私權政策](https://policies.google.com/privacy)。
- Spotify：[Spotify Developer Terms](https://developer.spotify.com/terms)
  與 [Design Guidelines](https://developer.spotify.com/documentation/design)。

你的 OAuth 憑證、Spotify Client ID 與登入 token 只存在你自己的電腦上，本專案不會收集或傳送任何資料。

本專案與 Google、YouTube、Spotify 沒有任何關聯，也未獲得其背書。YouTube 是 Google LLC 的商標，
Spotify 是 Spotify AB 的商標。
