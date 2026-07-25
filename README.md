# YouTube Playlist Manager — AI Agent Skill & 通用認知排序引擎

一個通用的 AI Agent-Skill 架構 YouTube 播放清單管理工具與 **通用型認知分群與多維度排序思考引擎 (Cognitive Sorting & Grouping Engine)**。廣泛支援 **Claude Code**、**Codex**、**GitHub Copilot Workspace**、**Antigravity CLI** 等多種開發者 AI Agent 框架。

本專案採用 **「Agent 為大腦、雙引擎為手臂」** 的架構設計：
1. **Python API 後台子系統 (`scripts/`)**：負責 YouTube Data API v3 通訊、OAuth 2.0 授權、Patience Sorting LIS 錨點演算法、尾端優先 (Tail-First) 漂移防護與增量斷點寫回。
2. **TypeScript 認知排序引擎 (`src/`)**：提供零硬編碼、強型別安全與 Sub-Agent 斷層掃描修復的通用型排序思考管道。

---

## 快速開始

### 1. 於 AI Agent 中啟用此 Skill

將以下 Prompt 貼入您的 AI Agent 聊天視窗，Agent 會自動完成環境建置與依賴安裝：

```
請幫我使用 https://github.com/DreamOver9183/YouTube-Playlist-Custom-Agent-skill 這個 skill
```

### 2. 前置需求：Google Cloud OAuth 憑證

使用前需先在 [Google Cloud Console](https://console.cloud.google.com) 完成以下設定：

1. 建立專案並啟用 **YouTube Data API v3**。
2. 在「API 和服務 → 憑證」中建立 **OAuth 2.0 Client ID**（類型選「桌面應用程式」）。
3. 下載 JSON 憑證檔案（放在任意位置即可）。

> 首次使用時，若偵測到沒有憑證，工具會主動在對話視窗要求您輸入憑證的絕對路徑。之後 Agent 會執行 `setup_credentials` 指令自動驗證並複製到安全路徑（限制檔名與權限為 `0o600` / `0o700`）。

### 3. 自然語言指令範例

環境就緒後，直接在聊天視窗對 Agent 下達指令：

```
「幫我把播放清單 PLxxxxxxxxx 按歌手歸類，同歌手按觀看次數從高到低排列」
「整理 https://www.youtube.com/playlist?list=PLxxxxxxxxx，把超過 10 分鐘的影片移到最後面」
```

---

## 專案架構與系統流程

### 系統架構圖

```
┌───────────────────────────────────────────────────────────────────┐
│ AI Agent (Claude / Codex / Copilot / Gemini CLI)                  │
│                                                                   │
│  SKILL.md (Agent SOP)                                             │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Phase 0: 需求診斷 + 憑證檢查 (無 GUI 自動彈窗)             │  │
│  │ Phase 1: fetch → 讀取與整合清單資料                          │  │
│  │ Phase 2: Cognitive Engine / optimize 零配額本地計算排序     │  │
│  │ Phase 3: diff/optimize → 異動預覽表 + 配額警告 ← 等待確認   │  │
│  │ Phase 4: update → 循序寫回 YouTube (含斷點續傳)               │  │
│  └─────────────────────────────────────────────────────────────┘  │
│        ↓ 呼叫                                            ↑ JSON   │
├────────┼──────────────────────────────────────────────────┼────────┤
│        ↓                                                 ↑        │
│  [TypeScript 通用認知排序引擎] (src/)                            │
│  ├── Extractor   → 解析屬性特徵與自然語言意圖                     │
│  ├── Planner     → 建構多階思考樹 (高階實體歸類 優先於 純量排序) │
│  ├── Comparator  → 動態合成組合比較器 (Composite Pattern)          │
│  └── Evaluator   → Sub-Agent 斷層掃描與修復迴圈 (上限 3 次疊代)  │
│                                                                   │
│  [Python API 後台工具與配額引擎] (scripts/yt_tool.py)             │
│  ├── setup_credentials → 驗證並安全複製憑證 (0o600 權限)          │
│  ├── fetch    → youtube_api.py + cache_manager.py                 │
│  ├── optimize → optimizer.py (Patience Sorting LIS 錨點演算法)   │
│  ├── diff     → executor.py (compute_diff / estimate_quota)       │
│  └── update   → youtube_api.py (尾端優先寫回 + 斷點續傳)          │
│                      ↓                                            │
│              YouTube Data API v3                                  │
└───────────────────────────────────────────────────────────────────┘
```

### 檔案結構

```
YouTube Playlist skill/
├── .gemini/
│   └── skills/
│       └── yt-playlist-manager/
│           ├── SKILL.md                # Agent Skill 註冊與 5-Phase SOP
│           └── config.json             # Skill 註冊設定
├── docs/
│   ├── agent/
│   │   └── AGENT_SOP.md            # 通用 Agent 標準作業程序
│   └── reports/
│       ├── v2.1更新20260620.md       # 配額引擎與 LIS 演算法報告
│       └── archive_implementation_plan_v2.md
├── src/                                # 【TypeScript 認知排序引擎】
│   ├── core/
│   │   ├── types/
│   │   │   ├── entity.ts           # 泛型 EntityRecord 與屬性提取器
│   │   │   └── schema.ts           # CognitivePlan, GroupDimension 等定義
│   │   ├── cognitive/
│   │   │   ├── Extractor.ts        # 語意與屬性提取器
│   │   │   ├── Planner.ts          # 階層排序思考樹產生器
│   │   │   └── Evaluator.ts        # Sub-Agent 自我檢驗修復迴圈
│   │   └── engine/
│   │       ├── ComparatorBuilder.ts# 動態比較器組合管道
│   │       └── SortingEngine.ts    # 核心排序執行引擎
│   ├── adapters/
│   │   ├── TrackListAdapter.ts     # 音樂曲目資料適配器範例
│   │   └── ECommerceAdapter.ts     # 電商商品資料適配器範例
│   └── index.ts                        # 專案統一進入點 (cognitiveSort)
├── scripts/                            # 【Python API 子系統】
│   ├── yt_tool.py                      # 後台 CLI 工具進入點
│   ├── optimizer.py                    # LIS 錨點演算法與三層藝人辨識
│   ├── youtube_api.py                  # YouTube Data API v3 封裝
│   ├── executor.py                     # 排序/篩選與 Diff 計算
│   ├── cache_manager.py                # 本地 JSON 快取層 (30-min TTL)
│   ├── schemas.py                      # Pydantic v2 資料模型
│   └── __init__.py
├── tests/
│   ├── test_cognitive_engine.ts        # TypeScript 認知引擎單元測試 (13 項)
│   └── test_optimizer.py               # Python 最佳化與 API 測試 (24 項)
├── package.json
├── tsconfig.json
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 呼叫語法與 CLI 指令

### 1. Python 後台工具 CLI

```bash
# 1. 設定憑證檔案（安全寫入 0o600 權限）
python -m scripts.yt_tool setup_credentials <path_to_client_secret.json>

# 2. 獲取播放清單資料（帶快取機制）
python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json

# 3. (選項 A) 執行歌手分組與 LIS 錨點最佳化（0 API 配額，自動產生最小變更）
python -m scripts.yt_tool optimize data/current.json --target-out data/new.json --out data/changes.json

# 4. (選項 B) 計算自訂順序 (data/new.json) 與原清單差異及配額預估
python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json

# 5. 將差異寫回 YouTube（尾端優先排序 + 斷點紀錄）
python -m scripts.yt_tool update <playlist_id_or_url> data/changes.json
```

### 2. TypeScript 認知排序引擎使用範例

```typescript
import { cognitiveSort, TrackListAdapter } from './dist/index.js';

// 方式 A：便利函式直接調用
const result = cognitiveSort(myItems, {
  text: "請將同專輯曲目歸類，並按軌號升冪排序"
});

console.log(result.items); // 排序完成之陣列
console.log(result.evaluation.isContinuous); // Sub-Agent 自我校驗連續性結果

// 方式 B：使用專用適配器
const trackAdapter = new TrackListAdapter();
const sortedTracks = trackAdapter.sortTracks(rawTracks);
```

---

## 技術測試與驗證

本專案具備完整且自動化的雙雙驗證機制：

```bash
# 1. 執行 TypeScript 嚴格型別檢查 (Strict Mode, 零 any)
npm run typecheck

# 2. 執行 TypeScript 認知引擎測試 (13/13 Passed)
npm run test

# 3. 執行 Python API 與最佳化引擎測試 (24/24 Passed)
python tests/test_optimizer.py
```

---

## API 配額參考與最佳化

| 操作 | 預估配額消耗 |
|:---|:---|
| 讀取清單 (`playlistItems.list`) | 1 unit / 次 |
| 讀取影片 Metadata (`videos.list`) | 1 unit / 50 支影片 |
| 更新影片位置 (`playlistItems.update`) | **50 units / 次** |

YouTube Data API v3 每日預設上限為 10,000 units。專案透過 **Patience Sorting LIS 錨點演算法** 可為大量影片排列 **節省 25% 至 70% 的 API 配額**。工具在 `diff` / `optimize` 階段會精確預估配額消耗，Agent 會在超過 2,500 units 時主動向使用者警示。

---

## 授權條款

此專案供個人使用。YouTube Data API 的使用需遵守 [YouTube API 服務條款](https://developers.google.com/youtube/terms/api-services-terms-of-service)。
