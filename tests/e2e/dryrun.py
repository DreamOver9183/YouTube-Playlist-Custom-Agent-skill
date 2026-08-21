"""
dryrun.py — 用真實快照演練寫回，不花任何 API 配額。

`update` 一旦送出就是每筆 50 units 的不可逆操作。這個工具把 `fetch` 抓下來的
真實快照灌進 E2E 的離線 API 替身，再執行**真正的** `yt_tool update` 指令，
於是整條寫回鏈路（快照驗證、順序相依的位置漂移、續傳檔、錯誤處理）都會照跑，
但一支影片都不會被移動、一個 unit 都不會被消耗。

用法：

    python tests/e2e/dryrun.py data/current.json data/changes.json \\
        --target data/new.json

    # 順便演練中途失敗與續傳（例如第 20 筆遇到配額耗盡）
    python tests/e2e/dryrun.py data/current.json data/changes.json \\
        --target data/new.json --fail-at 20

離開碼 0 代表這份變更集可以安全送出；非 0 代表真的送出去會出問題。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

from run_e2e import ids_of, replay  # noqa: E402 — needs HERE on sys.path

QUOTA_VERIFY = 1
QUOTA_MOVE = 50

#: 假憑證，只是為了讓 ensure_credentials 通過；替身不會真的用它換 token。
_FAKE_CLIENT_SECRET = {
    "installed": {
        "client_id": "dryrun.apps.googleusercontent.com",
        "client_secret": "dryrun",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_state(items: list[dict[str, Any]], playlist_id: str) -> dict[str, Any]:
    """把 fetch 產出的快照轉成替身的遠端狀態。"""
    return {
        "playlists": {
            playlist_id: [
                {
                    "playlist_item_id": item["playlist_item_id"],
                    "video_id": item["video_id"],
                    "channel_title": item.get("channel_title", ""),
                    "added_at": item.get("added_at", "2024-01-01T00:00:00Z"),
                    "privacy_status": item.get("privacy_status", "public"),
                }
                for item in items
            ]
        },
        "videos": {
            item["video_id"]: {
                "title": item.get("title", ""),
                "channel_title": item.get("channel_title", ""),
                "published_at": item.get("published_at") or "2024-01-01T00:00:00Z",
                "view_count": item.get("view_count", 0),
                "privacy_status": item.get("privacy_status", "public"),
            }
            for item in items
        },
        "quota_used": 0,
        "applied_updates": {},
        "calls": [],
        "faults": [],
        "oauth_consent_runs": 0,
    }


def run_update(workspace: Path, state_file: Path, playlist_id: str, changes: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(HERE), str(REPO)])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["YT_SKILL_HOME"] = str(workspace / "skill_home")
    env["YT_E2E_STATE"] = str(state_file)

    proc = subprocess.run(
        [sys.executable, "-m", "scripts.yt_tool", "update", playlist_id, str(changes)],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    payloads = [
        json.loads(line) for line in proc.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    if not payloads:
        raise SystemExit(f"演練無法解析輸出：\n{proc.stdout}\n{proc.stderr[-2000:]}")
    return payloads[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="離線演練一份變更集的寫回")
    parser.add_argument("current", help="fetch 產出的目前清單 JSON")
    parser.add_argument("changes", help="optimize / diff 產出的變更集 JSON")
    parser.add_argument("--target", help="目標順序 JSON（有給就會比對最終順序）")
    parser.add_argument("--fail-at", type=int, default=None, metavar="N",
                        help="在第 N 筆移動後注入配額耗盡，順便演練中斷與續傳")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    current = read_json(Path(args.current))
    change_set = read_json(Path(args.changes))
    playlist_id = change_set.get("playlist_id", "")
    changes = change_set.get("changes", [])
    current_ids = ids_of(current)

    if not playlist_id:
        print("變更集沒有 playlist_id，無法演練。")
        return 2

    snapshot_ids = change_set.get("source_snapshot", {}).get("item_ids", [])
    if snapshot_ids and snapshot_ids != current_ids:
        print("變更集的快照與這份 current.json 不一致——請用計算這份變更集時的快照。")
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="yt_dryrun_"))
    credentials = workspace / "skill_home" / "credentials"
    credentials.mkdir(parents=True)
    (credentials / "client_secret.json").write_text(
        json.dumps(_FAKE_CLIENT_SECRET), encoding="utf-8"
    )
    state_file = workspace / "remote_state.json"
    state_file.write_text(
        json.dumps(build_state(current, playlist_id), ensure_ascii=False), encoding="utf-8"
    )

    expected_quota = QUOTA_VERIFY + QUOTA_MOVE * len(changes)
    print("=" * 70)
    print(" 離線寫回演練（不消耗配額、不改動任何真實清單）")
    print("=" * 70)
    print(f" 播放清單   : {playlist_id}")
    print(f" 影片數     : {len(current)}")
    print(f" 移動筆數   : {len(changes)}")
    print(f" 真實成本   : {expected_quota} units（1 驗證 + 50 × {len(changes)}）")
    print("=" * 70)

    problems: list[str] = []

    if args.fail_at is not None:
        state = read_json(state_file)
        state["faults"].append({
            "op": "playlistItems.update", "playlist_id": playlist_id,
            "when_applied": args.fail_at, "status": 403,
            "reason": "quotaExceeded", "remaining": 1,
        })
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        interrupted = run_update(workspace, state_file, playlist_id, Path(args.changes))
        print(f"\n[中斷演練] status={interrupted['status']} "
              f"成功 {interrupted.get('successful')}/{len(changes)} "
              f"resume_from={interrupted.get('resume_from')}")
        if interrupted.get("successful") != args.fail_at:
            problems.append(
                f"中斷點應停在第 {args.fail_at} 筆，實際 {interrupted.get('successful')}"
            )
        partial = replay(current_ids, changes[: args.fail_at])
        live = [e["playlist_item_id"] for e in read_json(state_file)["playlists"][playlist_id]]
        if live != partial:
            problems.append("中斷後的清單狀態與已套用移動的重放結果不一致")

        state = read_json(state_file)
        state["faults"] = []
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        print("[續傳演練] 以相同指令續傳...")

    result = run_update(workspace, state_file, playlist_id, Path(args.changes))
    state = read_json(state_file)
    final_ids = [e["playlist_item_id"] for e in state["playlists"][playlist_id]]

    print(f"\n 結果       : status={result['status']} "
          f"成功 {result.get('successful')}/{result.get('total')} "
          f"失敗 {result.get('failed')}")
    print(f" 模擬配額   : {state['quota_used']} units")

    if result.get("status") != "success":
        problems.append(f"寫回未完成：{result.get('code') or result.get('message')}")
    if result.get("failed"):
        problems.append(f"{result['failed']} 筆移動失敗")

    replayed = replay(current_ids, changes)
    if final_ids != replayed:
        problems.append("實際套用結果與本地重放不一致（位置漂移模型有問題）")

    if args.target:
        target_ids = ids_of(read_json(Path(args.target)))
        if final_ids == target_ids:
            print(" 最終順序   : 與目標順序完全相符")
        else:
            first = next((i for i, (a, b) in enumerate(zip(final_ids, target_ids)) if a != b), None)
            problems.append(f"最終順序與目標不符（第一個差異在索引 {first}）")

    # 演練會留下續傳檔；真的寫回時它會讓工具誤判為「已完成」而整批跳過。
    for leftover in (REPO / "scripts" / "logs").glob(f"progress_{playlist_id}*.json"):
        leftover.unlink()
        print(f" 已清除演練殘留的續傳檔：{leftover.name}")

    print("=" * 70)
    if problems:
        print(" 演練失敗，請勿送出這份變更集：")
        for problem in problems:
            print(f"   - {problem}")
        print("=" * 70)
        return 1

    print(f" 演練通過：這份變更集可以安全送出，預估花費 {expected_quota} units。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
