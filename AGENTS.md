# AGENTS.md

給任何在此工作區運作的 AI Agent（Codex、GitHub Copilot Workspace、Antigravity CLI 等）的入口說明。
Claude Code 會另外自動載入 `.claude/skills/yt-playlist-manager/SKILL.md`；Gemini CLI 會載入 `.gemini/skills/yt-playlist-manager/SKILL.md`。三者指向同一份流程。

> **兩份 `SKILL.md` 必須逐位元組相同**，CI 的 `skill-parity` job 會擋下分岔。
> 它們只是「四條規則 + 指令速查 + 指向 `docs/agent/AGENT_SOP.md`」的入口，
> 流程細節一律寫在 `AGENT_SOP.md`，不要複製進 `SKILL.md`——上一次分岔就是這樣來的。

## 這個專案是什麼

一個 YouTube／Spotify 播放清單管理 Agent-Skill：Agent 當大腦（解讀需求、決定順序），`scripts/`（Python）負責 API 讀寫與呼叫數最少的重排計算。

## 使用者要求整理播放清單時

**先讀 `docs/agent/AGENT_SOP.md`**，並嚴格照它的五個 Phase 走。四條不可違反的規則：

1. 呼叫 `update` 前必須取得使用者明確同意：先開檢查頁（`review start` → `review wait`），再在聊天室用一句話報告成本並等回覆。不要貼預覽表。
2. 變更檔是順序相依的：必須依 `execution_order` 逐筆執行，不可編輯、重排、拆分或跳過。
3. 不要給使用者看 `new_position`／`range_start`／`insert_before`（送 API 的中繼位置）；提到位置一律用 `final_position`。
4. 收到 `STALE_SNAPSHOT` 就重新 `fetch --refresh` 後重算，不要用 `--skip-verify` 硬闖。

## 在此專案中開發時

```bash
# Python
pip install -r requirements.txt
python tests/test_optimizer.py          # 演算法與辨識單元測試
python tests/test_reorder_property.py   # 重排正確性窮舉驗證（必跑）
python tests/test_update_flow.py        # 寫回安全性
python tests/test_spotify_api.py       # Spotify 用戶端（PKCE、對應、重試策略）
python tests/test_review_server.py     # 拖拉檢查頁（token、驗證、保存、取消）
python tests/e2e/run_e2e.py             # 離線端對端（假 YouTube API，0 配額）
```

改動 `scripts/optimizer.py` 的重排邏輯後，`tests/test_reorder_property.py` 是唯一能證明「變更集重播後等於目標順序」的把關測試，**必須通過**。它會窮舉 n=2..7 的全排列（5912 組）並驗證移動次數等於理論下界 `N − LIS`；Spotify 的區段搬移另外驗證呼叫數等於 `S − LIS(段)`。
