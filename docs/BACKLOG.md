# 待辦

已經查證過、但還沒動工的事項。每一條都附上「為什麼還沒做」與「動工前要先確認什麼」，
避免下一次重新調查一遍。

---

## 1. ytmusicapi 作為辨識訊號層與免 OAuth 寫入路徑

**狀態**：讀取端已驗證，寫入端**卡住**（無法取得瀏覽器 cookie）。

### 背景

兩個獨立的問題指向同一個解法：

1. **推廣瓶頸**：非資訊背景的受測者無法自行申請 Google Cloud OAuth 憑證，卡在 Phase 0
   就無法開始測試。
2. **辨識品質**：現行三層 pipeline（標題 regex → 頻道名 → 模糊比對）本質上是在猜，
   真實清單上會把歌名當藝人（「晩餐歌」→ `晩餐歌`）、把整串標題當藝人
   （`egoist『bang!!!』(tvアニメ「ビルディバイド`），跨語言別名也不會合併
   （`kenshi yonezu` / `米津玄師` 各自成組）。

ytmusicapi 的 `browser` 認證只需從瀏覽器 devtools 複製 headers（不需要 Google Cloud
專案、不需要同意畫面、無配額），且 `get_playlist()` 直接回傳結構化的 `artists` /
`album` / `setVideoId`，`edit_playlist(moveItem=...)` 可直接重排。

### 已查證（2026-09-01，`tests/dev/ytm_probe.py`，2 份樣本）

| 結論 | 證據 |
|---|---|
| ✅ `artists` 修掉現行 pipeline 的猜測失敗 | 日文歌單 188 首中 34 筆分歧，幾乎全是 ytmusicapi 正確 |
| ✅ `album` 可直接取代 LLM 手工逆推專輯歸屬 | Task 2 報告手工推出的 iv 8 首 / V 8 首 / 2V-ALK 5 首，API 給的是 8 / 7 / 6 |
| ❌ artist 是「發行掛名」不是「藝人本人」 | 澤野弘之清單被切成 `SawanoHiroyuki[nZk]:mizuki`、`:Tielle`、`:Yosh` 等 19 組；現行 resolver 只切 5 組，反而更好 |
| ❌ 音樂目錄看不到全部項目 | 192→188、83→82，少的是目錄裡沒有對應曲目的一般 YouTube 影片 |

一致率在兩份樣本上是 82% 與 26%，**方向完全相反**。

### 結論：不是取代品，是訊號層

- 以 API 快照為準（它才有完整的 `playlist_item_id` 與全部項目），用 `videoId`
  把 ytmusicapi 的 `artists` / `album` **併回去當額外訊號**。
- 中間必須加正規化：去掉冒號後綴 → casefold → 走現有 `artist_aliases.json`。
  實測去冒號後綴可讓澤野弘之那份從 19 組收斂到 9 組。
- 目錄裡沒有的項目**退回現行 pipeline**，不可讓它們從目標順序中消失。

### 卡住的地方

寫入端需要 `setVideoId`，而無認證時是 0/188。認證要跑：

```bash
ytmusicapi browser
```

（**不是** `python -m ytmusicapi` —— 該套件沒有 `__main__.py`。）

它會要求在已登入的 music.youtube.com 開 devtools，找一個 POST `/browse` 請求並複製
request headers。目前卡在抓不到 cookie 資訊。

> ⚠️ 產出的 `browser.json` 裡是瀏覽器 session cookie，**等同帳號憑證**，已加入
> `.gitignore`。它的有效期約 2 年（除非登出），比 OAuth 的 token 更敏感。

### 解除阻塞後要先確認兩件事

1. 一般 YouTube 清單（`PL...`，非 YouTube Music 原生清單）認證後拿不拿得到 `setVideoId`。
2. 單筆 `edit_playlist(moveItem=(from, to))` 是否真的改變順序、語意是否為「移到目標之前」。
   拿 83 首那份試一筆即可（可再搬回來）。

### 風險

非官方 API，會隨 YouTube 改版而壞；正式路徑仍應保留 YouTube Data API v3。

---

## 2. 群內排序的「無意義搬動」

**狀態**：根因已定位，尚未修。

2026-09-01 的真實測試中，使用者只要求「把同歌手的影片整齊排序」，但工具額外做了群內
觀看數排序 —— 清單開頭 7 首 tuki. **本來就已經連在一起**，卻為了群內排序多花了 5 筆
移動（250 units）。使用者的原話是「無意義搬動」。

根因有兩層，都不在演算法（LIS 移動數確實是最小的）：

1. `docs/agent/AGENT_SOP.md` 路徑 A 的推薦指令範例**直接寫死了**
   `--within-group-sort viewCount:desc`，Agent 照抄就等於自動加上使用者沒要求的排序。
2. `docs/agent/clarify_scenarios.json` 裡**早就有**這題的追問模板（`missing_sort_field`，
   第一個選項就是「維持原順序」），但它只掛在路徑 C，觸發條件是「認知引擎解析不出欄位」。
   Agent 自己把 view_count 補進 intent 字串後，斷層在進引擎前就被填掉，閘門永遠不會開。

修法：

- Phase 0 增加一步「記錄使用者原話指定了哪些維度」，沒指定群內排序就強制查表發問；
  同時把 SOP 範例的 `--within-group-sort` 拿掉或標註為「僅在使用者明確指定時加」。
- 預覽表把移動拆成「為了聚集群組」與「只是群內重排」兩桶，讓使用者能單獨取消後者。

---

## 3. 測試產物沒有留存，證據會被下一次執行蓋掉

**狀態**：已確認，尚未修。

`data/` 是單一可變槽位。Task 2 在 11:46 覆蓋了 `data/changes.json` 與 `data/current.json`，
導致 Task 1 真正執行的變更檔已不存在，事後無法稽核（留下的 `changes_optimized.json`
是更早的廢棄版本，模擬後與真實結果只對上 37/192）。

同一次測試的報告中，配額數字也對不上：74 筆移動應為 `1 + 74×50 = 3,701`，報告寫 3,752；
節省量應為 `9,600 − 3,700 = 5,900`，報告寫 5,700。

同樣的情形也出現在 Task 3：`UserTest_Task3-1.md` 是抓取後的原始格式，
`scripts/convert_and_verify_task3.py` 把它轉成 `UserTest_Task3-1.json`、
`tests/verify_json_task3.py` 再拿 JSON 回頭比對 `.md`。`.md` 在轉換後被移除，
這兩支腳本因此**無法再重跑**（不是程式壞了，是輸入被消化掉了）。

修法：每次執行落一個帶時間戳的目錄，**原始格式、變更檔、快照與報告放在一起**，
報告的統計數字由變更檔計算而非另行填寫。轉換型腳本的來源檔案應保留而非刪除。
