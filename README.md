# YouTube Playlist Manager — Antigravity Agent-Skill

一個基於 [Antigravity](https://github.com/google-deepmind/antigravity) Agent-Skill 架構的 YouTube 播放清單管理工具。透過 YouTube Data API v3 讀取播放清單資料，由 Agent 依據使用者的自然語言指令計算排序，經確認後寫回 YouTube。

本專案**不是**一個獨立的 CLI 應用程式。它是一組提供給 Antigravity Agent 呼叫的後台工具與 SOP 指令集。Agent 負責理解需求與互動，Python 腳本僅負責 API 通訊與資料運算。

---

## 快速開始

### 1. 安裝依賴

```bash
pip install -r requirements.txt
```

### 2. 設定 Google Cloud OAuth 憑證

1. 前往 [Google Cloud Console](https://console.cloud.google.com)，建立專案並啟用 **YouTube Data API v3**。
2. 在「API 和服務 → 憑證」中建立 **OAuth 2.0 Client ID**（類型選「桌面應用程式」）。
3. 下載 JSON 憑證檔案。

> **首次使用時，工具會自動彈出系統原生的檔案選擇視窗**（僅顯示 `.json`），引導你選取憑證。選取後，工具會自動驗證內容並將其複製到安全路徑 `~/.gemini/skills/yt-playlist-manager/credentials/client_secret.json`，無需手動移動檔案。

### 3. 透過 Agent 使用（標準方式）

在 Antigravity 聊天視窗中直接對 Agent 下達指令，例如：

```
「幫我把播放清單 PLxxxxxxxxx 按觀看次數從高到低排列」
「整理 https://www.youtube.com/playlist?list=PLxxxxxxxxx，把超過 10 分鐘的影片移到最後面」
```

Agent 會自動執行以下流程：

```
Phase 0  確認 Playlist ID，檢查憑證（自動彈窗引導）
   ↓
Phase 1  呼叫 fetch 取得清單資料
   ↓
Phase 2  根據需求計算新的排序
   ↓
Phase 3  在聊天室顯示變更預覽表 ← 等待你確認
   ↓
Phase 4  確認後呼叫 update 寫回 YouTube
```

### 4. 底層工具 CLI 語法（供 Agent 或進階使用者參考）

```bash
# 獲取播放清單資料
python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json

# 計算新舊清單的差異與配額預估
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
├── .gemini/skills/yt-playlist-manager/
│   ├── SKILL.md                # Agent SOP：定義 Phase 0~4 的執行流程
│   └── config.json             # Skill 註冊設定
├── scripts/
│   ├── yt_tool.py              # Agent 呼叫的後台工具入口（fetch / diff / update）
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
│ Antigravity Agent                                         │
│                                                           │
│  SKILL.md (SOP)                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ Phase 0: 需求確認 + 憑證自動偵測（OpenFileDialog）  │  │
│  │ Phase 1: fetch → 取得清單                           │  │
│  │ Phase 2: Agent 計算排序（可動態寫腳本）             │  │
│  │ Phase 3: diff → 預覽 + 配額警告 ← 等待使用者確認   │  │
│  │ Phase 4: update → 寫回 YouTube                      │  │
│  └─────────────────────────────────────────────────────┘  │
│        ↓ 呼叫                                    ↑ JSON   │
├────────┼──────────────────────────────────────────┼────────┤
│        ↓                                         ↑        │
│  yt_tool.py (CLI 後台工具)                                │
│  ├── ensure_credentials()  → tkinter OpenFileDialog       │
│  ├── fetch  → youtube_api.py + cache_manager.py           │
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
| API 客戶端 | google-api-python-client |
| OAuth 認證 | google-auth, google-auth-oauthlib |
| 憑證引導 | tkinter（Python 標準函式庫，無需額外安裝） |
| 日誌 | Python logging 模組 → `scripts/logs/yt_skill.log` |
| 快取 | 本地 JSON 檔案（`scripts/cache/`，預設 30 分鐘 TTL） |
| 架構模式 | Antigravity Agent-Skill（Agent 為大腦，腳本為工具） |

### 使用環境

| 項目 | 需求 |
|------|------|
| 作業系統 | Windows（OpenFileDialog 依賴 tkinter GUI）|
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
