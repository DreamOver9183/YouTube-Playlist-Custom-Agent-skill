# YouTube Playlist Manager — AI Agent Skill & 通用認知排序引擎

一個通用的 AI Agent-Skill 架構 YouTube 播放清單管理工具與 **通用型認知分群與多維度排序思考引擎 (Cognitive Sorting & Grouping Engine)**。廣泛支援 **Claude Code**、**Codex**、**GitHub Copilot Workspace**、**Antigravity CLI** 等多種開發者 AI Agent 框架。

本專案採用 **「Agent 為大腦、雙引擎為手臂」** 的架構設計：
1. **Python API 後台子系統 (`scripts/`)**：負責 YouTube Data API v3 通訊、OAuth 2.0 授權、Patience Sorting LIS 錨點演算法、模擬盤面的精確移動規劃與斷點續傳寫回。
2. **TypeScript 認知排序引擎 (`src/`)**：提供零硬編碼、強型別安全與 Sub-Agent 斷層掃描修復的通用型排序思考管道，可透過 `npm run plan` 直接接入上述流程。

---

## 快速開始

### 1. 於 AI Agent 中啟用此 Skill

將以下 Prompt 貼入您的 AI Agent 聊天視窗，Agent 會自動完成環境建置與依賴安裝：

```
請幫我使用 https://github.com/DreamOver9183/YouTube-Playlist-Custom-Agent-skill 這個 skill
```

各框架的自動載入進入點（內容皆指向同一份 `docs/agent/AGENT_SOP.md`）：

| 框架 | 進入點 |
|:---|:---|
| Claude Code | `.claude/skills/yt-playlist-manager/SKILL.md` |
| Gemini CLI / Antigravity | `.gemini/skills/yt-playlist-manager/SKILL.md` |
| Codex / GitHub Copilot Workspace 等 | 根目錄 `AGENTS.md` |

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
│  │ Phase 4: update → 一致性驗證 + 循序寫回 (含斷點續傳)         │  │
│  └─────────────────────────────────────────────────────────────┘  │
│        ↓ 呼叫                                            ↑ JSON   │
├────────┼──────────────────────────────────────────────────┼────────┤
│        ↓                                                 ↑        │
│  [TypeScript 通用認知排序引擎] (src/) ── npm run plan 接入流程    │
│  ├── Extractor   → 解析屬性特徵與自然語言意圖 (中英雙語)          │
│  ├── Planner     → 建構多階思考樹 (高階實體歸類 優先於 純量排序) │
│  ├── Comparator  → 動態合成組合比較器 (Composite Pattern)          │
│  └── Evaluator   → 分群斷層 + 群內單調性掃描與修復迴圈 (上限 3 次)│
│                                                                   │
│  [Python API 後台工具與配額引擎] (scripts/yt_tool.py)             │
│  ├── setup_credentials → 驗證並安全複製憑證 (0o600 權限)          │
│  ├── fetch    → youtube_api.py + cache_manager.py (--refresh)     │
│  ├── optimize → optimizer.py (LIS 錨點 + 群內排序)               │
│  ├── diff     → optimizer.plan_reorder (同樣套用 LIS 錨點)        │
│  └── update   → youtube_api.py (一致性驗證 + 順序寫回 + 續傳)     │
│                      ↓                                            │
│              YouTube Data API v3                                  │
└───────────────────────────────────────────────────────────────────┘
```

### 檔案結構

```
YouTube Playlist skill/
├── AGENTS.md                           # Codex / Copilot 等 Agent 的入口說明
├── .claude/skills/yt-playlist-manager/
│   └── SKILL.md                        # Claude Code Skill 進入點
├── .gemini/
│   └── skills/
│       └── yt-playlist-manager/
│           ├── SKILL.md                # Gemini CLI Skill 註冊與 5-Phase SOP
│           └── config.json             # Skill 註冊設定
├── docs/
│   ├── agent/
│   │   └── AGENT_SOP.md            # 通用 Agent 標準作業程序（單一真實來源）
│   └── reports/
│       ├── architecture_audit_2026-08.md  # 架構與演算法稽核報告
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
│   ├── cli.ts                          # 橋接 CLI：current.json → new.json
│   └── index.ts                        # 專案統一進入點 (cognitiveSort)
├── scripts/                            # 【Python API 子系統】
│   ├── yt_tool.py                      # 後台 CLI 工具進入點
│   ├── optimizer.py                    # LIS 錨點、移動規劃與三層藝人辨識
│   ├── youtube_api.py                  # YouTube Data API v3 封裝與錯誤分類
│   ├── executor.py                     # 排序/篩選與 Diff 計算
│   ├── cache_manager.py                # 本地 JSON 快取層 (30-min TTL)
│   ├── schemas.py                      # Pydantic v2 資料模型與指紋工具
│   └── __init__.py
├── tests/
│   ├── test_cognitive_engine.ts        # TypeScript 認知引擎測試 (28 項)
│   ├── test_optimizer.py               # Python 最佳化與辨識測試 (24 項)
│   ├── test_reorder_property.py        # 重排正確性窮舉驗證 (12 項)
│   └── test_update_flow.py             # 寫回安全性測試 (7 項)
├── .github/workflows/ci.yml            # CI：型別檢查與四組測試
├── package.json
├── tsconfig.json
├── tsconfig.test.json
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

# 2. 獲取播放清單資料（帶 30 分鐘快取；--refresh 可強制重抓）
python -m scripts.yt_tool fetch <playlist_id_or_url> --out data/current.json [--refresh]

# 3. (選項 A) 歌手分組 + 群內排序 + LIS 錨點最佳化（0 API 配額）
python -m scripts.yt_tool optimize data/current.json \
    --target-out data/new.json --out data/changes.json \
    --group-order first_appearance --within-group-sort viewCount:desc

# 4. (選項 B) 計算自訂順序 (data/new.json) 與原清單差異；同樣套用 LIS 錨點
python -m scripts.yt_tool diff data/current.json data/new.json --out data/changes.json

# 5. 將差異寫回 YouTube（寫回前 1 unit 一致性驗證 + 順序執行 + 斷點續傳）
python -m scripts.yt_tool update <playlist_id_or_url> data/changes.json
```

> 變更檔中的移動是**順序相依**的：每一次 `playlistItems.update` 都會讓其餘影片重新編號，
> 因此位置是在本地模擬盤面上即時算出的，必須依 `execution_order` 逐筆執行，不可重新排序或跳過。

### 2. TypeScript 認知排序引擎

作為 CLI 接入播放清單流程（讀 `current.json` → 排序 → 寫 `new.json`）：

```bash
npm run build
npm run plan -- --input data/current.json --output data/new.json \
    --intent "把同一個頻道的影片放在一起，觀看次數由高到低"
# 或明確指定欄位
npm run plan -- -i data/current.json -o data/new.json \
    --group-by channel_title --sort-by view_count:desc
```

作為函式庫使用：

```typescript
import { cognitiveSort, TrackListAdapter } from './dist/index.js';

// 方式 A：便利函式直接調用
const result = cognitiveSort(myItems, {
  text: "請將同專輯曲目歸類，並按軌號升冪排序"
});

console.log(result.items);                    // 排序完成之陣列
console.log(result.evaluation.isContinuous);  // 分群連續性與群內排序單調性檢查結果
console.log(result.evaluation.gaps);          // 若有斷層，指出是哪個欄位被打散

// 方式 B：使用專用適配器
const trackAdapter = new TrackListAdapter();
const sortedTracks = trackAdapter.sortTracks(rawTracks);
```

> 分群維度預設以 `groupOrder: 'first_appearance'` 排列群組。重排既有清單時請保持此設定：
> 字典序會把所有群組重新洗牌，需要移動的項目數（也就是 API 配額）會高出好幾倍。

---

## 技術測試與驗證

```bash
# TypeScript 嚴格型別檢查（含 tests，Strict Mode、零 any）
npm run typecheck

# TypeScript 認知引擎測試（會自動先 build）
npm test

# Python 演算法與辨識單元測試
python tests/test_optimizer.py

# 重排正確性窮舉驗證：n=2..7 全排列（5912 組）重播後必須等於目標順序，
# 且移動次數等於理論下界 N − LIS
python tests/test_reorder_property.py

# 寫回安全性：過期快照、中斷續傳、進度污染、配額耗盡、退避重試
python tests/test_update_flow.py
```

`tests/test_reorder_property.py` 是整個專案的核心把關：任何改動重排邏輯的變更都必須讓它通過。

---

## API 配額參考與最佳化

| 操作 | 預估配額消耗 |
|:---|:---|
| 讀取清單 (`playlistItems.list`) | 1 unit / 次 |
| 讀取影片 Metadata (`videos.list`) | 1 unit / 50 支影片 |
| 寫回前一致性驗證 | 1 unit / 次 |
| 更新影片位置 (`playlistItems.update`) | **50 units / 次** |

YouTube Data API v3 每日預設上限為 10,000 units。專案透過 **Patience Sorting LIS 錨點演算法**，把需要的 `update` 次數壓到理論下界 `N − LIS`；對一般的分組重排可 **節省 25% 至 70% 的 API 配額**（實際比例取決於原順序與目標順序的相似度）。工具在 `diff` / `optimize` 階段會精確預估配額消耗，Agent 會在超過 2,500 units 時主動向使用者警示。

---

## 授權條款

此專案供個人使用。YouTube Data API 的使用需遵守 [YouTube API 服務條款](https://developers.google.com/youtube/terms/api-services-terms-of-service)。
