# 測試樣本格式（`yt-playlist-sample/v1`）

`User Testing/` 底下的樣本檔一律使用這個格式。

定這個格式的原因很簡單：樣本的 `items` **就是** `EnrichedPlaylistItem`，與
`yt_tool fetch --out` 的產物同一種東西。任何工具要吃樣本都不需要轉接層。

在此之前，Task 3 的第一版樣本用的是自訂欄位（`index` / `channel` / `views` /
`published_date`），每次要餵給 `optimizer` 都得先寫一段對照程式碼，而那段程式碼
本身沒有被測試涵蓋 —— 轉接層是缺陷的溫床，不是便利。

---

## 檔案結構

```jsonc
{
  "schema": "yt-playlist-sample/v1",

  "source": {
    "playlist_id":   "PLLpKeZeMXlNY",
    "playlist_name": "skills Test",
    "playlist_url":  "https://www.youtube.com/playlist?list=PLLpKeZeMXlNY",
    "fetched_at":    "2026-09-01T19:55:04+08:00"
  },

  // 只有「由其他樣本推導出來」的檔案才有這一段。原始抓取的樣本沒有。
  "derivation": {
    "base":   "UserTest_Task3-1",
    "method": "random.Random(seed).shuffle",
    "seed":   42
  },

  // 這份樣本裡「不是真實資料」的欄位。見下方〈合成欄位〉。
  "synthetic_fields": ["added_at", "comment_count", "tags"],

  "item_count": 192,

  // 這裡開始就是純粹的 EnrichedPlaylistItem 陣列，可直接餵給任何工具。
  "items": [
    {
      "playlist_item_id": "UExMcEtlWmVNWGxOWS4yODlGNEE0NkRGMEEzMEQy",
      "video_id":         "PWIkRLwbAsw",
      "position":         0,
      "added_at":         "2026-09-01T19:55:04+08:00",
      "playlist_id":      "PLLpKeZeMXlNY",
      "title":            "K-391, Hoaprox, Nick Strand - Die Alone (Visualizer)",
      "channel_title":    "K391VEVO",
      "published_at":     "2024-03-22T00:00:00+00:00",
      "duration_seconds": 194,
      "view_count":       5044856,
      "like_count":       60990,
      "comment_count":    0,
      "tags":             [],
      "privacy_status":   "public",
      "is_available":     true,
      "metadata_available": true
    }
  ]
}
```

取出可直接使用的清單只要一行：

```python
items = [EnrichedPlaylistItem(**raw) for raw in json.load(f)["items"]]
```

---

## 欄位規則

| 欄位 | 規則 |
|---|---|
| `schema` | 固定字串，變更格式時遞增版本 |
| `source.playlist_id` | 必填。樣本的來源清單 |
| `source.fetched_at` | 必填。抓取時間，帶時區 |
| `derivation` | 推導樣本必填、原始樣本必須不存在 |
| `synthetic_fields` | 必填（沒有合成欄位時為 `[]`） |
| `item_count` | 必須等於 `len(items)` |
| `items[].position` | **0-based**，且必須等於陣列索引 |
| `items[].playlist_item_id` | 必須唯一。這是清單項目的主鍵 |
| `items[].video_id` | **允許重複** —— 同一支影片可以被加入清單多次 |

### `position` 為什麼是 0-based

`EnrichedPlaylistItem.position` 與 YouTube API 的 `snippet.position` 一致，都是
0-based。舊格式的 `index` 是 1-based，轉換時必須 `-1`，不要沿用。

### `video_id` 為什麼允許重複

`UserTest_Task3-1` 有 192 個項目但只有 188 支不重複影片 —— 4 支影片各被加入兩次。
任何以 `video_id` 當主鍵的程式碼都會在這裡把兩筆收斂成一筆，導致目標順序長度不足、
寫回位置整段錯位。**主鍵永遠是 `playlist_item_id`。**

---

## 合成欄位

樣本的來源不一定帶有全部欄位。`.md` 格式的清冊就沒有記錄 `added_at`、
`comment_count` 與 `tags`。這些欄位仍必須存在（`EnrichedPlaylistItem` 要求），
但要在 `synthetic_fields` 中列出，**表示不可用於任何斷言**。

填值規則刻意選擇「明顯退化」而非「看起來合理」：

| 欄位 | 合成值 | 為什麼這樣填 |
|---|---|---|
| `added_at` | 全部等於 `source.fetched_at`（常數） | 依 `added_at` 排序會變成完全無作用（穩定排序保留原順序），一眼就看得出沒有資訊，不會產生「看起來對但其實錯」的排序結果 |
| `comment_count` | `0` | 同上，不參與任何排序判準 |
| `tags` | `[]` | 空陣列，不影響分群 |

反過來說，**不要**把 `added_at` 填成遞增的假時間戳。那會讓依加入時間排序的測試
產生一個貌似合理的結果，掩蓋掉「這份樣本根本沒有這個資料」的事實。

由 `yt_tool fetch` 直接產生的樣本沒有這個問題，`synthetic_fields` 應為 `[]`。

---

## 建立與驗證

```bash
# 由舊格式重建 Task 3 的五份樣本（唯讀；要寫檔需 YT_DEV_SAMPLE=1）
YT_DEV_SAMPLE=1 python tests/dev/build_task3_samples.py

# 驗證樣本符合本文件的所有規則
python tests/dev/build_task3_samples.py --validate

# Task 3 專屬的統計驗證（Jaccard / Kendall / 種子可重現）
python tests/verify_task3_randomization.py
```

新的公開清單樣本直接用 `fetch` 產生即可，它輸出的就是 `items` 的內容：

```bash
python -m scripts.yt_tool fetch <url> --out data/current.json --refresh
```

---

## 衍生產物的方向

```
.md 清冊  ──(一次性人工/腳本轉換)──►  樣本 JSON  ──(衍生)──►  人類可讀報告
                                        ▲
                                   單一事實來源
```

方向只有一個。**不要**讓任何流程從 Markdown 反向解析回 JSON —— Task 3 第一版就是
這樣做的，來源 `.md` 一度被刪除後整條鏈就無法重跑。人類要看的東西一律由 JSON 生成。
