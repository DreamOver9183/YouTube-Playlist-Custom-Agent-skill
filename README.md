# YouTube Playlist Manager — AI Agent Skill

一個通用的 AI Agent-Skill 架構 YouTube 播放清單管理工具，廣泛支援 **Claude Code**、**Codex**、**GitHub Copilot Workspace** 等多種開發者 AI Agent 框架。透過 YouTube Data API v3 讀取播放清單資料，由 Agent 依據使用者的自然語言指令計算排序，經確認後寫回 YouTube。

本專案**不是**一個獨立的 CLI 應用程式。它是一組提供給 AI Agent 呼叫的後台工具與標準作業程序（SOP）。Agent 負責理解需求與互動，Python 腳本僅負責 API 通訊與資料運算。任何支援終端執行與程式碼產生的 Agent 皆可閱讀 `docs/agent/` 目錄下的 [AGENT_SOP.md](file:///d:/Antigravity%20Code/YouTube%20Playlist%20skill/docs/agent/AGENT_SOP.md) 以遵循相同的運作流程。

---

## 快速開始

將以下 Prompt 貼入您的 AI Agent 聊天視窗，Agent 會自動完成環境建置與依賴安裝：

```
請幫我使用 https://github.com/DreamOver9183/YouTube-Playlist-Custom-Agent-skill 這個 skill
```

### 前置需求：Google Cloud OAuth 憑證

使用前需先在 [Google Cloud Console](https://console.cloud.google.com) 完成以下設定：

1. 建立專案並啟用 **YouTube Data API v3**。
2. 在「API 和服務 → 憑證」中建立 **OAuth 2.0 Client ID**（類型選「桌面應用程式」）。
3. 下載 JSON 憑證檔案（放在任意位置即可）。

> 首次使用時，Agent 在偵測到沒有憑證時，會主動在對話視窗要求您輸入憑證的絕對路徑。之後 Agent 會執行 `setup_credentials` 指令自動驗證並複製到安全路徑。

### 使用範例

環境就緒後，直接在聊天視窗對 Agent 下達指令：

```
「幫我把播放清單 PLxxxxxxxxx 按觀看次數從高到低排列」
「整理 https://www.youtube.com/playlist?list=PLxxxxxxxxx，把超過 10 分鐘的影片移到最後面」
```

Agent 的執行流程：

```
Phase 0  確認 Playlist ID，檢查憑證（若缺失則在聊天室引導使用者輸入路徑）
   ↓
Phase 1  呼叫 fetch 取得清單資料
   ↓
Phase 2  本地計算新排序（可呼叫內建 optimize 指令零配額分組，或由 Agent 撰寫腳本）
   ↓
Phase 3  在聊天室顯示變更預覽表與配額估算 ← 等待您確認
   ↓
Phase 4  確認後呼叫 update 寫回 YouTube
```

### 4. 底層工具 CLI 語法（供 Agent 或進階使用者參考）

```bash
# 設定憑證檔案
python -m scripts.yt_tool setup_credentials <path_to_client_secret.json>

# 獲取播放清單資料
python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json

# (選項 A) 使用內建引擎將相同歌手分組最佳化（0 API配額，自動產生最小變更）
python -m scripts.yt_tool optimize data/current.json --target-out data/new.json --out data/changes.json

# (選項 B) 使用者自訂順序 (data/new.json) 後，計算新舊清單的差異與配額預估
python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json

# 將差異寫回 YouTube
python -m scripts.yt_tool update <playlist_id_or_url> data/changes.json
```

所有指令的 stdout 輸出皆為 JSON 格式，供 Agent 解析。範例回傳：

```json
// fetch 成功
{"status": "success", "item_count": 42, "file": "data/current.json"}

// diff 成功
{"status": "success", "changes_count": 15, "estimated_quota": 750, "file": "data/changes.json"}

// update 成功
{"status": "success", "total": 15, "successful": 15, "failed": 0, "quota_used": 750}
```

---

## 專案架構

### 檔案結構

```
YouTube Playlist skill/
├── docs/
│   ├── agent/
│   │   └── AGENT_SOP.md        # Agent 通用標準作業程序
│   └── reports/
│       └── v1.5更新20260620.md   # 版本更新與架構報告（供人類閱讀）
├── .gemini/skills/yt-playlist-manager/
│   ├── SKILL.md                # Agent Skill 註冊設定檔
│   └── config.json             # Skill 註冊設定
├── scripts/
│   ├── yt_tool.py              # Agent 呼叫的後台工具入口（fetch / optimize / diff / update）
│   ├── optimizer.py            # API 配額最佳化引擎（LIS 錨點演算法、三層藝人辨識）
│   ├── youtube_api.py          # YouTube Data API v3 封裝（OAuth 認證、清單讀取、位置更新）
│   ├── schemas.py              # Pydantic 資料模型定義（PlaylistItem, PositionChange 等）
│   ├── executor.py             # 排序引擎與 Diff 演算法
│   ├── cache_manager.py        # 本地 JSON 快取層（減少重複 API 請求）
│   └── __init__.py
├── requirements.txt
├── .gitignore
└── README.md
```

### 系統架構

```
┌───────────────────────────────────────────────────────────┐
│ AI Agent (Claude/Codex/Copilot/etc.)                      │
│                                                           │
│  SKILL.md (SOP)                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ Phase 0: 需求確認 + 憑證問答（無 GUI）              │  │
│  │ Phase 1: fetch → 取得清單                           │  │
│  │ Phase 2: optimize (內建分組) 或 Agent 寫腳本排序    │  │
│  │ Phase 3: diff/optimize → 預覽 + 配額警告 ← 等待確認 │  │
│  │ Phase 4: update → 寫回 YouTube                      │  │
│  └─────────────────────────────────────────────────────┘  │
│        ↓ 呼叫                                    ↑ JSON   │
├────────┼──────────────────────────────────────────┼────────┤
│        ↓                                         ↑        │
│  yt_tool.py (CLI 後台工具)                                │
│  ├── setup_credentials() → 驗證並複製憑證                 │
│  ├── fetch    → youtube_api.py + cache_manager.py         │
│  ├── optimize → optimizer.py (LIS最小化移動 + Regex解析)  │
│  ├── diff   → executor.py (compute_diff / estimate_quota) │
│  └── update → youtube_api.py (循序寫入 + 中斷續傳)       │
│                      ↓                                    │
│              YouTube Data API v3                          │
└───────────────────────────────────────────────────────────┘
```

### 使用技術

| 類別 | 技術 |
|------|------|
| 語言 | Python 3.10+ |
| API | YouTube Data API v3（OAuth 2.0 授權） |
| 資料驗證 | Pydantic v2 |
| 模糊比對 | RapidFuzz（用於藝人名稱智慧辨識）|
| API 客戶端 | google-api-python-client |
| OAuth 認證 | google-auth, google-auth-oauthlib |
| 日誌 | Python logging 模組 → `scripts/logs/yt_skill.log` |
| 快取 | 本地 JSON 檔案（`scripts/cache/`，預設 30 分鐘 TTL） |
| 最佳化演算法 | Patience Sorting (O(N log N) LIS) 最小化 API 消耗 |
| 架構模式 | Agent-Skill 通用架構（Agent 為大腦，腳本為工具） |

### 使用環境

| 項目 | 需求 |
|------|------|
| 作業系統 | 跨平台（支援 Windows, macOS, Linux 等無 GUI/Headless 環境）|
| Python 版本 | 3.10 以上 |
| 網路 | 需要存取 YouTube Data API v3 |
| Google 帳號 | 需具備目標播放清單的編輯權限 |

### API 配額參考

| 操作 | 預估配額消耗 |
|------|-------------|
| 讀取清單 (`playlistItems.list`) | 1 unit / 次 |
| 讀取影片 Metadata (`videos.list`) | 1 unit / 50 支影片 |
| 更新影片位置 (`playlistItems.update`) | **50 units / 次** |

YouTube Data API v3 每日預設上限為 10,000 units。工具在 `diff` 階段會預估配額消耗，Agent 在超過 2,500 units 時會主動發出警告。

---

## 授權條款

此專案供個人使用。YouTube Data API 的使用需遵守 [YouTube API 服務條款](https://developers.google.com/youtube/terms/api-services-terms-of-service)。
