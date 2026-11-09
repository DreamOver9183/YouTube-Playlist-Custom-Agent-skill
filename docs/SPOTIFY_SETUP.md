# Spotify 設定：建立你自己的 Spotify App（約 5 分鐘）

要讓這個工具讀取與重排你的 Spotify 播放清單，需要一組 **Client ID**。它來自你在
Spotify 開發者後台建立的「App」。這是 Spotify 的規定，不是這個工具的限制：
每個人都要用自己的 App，而且 **App 擁有者必須是 Spotify Premium 會員**（2026-02 起）。

> 不需要 Client Secret，也不需要寫程式。整個過程只是在網頁上填一張表。
> Spotify 偶爾會改版後台的按鈕名稱；若下面某一步長得不一樣，照「意思」找最接近的按鈕即可，
> 或把畫面截圖給 Agent。

## 步驟

1. **開啟開發者後台**：用瀏覽器打開 <https://developer.spotify.com/dashboard>，
   用你平常聽歌的那個 Spotify 帳號登入。
2. **第一次使用時會要求同意開發者條款**：請自己閱讀並勾選同意。
   （Agent 不會、也不應該替你按同意。）
3. **建立 App**：按 **Create app**，然後填寫：

   | 欄位 | 填什麼 |
   |---|---|
   | App name | 隨意，例如 `My Playlist Sorter` |
   | App description | 隨意，例如 `Reorder my own playlists` |
   | Website | 留空即可 |
   | **Redirect URIs** | **`http://127.0.0.1:8765/callback`**（一個字都不能錯，填完記得按旁邊的 **Add**） |
   | Which API/SDKs are you planning to use? | 勾 **Web API** |

   最下方勾選同意 Developer Terms of Service 與 Design Guidelines，按 **Save**。

   > Redirect URI 必須是 `127.0.0.1`，**不能**寫成 `localhost`——Spotify 從 2025-04 起不接受 `localhost`。

4. **複製 Client ID**：進入剛建立的 App，按 **Settings**，在 **Basic Information** 裡找到
   **Client ID**（32 個英數字元），複製它。**不要**按「View client secret」，用不到。
5. **交給 Agent**：把 Client ID 貼到聊天室。Agent 會執行：

   ```bash
   python -m scripts.yt_tool setup_spotify <你的 Client ID>
   ```

6. **第一次登入**：Agent 第一次讀取你的清單時，瀏覽器會自動打開 Spotify 的授權頁，
   按 **同意（Agree）** 即可。之後約 6 個月內不用再登入（Spotify 規定授權最長 6 個月，
   到期時工具會自動再開一次授權頁）。

## 常見問題

- **INVALID_CLIENT：Invalid redirect URI**：第 3 步的 Redirect URI 打錯了，或忘了按 **Add**。
  回到 App 的 Settings 檢查是否完全等於 `http://127.0.0.1:8765/callback`。
- **授權頁說「這個 App 需要擁有者是 Premium」或無法登入**：App 擁有者的 Premium 過期時，
  App 會停止運作。
- **想讓家人也能用**：開發模式最多 5 位使用者，要在 App 的 **User Management** 加入對方的
  Spotify 帳號 email。對方也可以自己建一個 App，比較單純。
- **只能整理自己的清單**：Spotify 只允許讀取與修改你擁有（或協作）的播放清單；
  別人公開的清單無法重排。

## 這個 Client ID 存在哪裡

`setup_spotify` 會把它存到 `~/.gemini/skills/yt-playlist-manager/credentials/spotify.json`
（可用環境變數 `YT_SKILL_HOME` 改位置），檔案權限 600，不會進版控。
登入後的授權憑證存在同一個資料夾的 `spotify_token.json`，**等同帳號授權，請勿分享**。
