# 首次使用強制憑證引導 — 實作計畫（更新版 v2）

## 核心設計改動

與上一版計畫不同，本版將憑證偵測責任**下沉至 `yt_tool.py` 工具層級**，而非依賴 `SKILL.md` 的 Agent SOP 主動觸發。

| 項目 | 舊版（SOP 層級）| 新版（工具層級）|
|------|----------------|----------------|
| 偵測時機 | Agent 走 Phase 0 時才偵測 | **任何指令執行前自動偵測** |
| 偵測位置 | SKILL.md + yt_tool.py 兩處 | 只在 yt_tool.py 一處 |
| 觸發方式 | Agent 判斷 → 呼叫 setup-credentials | **工具自動觸發**，Agent 不需要額外邏輯 |
| 首次體驗 | 需多回合對話才進入設定 | Agent 呼叫任何指令就自動彈窗 |

---

## 目標

使用者第一次觸發 Skill（不管是哪個指令），`yt_tool.py` 就自動在執行前偵測憑證，若不存在則**立即彈出 `OpenFileDialog`**。使用者在系統原生視窗中選取 JSON 後，系統自動驗證並複製到安全位置，再繼續執行原本的指令。整個過程對 Agent 透明，Agent 不需要特殊的前置判斷。

---

## 設計核心：`ensure_credentials()` 守衛函式

### 執行流程圖

```
任何指令（fetch / diff / update）被 Agent 呼叫
        ↓
   main() 解析參數
        ↓
ensure_credentials() 自動執行
        ↓
   憑證是否存在？
   ├─ 是 → 直接繼續執行原指令
   └─ 否 → 彈出 OpenFileDialog（只顯示 .json 檔案）
              ↓
         使用者選取後
         ├─ 取消          → stdout: USER_CANCELLED → sys.exit(1)
         ├─ 選到非 OAuth JSON → stdout: INVALID_JSON  → sys.exit(1)
         └─ 合法 OAuth JSON → 自動建立資料夾 → 複製
                              → 成功後繼續執行原指令
                              → 複製失敗 → stdout: COPY_FAILED → sys.exit(1)
```

### 偽程式碼

```python
def ensure_credentials() -> None:
    if DEFAULT_CREDENTIALS_PATH.is_file():
        return  # 已存在，直接透過

    # 強制彈出原生檔案對話框
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.lift()
    root.attributes('-topmost', True)

    path_str = filedialog.askopenfilename(
        title="請選擇您從 Google Cloud Console 下載的 OAuth 憑證 JSON 檔案",
        filetypes=[("JSON 憑證檔案", "*.json")]   # 只顯示 .json
    )
    root.destroy()

    if not path_str:
        print(json.dumps({"status": "error", "code": "USER_CANCELLED"}))
        sys.exit(1)

    # 驗證是否為合法 OAuth JSON
    try:
        data = json.loads(Path(path_str).read_text(encoding='utf-8'))
        if 'installed' not in data and 'web' not in data:
            raise ValueError
    except Exception:
        print(json.dumps({"status": "error", "code": "INVALID_JSON",
                          "message": "所選檔案不是合法的 Google OAuth 2.0 憑證。"}))
        sys.exit(1)

    # 複製到安全位置
    try:
        DEFAULT_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path_str, DEFAULT_CREDENTIALS_PATH)
    except Exception as exc:
        print(json.dumps({"status": "error", "code": "COPY_FAILED", "message": str(exc)}))
        sys.exit(1)

    # 成功後不輸出額外訊息，直接繼續執行原指令
```

```python
def main():
    parser = argparse.ArgumentParser(...)
    # ... 現有參數設定 ...
    args = parser.parse_args()

    ensure_credentials()   # ← 一進入就執行，任何指令前都會被守衛
    args.func(args)
```

---

## Proposed Changes

### 1. 修改後台工具
#### [MODIFY] [yt_tool.py](file:///d:/Antigravity%20Code/YouTube%20Playlist%20skill/scripts/yt_tool.py)

- 在檔案頂端新增 `import shutil`。
- 新增 `ensure_credentials()` 函式（如上偽程式碼）。
- 修改 `main()` 函式：在 `args.func(args)` 之前插入 `ensure_credentials()` 呼叫。
- 移除獨立的 `setup-credentials` 子指令（整合進守衛函式，不再需要）。

### 2. 簡化 Agent SOP
#### [MODIFY] [SKILL.md](file:///d:/Antigravity%20Code/YouTube%20Playlist%20skill/.gemini/skills/yt-playlist-manager/SKILL.md)

Phase 0 大幅簡化。Agent **不再**需要主動偵測憑證或走多步驟引導，改為只需要：

```
Phase 0 的憑證處理（新）：

Agent 直接走 Phase 1 呼叫 fetch。

若 fetch 回傳錯誤碼，Agent 依以下規則應對：

- code "USER_CANCELLED"
    → 告知使用者已取消，詢問是否要重試。
      若同意，再次呼叫 fetch（會再次彈出視窗）。

- code "INVALID_JSON"
    → 提示所選 JSON 不是 Google OAuth 憑證，
      說明應在 Google Cloud Console 建立「桌面應用程式」類型的 OAuth 2.0 Client ID，
      再次呼叫 fetch 重新選取。

- code "COPY_FAILED"
    → 提示可能是系統權限問題，建議以管理員身份執行。
```

> [!NOTE]
> 若工具成功彈窗且使用者選取合法憑證，工具不會回傳特殊設定訊息，
> 而是直接繼續執行 fetch 的正常結果。Agent 無需特殊判斷。

---

## 錯誤碼規範（精簡版）

| Code | 情境 | Agent 應對 |
|------|------|-----------|
| `USER_CANCELLED` | 使用者關閉對話框 | 詢問是否重試，重試則再次呼叫相同指令 |
| `INVALID_JSON` | 所選 JSON 非合法 OAuth 憑證 | 提示憑證類型，提供重試 |
| `COPY_FAILED` | 複製失敗（通常是權限問題）| 建議以管理員執行 |

---

## 使用者完整體驗流程（實作後）

```
使用者說：「幫我整理播放清單 PLxxxxx」

Agent 直接呼叫：python -m scripts.yt_tool fetch PLxxxxx --out data/current.json

【自動】工具偵測到沒有憑證 → 彈出系統原生檔案選擇視窗
         標題：「請選擇您的 Google OAuth 憑證 JSON 檔案」
         只顯示 .json 檔案

使用者點選憑證 JSON → 確定

【自動】工具驗證 + 複製憑證到安全位置 → 繼續執行 fetch

fetch 成功，Agent 收到正常 JSON 結果 → 繼續 Phase 2 ~ 4
```

---

## Verification Plan

1. **首次使用測試**：無憑證狀態下呼叫 `fetch`，確認對話框第一時間彈出。
2. **同一執行過程中設定後繼續執行**：使用者選取憑證後，`fetch` 應繼續完成，不中斷。
3. **第二次使用測試**：憑證已存在，確認對話框**不會**再次出現。
4. **錯誤場景測試**：
   - 按「取消」→ 工具回傳 `USER_CANCELLED`，程式結束。
   - 選取一般 JSON（非 OAuth）→ 回傳 `INVALID_JSON`，程式結束。
