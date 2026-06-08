# YouTube Playlist Manager (Antigravity Agent-Skill)

這是一個專為 Antigravity Agent 設計的 YouTube / YouTube Music 播放清單管理技能。
透過 Agent 的自然語言理解與大腦調度，你可以輕鬆要求 Agent 幫你自動完成 YouTube 播放清單的各種複雜排序與篩選。

## 架構特色 (Agent-Skill Pattern)

本專案採用了先進的 **Agent-Skill 架構**：
1. **Agent 作為大腦**：捨棄了傳統的 CLI 終端機介面與腳本內建 LLM。你只需在聊天視窗中對 Agent 說話，Agent 會負責理解你的意圖。
2. **無限制客製化**：遇到複雜的排序條件時，Agent 可以在背景為你即時撰寫 Python 腳本來過濾資料，不受限於任何預設的篩選器。
3. **強制預覽與安全防護**：Agent 會使用 Markdown 在聊天室中畫出「變更預覽表」與「API 配額消耗預估」。在你明確說「OK」之前，絕對不會寫回 YouTube。
4. **工具純粹化**：底層的 Python 腳本 (`yt_tool.py`) 僅提供無狀態的 `fetch`, `diff`, `update` 三個指令，不干涉對話邏輯。

---

## 快速開始

### 1. 安裝底層依賴
```bash
pip install -r requirements.txt
```

### 2. 設定 OAuth 2.0 憑證
1. 在 [Google Cloud Console](https://console.cloud.google.com) 啟用 YouTube Data API v3
2. 建立 OAuth 2.0 Client ID（桌面應用程式）
3. 下載 JSON 並重新命名為 `client_secret.json`
4. 將其放置於全域安全路徑：`~/.gemini/skills/yt-playlist-manager/credentials/client_secret.json`

### 3. 如何使用
只要喚醒具備此 Skill 的 Agent，並直接在對話中說：
- 「幫我把 YouTube 播放清單 PLxxxxxxxxx 按照觀看次數從高到低重新排列。」
- 「幫我整理 PLxxxxxxxxx，把超過 10 分鐘的影片移到最後面，剩下的按發布日期排序。」

Agent 會自動：
1. 呼叫底層工具獲取資料。
2. 計算新排序。
3. 在聊天室給出 Markdown 差異預覽表。
4. 等待你的「同意」後執行 API 寫回。

---

## 專案結構

```
├── .gemini/skills/yt-playlist-manager/
│   └── SKILL.md               # 注入給 Agent 的 SOP 大腦指令
├── scripts/
│   ├── schemas.py             # 資料結構定義 (Pydantic)
│   ├── youtube_api.py         # YouTube Data API 封裝
│   ├── executor.py            # 配額估算與 Diff 引擎
│   ├── cache_manager.py       # 本地快取層，減少 API 消耗
│   └── yt_tool.py             # Agent 專用的背景呼叫工具
├── requirements.txt
└── README.md
```

## API 配額與限制

| 操作 | 預估配額消耗 |
|------|-------------|
| 讀取清單 | 1 unit/次 |
| 讀取 Metadata | 1 unit/50支影片 |
| 更新影片位置 | **50 units/次** (極耗配額) |

> ⚠️ YouTube Data API v3 每日預設上限為 10,000 units。Agent 在預估單次操作超過 2,500 units 時，會主動給予警告。

## 授權條款

此專案供個人使用。使用前需遵守 [YouTube API 服務條款](https://developers.google.com/youtube/terms/api-services-terms-of-service)。
