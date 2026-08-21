# 端對端測試套件 (E2E Suite)

`tests/e2e/` 驗證的不是單一函式，而是 **AI Agent 依照 `docs/agent/AGENT_SOP.md` 實際會跑的整條流程**：
Phase 0 憑證握手 → Phase 1 fetch → Phase 2 本地計算（optimize / diff / 認知引擎）→
Phase 3 預覽資料 → Phase 4 寫回、重試、中斷續傳。

```bash
python tests/e2e/run_e2e.py                      # 全部 16 個情境
python tests/e2e/run_e2e.py -k update,resume     # 只跑指定情境（逗號分隔、子字串比對）
python tests/e2e/run_e2e.py --keep               # 保留暫存工作區以便檢查產出檔案
```

離線執行，**不需要真實 Google 憑證、不消耗任何 API 配額、不會碰到任何真實播放清單**。

> 情境之間是一條流水線（例如第 5 個情境要用第 3 個情境 fetch 下來的檔案），
> 因此 `-k` 只適合用來重現失敗時的除錯；正式驗收請跑完整套件。

---

## 測試方法

| 設計決策 | 原因 |
|:---|:---|
| 每一步都是**真實子行程**（`python -m scripts.yt_tool …`、`node dist/cli.js …`） | 測到的是使用者／Agent 真正會打的指令，包含 argparse、stdout JSON 協定、離開碼 |
| 測試檔案**完全不 import `scripts/`** | 預期順序、配額、replay 結果全部在 `run_e2e.py` 內獨立算出，實作有 bug 時不會「自己驗自己」而漏掉 |
| 只假造**網路邊界**（`fake_youtube.py`） | `youtube_api.py` 的分頁、批次、錯誤分類、重試與續傳邏輯全部照跑 |
| `sitecustomize.py` 在直譯器啟動時注入 | 這是唯一早於 `from googleapiclient.discovery import build` 的時機 |
| 假 API 以 JSON 檔保存狀態 | 一個情境會開十幾個子行程，播放清單順序、配額、故障腳本必須跨行程延續 |
| `$YT_SKILL_HOME`、`$YT_E2E_STATE` 指向暫存目錄 | 絕不會覆蓋開發者本機真正的 OAuth 憑證 |

`fake_youtube.py` 忠實重現的 API 語意：

- `playlistItems.list`：每頁 50 筆 + `nextPageToken`，1 unit
- `videos.list`：單次最多 50 個 id，且**不回傳**私人影片（與真實 API 一致），1 unit
- `playlistItems.update`：**先移除再插入**，會讓其後所有項目重新編號，50 units
- 失敗情境丟出真正的 `googleapiclient.errors.HttpError`，帶著與 Google 相同的 JSON 錯誤內容，
  因此 `classify_http_error` 是真的被執行到

---

## 通過條件

全部 16 個情境必須通過，離開碼為 0。核心判準：

**正確性（最重要）**

1. `replay(current, changes) == target`——以 API 合約（remove + insert）獨立重放變更集，必須精確得到目標順序。
2. 真的寫回之後，**假 API 上的清單順序 == 目標順序**，且再 `fetch` 一次確認一致。
3. 目標順序必須是原清單的排列（不增不減），私人／已刪除影片仍留在原索引。
4. 分群結果：同一群連續、群組順序為首次出現順序、群內依指定欄位遞減。

**配額**

5. `optimize` / `diff` 消耗 **0 units**（並且在 `googleapiclient` 完全無法 import 的環境下仍可執行）。
6. `update` 消耗 `1 + 50 × 移動筆數`，且與假 API 實際記帳一致。
7. 無 pinned 項目時，移動筆數等於理論下限 `N − LIS`（在 `run_e2e.py` 內獨立計算）。
8. `quota_saved_vs_naive` 與獨立算出的 naive 基準一致。

**安全性（寧可不做，也不能做錯）**

9. 缺憑證、錯誤憑證、舊版 (v1) 變更檔、他人播放清單的變更檔、被手動改過 `execution_order` 的檔案、
   不支援的版本號——全部拒絕，且**一個 API unit 都不花**。
10. 遠端清單被人動過 → `STALE_SNAPSHOT`，只花 1 unit，不寫入任何一筆。
11. 已套用完成的變更集再跑一次 → 同樣被 `STALE_SNAPSHOT` 擋下（不會重複套用打亂清單）。
12. 中途致命錯誤（`quotaExceeded`）→ 立刻停止、寫入續傳檔；再次執行同一指令時只補完剩下的筆數並抵達目標順序。
13. 可重試錯誤（503）→ 自動退避重試後成功。

**介面契約**

14. 所有錯誤都是結構化 JSON（`code` 欄位），不是 traceback。
15. `.claude` / `.gemini` / `AGENTS.md` 三個 Agent 進入點與 SOP 文件都存在。
16. 預覽表所需欄位（`old_position` / `final_position` / `title`）與實際索引相符；
    且 `new_position != final_position` 的列確實存在（證明位置漂移模型有在運作）。

---

## 情境一覽

| # | 階段 | 情境 |
|:--|:--|:--|
| 1 | Phase 0 | Agent 進入點與 CLI 介面 |
| 2 | Phase 0 | 憑證握手：`CREDENTIALS_MISSING` → `FILE_NOT_FOUND` → `INVALID_JSON` → 成功 |
| 3 | Phase 1 | fetch：120 筆／3 頁分頁、3 批 metadata、私人影片保留 |
| 4 | Phase 1 | 快取命中 vs `--refresh`、token 重用不再要求授權 |
| 5 | Phase 2A | optimize：分群、群內排序、最小變更集、0 配額 |
| 6 | Phase 2A | optimize 在無 Google 套件、無憑證環境下仍可執行且結果一致 |
| 7 | Phase 3 | 預覽表資料與真實索引相符 |
| 8 | Phase 4 | 寫回抵達目標順序、快取失效、續傳檔清除 |
| 9 | Phase 4 | 已用完的變更集不可重複套用 |
| 10 | Phase 4 | 遠端漂移在寫入前被擋下 |
| 11 | Phase 4 | 503 重試、配額耗盡中止、續傳補完 |
| 12 | Phase 2C | TypeScript 認知引擎 → diff → update 全鏈路 |
| 13 | Phase 2B | 非排列的目標順序被拒絕 |
| 14 | Phase 4 | 變更集完整性防護 |
| 15 | Phase 2A | 已排序清單為真正的 no-op |
| 16 | 全域 | 參數錯誤回傳結構化錯誤碼、接受播放清單網址 |

---

## 與既有測試的關係

`tests/test_optimizer.py`、`test_reorder_property.py`、`test_update_flow.py` 是**行程內單元／屬性測試**
（窮舉排列的最小性證明、寫回安全性）。本套件不取代它們，而是補上它們涵蓋不到的部分：
CLI 契約、跨行程狀態（快取／續傳檔）、OAuth 與憑證安裝、TypeScript 與 Python 兩個子系統的接合。
