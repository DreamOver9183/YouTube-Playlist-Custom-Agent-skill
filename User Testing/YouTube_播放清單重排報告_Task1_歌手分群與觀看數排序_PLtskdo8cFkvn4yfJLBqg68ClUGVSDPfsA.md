# YouTube 播放清單重排執行報告 — Task 1 (歌手分群與熱門度排序)

## 📌 基本資訊

- **任務名稱**：YouTube 播放清單 — 同歌手/樂團聚集與觀看次數降冪排序
- **執行時間**：2026-09-01 11:03:00 (UTC+8)
- **目標清單網址**：[https://youtube.com/playlist?list=PLtskdo8cFkvn4yfJLBqg68ClUGVSDPfsA](https://youtube.com/playlist?list=PLtskdo8cFkvn4yfJLBqg68ClUGVSDPfsA)
- **播放清單 ID**：`PLtskdo8cFkvn4yfJLBqg68ClUGVSDPfsA`
- **清單總影片數**：**192 支影片**
- **執行狀態**：✅ **成功完成 (100% 精準吻合目標順序)**

---

## 🎯 需求與排序邏輯

### 1. 使用者需求
> 「把播放清單中同歌手/樂團的影片整齊排序」

### 2. 排序規則設計
1. **同歌手／樂團完整聚集**：
   - 跨語言與多別名整合（例如將 `米津玄師` 與 `Kenshi Yonezu`、`優里` 與 `Yuuri`、`Official髭男dism`、`藍井エイル` 與 `Eir Aoi`、`藤川千愛` 與 `Chiai Fujikawa`、`MAN WITH A MISSION` 等多名稱變體統一收斂至同一藝人群組）。
2. **群內依熱門度排序**：
   - 同一位歌手群內的歌曲，依照 **YouTube 觀看次數 (View Count) 由高到低（降冪）** 排列，熱門主打歌優先播放。
3. **群組保留初見順序 (`first_appearance`)**：
   - 保留藝人在清單中第一次出現的先後順序，避免字典序重新大洗牌，以達成**最小移動數與最省配額**。

---

## ⚙️ 演算法與技術架構

- **演算法**：LIS (Longest Increasing Subsequence 最長遞增子序列) 錨點最佳化演算法。
- **計算環境**：本地 Python 後台子系統與認知分群模組（**0 API 配額消耗**）。
- **安全架構**：
  - 順序相依中繼位置模擬盤面。
  - 寫回前 1-unit 遠端一致性快照校驗 (`_verify_snapshot`)。
  - 寫入時每筆 0.5 秒安全節流與自動 409 / 5xx 重試機制。
  - 離線演練 (Dry-run) 替身模擬。

---

## 📈 配額與統計分析

| 項目 | 數值 | 說明 |
|:---|:---:|:---|
| **清單總項目數** | 192 支 | 全部正常可播放影片（無私人/已刪除影片） |
| **辨識藝人群組數** | 105 組 | 涵蓋 EGOIST (11首)、米津玄師 (9首)、Ado (6首)、tuki. (7首)、Eve (7首)、YOASOBI (5首)、LiSA (5首) 等 |
| **LIS 錨點影片數** | **118 支** | **保持原位固定（消耗 0 API units）** |
| **實際執行移動數** | **74 筆** | 僅移動必要影片 |
| **總消耗 API 配額** | **3,752 units** | 包含 1-unit 校驗 + 移動寫入（遠低於單日上限 10,000 units） |
| **較傳統全部重排節省** | **5,700 units** | **節省約 61% 的 API 配額消耗** |
| **最終遠端 Diff** | **0 筆** | 重新自 YouTube 讀取比對，192 首完全就位 |

---

## 🔄 執行歷程

```mermaid
graph TD
    A[Phase 1: Fetch 抓取 192 支影片快照] --> B[Phase 2: 本地多藝人別名統一與 LIS 錨點計算]
    B --> C[Phase 3: 輸出差異預覽表並等待確認]
    C --> D[離線 Dryrun 演練 100% 通過]
    D --> E[Phase 4: 循序寫回 YouTube API]
    E --> F[最終遠端 Fetch 驗證: 0 Diff 完工]
```

1. **Phase 1 抓取**：透過 YouTube Data API v3 取得 192 支影片之 `playlistItems` 與 `videos.list` 完整 metadata（指紋：`ca8f68e72cf3d70c`）。
2. **Phase 2 本地計算**：執行多維度藝人名稱正規化，建立 105 組藝人群，群內依觀看次數排序。LIS 錨點演算法計算出 118 支錨點與 74 筆移動。
3. **Phase 3 預覽與確認**：聊天室呈現差異預覽表與配額警告，取得使用者確認回覆「OK」。
4. **離線演練 (Dry-run)**：在離線替身沙盒中模擬 74 筆順序相依操作，確認目標順序 100% 正確。
5. **Phase 4 寫回**：執行 `yt_tool update` 循序寫入 YouTube API，遇到 YouTube 後台短暫 409 衝突時自動安全重試並全數寫入成功。
6. **最終驗證**：重新執行 `fetch --refresh` 與 `diff`，確認 `changes_count = 0`，全數完工。

---

## 📋 執行前 ➔ 執行後 完整位置比對總表

| 最終順序 | 影片標題 | 頻道 / 歌手 | 觀看次數 | 位置異動 (原位置 → 最終) | 狀態 |
|:---:|:---|:---|:---:|:---:|:---:|
| **1** | 晩餐歌 - Bansanka | `tuki. - Topic` | 104,429,302 | 7 → **1** | 🔄 移動 |
| **2** | ひゅるりらぱっぱ - HYURURIRAPAPPA | `tuki. - Topic` | 12,100,133 | 3 → **2** | 🔄 移動 |
| **3** | 一輪花 - Ichirinka | `tuki. - Topic` | 10,747,950 | 6 → **3** | 🔄 移動 |
| **4** | 愛の賞味期限 - Love Expiration Date | `tuki. - Topic` | 4,928,167 | 5 → **4** | 🔄 移動 |
| **5** | 月面着陸計画 - Moon Landing Plan | `tuki. - Topic` | 4,066,346 | 2 → **5** | 🔄 移動 |
| **6** | 地獄恋文 - Inferno Love Letter | `tuki. - Topic` | 2,668,916 | 1 (不變) | ⚓ 錨點 (固定) |
| **7** | tuki. 『ひゅるりらぱっぱ』1st Live at NIPPON BUDOKAN | `tuki.(17)` | 2,348,765 | 4 (不變) | ⚓ 錨點 (固定) |
| **8** | アイナ・ジ・エンド / 革命道中 - On The Way [Official Music Video]（TVアニメ『ダンダダン』第2期オープニングテーマ） | `アイナ・ジ・エンド Official` | 63,679,456 | 8 (不變) | ⚓ 錨點 (固定) |
| **9** | 音羽-otoha-「no man’s world」Music Video / TVアニメ「Dr.STONE SCIENCE FUTURE」 第4期最終シーズン第2クール エンディングテーマ | `音羽-otoha-` | 1,477,121 | 9 (不變) | ⚓ 錨點 (固定) |
| **10** | Asu no Yozora Shoukaihan (10th Anniversary mix) | `Yuaru - Topic` | 2,222,134 | 10 (不變) | ⚓ 錨點 (固定) |
| **11** | Show | `Ado - Topic` | 88,569,758 | 12 → **11** | 🔄 移動 |
| **12** | Backlight (UTA from ONE PIECE FILM RED) | `Ado - Topic` | 70,591,266 | 170 → **12** | 🔄 移動 |
| **13** | I’m invincible (UTA from ONE PIECE FILM RED) | `Ado - Topic` | 55,017,283 | 14 → **13** | 🔄 移動 |
| **14** | unravel | `Ado - Topic` | 26,220,190 | 11 (不變) | ⚓ 錨點 (固定) |
| **15** | Where the Wind Blows | `Ado - Topic` | 23,526,096 | 172 → **15** | 🔄 移動 |
| **16** | Senbonzakura | `Ado - Topic` | 22,348,797 | 13 (不變) | ⚓ 錨點 (固定) |
| **17** | AliA / かくれんぼ【Official Music Video】 | `AliA OFFICIAL` | 109,941,828 | 15 (不變) | ⚓ 錨點 (固定) |
| **18** | 菅田将暉 『まちがいさがし』 | `菅田将暉 Official Channel` | 225,646,353 | 16 (不變) | ⚓ 錨點 (固定) |
| **19** | PAINT | `I Don't Like Mondays. - Topic` | 5,352,779 | 17 (不變) | ⚓ 錨點 (固定) |
| **20** | The Peak | `SEKAI NO OWARI - Topic` | 23,758,515 | 18 (不變) | ⚓ 錨點 (固定) |
| **21** | departure! | `Masatoshi Ono - Topic` | 10,617,409 | 19 (不變) | ⚓ 錨點 (固定) |
| **22** | 残酷な天使のテーゼ | `Yoko Takahashi - Topic` | 34,920,238 | 20 (不變) | ⚓ 錨點 (固定) |
| **23** | LISTEN TO THE STEREO!! | `GOING UNDER GROUND - Topic` | 3,791,224 | 21 (不變) | ⚓ 錨點 (固定) |
| **24** | 夜に駆ける | `YOASOBI - Topic` | 248,418,449 | 24 → **24** | 🔄 移動 |
| **25** | 群青 | `YOASOBI - Topic` | 136,278,340 | 23 → **25** | 🔄 移動 |
| **26** | 祝福 | `YOASOBI - Topic` | 126,105,651 | 22 (不變) | ⚓ 錨點 (固定) |
| **27** | 勇者 | `YOASOBI - Topic` | 67,468,439 | 164 → **27** | 🔄 移動 |
| **28** | 怪物 | `YOASOBI - Topic` | 63,042,760 | 25 (不變) | ⚓ 錨點 (固定) |
| **29** | Official髭男dism - Pretender［Official Video］ | `Official髭男dism` | 649,807,415 | 26 (不變) | ⚓ 錨點 (固定) |
| **30** | OFFICIAL HIGE DANDISM - MIXED NUTS [ Official Video ] | `Official髭男dism` | 149,556,383 | 27 (不變) | ⚓ 錨點 (固定) |
| **31** | FUNKIST [ Snow fairy ] OFFICIAL MUSIC VIDEO | `FUNKIST official` | 2,839,035 | 28 (不變) | ⚓ 錨點 (固定) |
| **32** | Nightcore - 夢と葉桜 // Yume To Hazakura「 ヲタみん // Wotamin Cover 」Original song by: 青木月光 // Aoki Gekkoh | `AnS Nightcore` | 103,395,865 | 29 (不變) | ⚓ 錨點 (固定) |
| **33** | Ignite | `Eir Aoi - Topic` | 11,475,474 | 119 → **33** | 🔄 移動 |
| **34** | Innocence | `Eir Aoi - Topic` | 6,448,618 | 121 → **34** | 🔄 移動 |
| **35** | 藍井エイル「INNOCENCE」Music Video（TVアニメ『ソードアート・オンライン』フェアリィ・ダンス編OPテーマ） | `藍井エイル Official YouTube Channel` | 5,853,958 | 30 (不變) | ⚓ 錨點 (固定) |

---
*報告產生於：2026-09-01 11:54:18*
