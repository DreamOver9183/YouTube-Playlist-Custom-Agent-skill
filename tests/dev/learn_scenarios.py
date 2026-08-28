"""
learn_scenarios.py — 滾動式學習：把「不完整的 Prompt」累積成預設情境

**這是開發者工具，不是 skill 的一部分。** 一般使用 skill 時不會、也不該執行它。
擋門有三道：程式放在 ``tests/dev/``（不在任何 skill 載入路徑）、需要專用進入點、
且沒有設 ``YT_DEV_LEARN=1`` 就完全不寫檔。SKILL.md 從頭到尾不提這支程式。

它在做什麼
----------

Agent 每次遇到「需求講不完整」時（例如使用者說「把同歌手的歌放在一起」卻沒交代
群內順序），都只能停下來反問。這種斷層以前每次都要重新發現一次，沒有累積。

這支工具刻意餵一批不完整的 Prompt 給**真正的**認知引擎（``node dist/cli.js``，
不 mock），把引擎卡住的地方記成待審情境，交給開發者判定它屬於哪一種：

- ``ask``        本質模糊，補再多詞彙也猜不出來，只能問使用者。
                 升級後進 ``docs/agent/clarify_scenarios.json``，Agent 查表發問。
- ``vocabulary`` 需求其實講得很完整，只是這個說法沒被收錄。
                 升級後進 ``src/core/cognitive/learned-vocabulary.ts``，引擎直接看懂。

工具只負責蒐證與備妥選項，**判斷永遠留給人**。

用法
----

::

    python tests/dev/learn_scenarios.py                    # 只報告，不寫任何檔案
    YT_DEV_LEARN=1 python tests/dev/learn_scenarios.py     # 寫入待審區
    # （人工編輯 tests/dev/scenarios/pending/*.json 的 verdict 與 resolution）
    YT_DEV_LEARN=1 python tests/dev/learn_scenarios.py --promote
    python tests/dev/learn_scenarios.py --check            # 回歸模式，CI 用

全程本地計算：不碰 YouTube API、不需要憑證、0 配額。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - 主控台編碼防護
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROBES = HERE / "probes.jsonl"
SCENARIOS = HERE / "scenarios"
PENDING_DIR = SCENARIOS / "pending"
PROMOTED = SCENARIOS / "promoted.json"
QUESTIONS = REPO / "docs" / "agent" / "clarify_scenarios.json"
VOCAB_TS = REPO / "src" / "core" / "cognitive" / "learned-vocabulary.ts"

#: 沒有這個環境變數就只報告、不寫檔。三道擋門的最後一道。
ENV_GATE = "YT_DEV_LEARN"

#: 身分欄位與旗標不是有意義的排序鍵——拿 playlist_item_id 或 is_available 排序
#: 對使用者毫無意義，把它們放進問題選項只會干擾判斷。其餘欄位一律從
#: EnrichedPlaylistItem 推導，schema 一改，探針就跟著誠實。
NON_SORTABLE: frozenset[str] = frozenset({
    "playlist_item_id", "video_id", "playlist_id", "position",
    "is_available", "metadata_available", "privacy_status", "tags",
})

_node_ready = False


# ─── 基礎工具 ────────────────────────────────────────────────────


def fail(message: str) -> NoReturn:
    print(f"  ✗ {message}")
    raise SystemExit(1)


def writes_enabled() -> bool:
    return os.environ.get(ENV_GATE, "") not in ("", "0", "false", "False")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── 夾具：忠實反映 EnrichedPlaylistItem ─────────────────────────


def sortable_fields() -> list[str]:
    """可當排序鍵的欄位，直接從 schema 推導。"""
    from scripts.schemas import EnrichedPlaylistItem

    return [f for f in EnrichedPlaylistItem.model_fields if f not in NON_SORTABLE]


def build_fixture(path: Path) -> list[dict[str, Any]]:
    """產生一份小型但欄位完全真實的播放清單快照。

    用 EnrichedPlaylistItem 本尊序列化（與 ``yt_tool fetch`` 同一條路徑），
    確保引擎看到的欄位名稱與真實資料一模一樣。
    """
    from scripts.schemas import EnrichedPlaylistItem

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    channels = ["Artist A", "Artist B", "Artist C"]
    items = []
    for i in range(9):
        channel = channels[i % len(channels)]
        items.append(
            EnrichedPlaylistItem(
                playlist_item_id=f"item{i:02d}",
                video_id=f"vid{i:02d}",
                position=i,
                added_at=base + timedelta(days=i),
                playlist_id="PLdevlearner",
                title=f"{channel} - Song {i}",
                channel_title=channel,
                published_at=base + timedelta(days=200 - i * 7),
                duration_seconds=180 + i * 11,
                view_count=(9 - i) * 1000,
                like_count=(9 - i) * 10,
                comment_count=i,
            )
        )
    records = [item.model_dump(mode="json") for item in items]
    write_json(path, records)
    return records


def groupable_fields(records: list[dict[str, Any]], candidates: list[str]) -> list[str]:
    """哪些欄位真的能形成群組——由資料本身判定，不靠型別猜測。

    一個欄位若每筆都不同（例如 title），拿它分群等於沒分；若全部相同，也一樣。
    只有「有重複但不是全部相同」的欄位才是真正的分群候選。
    """
    result = []
    for field in candidates:
        values = {
            json.dumps(record.get(field), sort_keys=True, ensure_ascii=False)
            for record in records
        }
        if 1 < len(values) < len(records):
            result.append(field)
    return result


# ─── 驅動真正的認知引擎 ──────────────────────────────────────────


def ensure_node_build() -> None:
    """建置一次 TypeScript 引擎（沿用 tests/e2e/run_e2e.py 的既有作法）。"""
    global _node_ready
    if _node_ready:
        return

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        fail("npm 不在 PATH 上——認知引擎無法建置，探針跑不了")

    proc = subprocess.run(
        [npm, "run", "build"], cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=(os.name == "nt"), timeout=600,
    )
    if proc.returncode != 0:
        fail(f"npm run build 失敗:\n{proc.stdout}\n{proc.stderr}")
    if not (REPO / "dist" / "cli.js").is_file():
        fail("建置後仍找不到 dist/cli.js")
    _node_ready = True


def run_engine(fixture: Path, out: Path, intent: str) -> tuple[int, dict[str, Any]]:
    """跑真正的 CLI，回傳 (離開碼, 最後一行 JSON)。"""
    ensure_node_build()
    argv = [
        "node", str(REPO / "dist" / "cli.js"),
        "-i", str(fixture), "-o", str(out), "--intent", intent,
    ]
    proc = subprocess.run(
        argv, cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    payload: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            payload = json.loads(stripped)
    return proc.returncode, payload


def classify_gaps(code: int, payload: dict[str, Any]) -> list[str]:
    """從 CLI 輸出判定斷層種類。

    刻意只讀 CLI 對外承諾的欄位，不窺探引擎內部——這樣引擎重構不會弄壞學習器，
    而學習器看到的也正是 Agent 在真實流程中看得到的東西。
    """
    if code != 0:
        if payload.get("code") == "NO_CRITERIA":
            return ["no_criteria"]
        return [f"engine_error:{payload.get('code', 'UNKNOWN')}"]

    gaps = []
    if not payload.get("sort_criteria"):
        gaps.append("missing_sort_field")
    if not payload.get("group_dimensions"):
        gaps.append("missing_group_field")
    return gaps


# ─── 探針 ────────────────────────────────────────────────────────


def load_probes() -> list[dict[str, Any]]:
    if not PROBES.is_file():
        fail(f"找不到探針語料 {PROBES}")

    probes = []
    for number, line in enumerate(PROBES.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            probes.append(json.loads(stripped))
        except json.JSONDecodeError as error:
            fail(f"{PROBES}:{number} 不是合法 JSON：{error}")
    return probes


def draft_resolution(gaps: list[str], sortable: list[str],
                     groupable: list[str]) -> dict[str, Any]:
    """先擬一份草稿，讓人工只需要修改而不必從零想選項。"""
    if "missing_sort_field" in gaps:
        options: list[dict[str, Any]] = [{
            "label": "維持原順序",
            "sort_by": None,
            "description": "只分群，不動群內既有順序。移動次數最少、配額最省。",
        }]
        options.extend({
            "label": f"依 {field}",
            "sort_by": f"{field}:desc",
            "description": "（請補上這個選項對使用者的實際意義）",
        } for field in sortable)
        return {
            "scenario_key": None,
            "when": "",
            "header": "群內排序",
            "question": "（請改寫成你實際會問使用者的話）",
            "options": options,
        }

    if "missing_group_field" in gaps:
        return {
            "scenario_key": None,
            "when": "",
            "header": "分群依據",
            "question": "（請改寫成你實際會問使用者的話）",
            "options": [
                {"label": f"依 {field}", "group_by": field,
                 "description": "（請補上這個選項對使用者的實際意義）"}
                for field in groupable
            ],
        }

    return {"header": "", "question": "（請改寫成你實際會問使用者的話）", "options": []}


def observe(probe: dict[str, Any], fixture: Path, workdir: Path,
            sortable: list[str], groupable: list[str]) -> dict[str, Any]:
    """跑一個探針，產出一筆情境記錄。"""
    probe_id = probe["id"]
    intent = probe["intent"]
    code, payload = run_engine(fixture, workdir / f"{probe_id}.json", intent)
    gaps = classify_gaps(code, payload)

    return {
        "id": probe_id,
        "probe": intent,
        "why_incomplete": probe.get("why", ""),
        "gaps": gaps,
        "engine": {
            "exit_code": code,
            "status": payload.get("status"),
            "code": payload.get("code"),
            "warnings": payload.get("warnings", []),
            "group_dimensions": payload.get("group_dimensions", []),
            "sort_criteria": payload.get("sort_criteria", []),
        },
        "candidate_fields": {"sort": sortable, "group": groupable},
        "observed_at": now_iso(),
        "verdict": None,
        "resolution": draft_resolution(gaps, sortable, groupable),
        "how_to_review": (
            "把 verdict 填成 \"ask\"（本質模糊，補詞彙也救不了，只能問使用者）或 "
            "\"vocabulary\"（需求其實完整，只是這個說法沒被收錄）。"
            "填 \"vocabulary\" 時把 resolution 換成 "
            "{\"vocabulary\": {\"sort\": {\"<正規詞>\": [\"<新同義詞>\"]}}}，"
            "正規詞請用 Extractor.ts 既有的鍵（view / date / time / like ...）。"
            "填 \"ask\" 時把 resolution 的 question 與 options 改寫成你真的會問的話；"
            "若同一種斷層底下這是個需要分開問的變體，另外填 scenario_key 與 when。"
        ),
    }


# ─── 產生 learned-vocabulary.ts ──────────────────────────────────


VOCAB_HEADER = """/**
 * learned-vocabulary.ts — 滾動式學習累積下來的詞彙（由工具產生，但進版控）
 *
 * **請不要手改這個檔案。** 它由 `tests/dev/learn_scenarios.py --promote` 產生：
 * 開發者在測試中餵入不完整的 Prompt，把引擎辨識不出來的說法記成待審情境，
 * 判定為 `vocabulary`（只是沒收錄的講法，不是本質模糊）之後才會落到這裡。
 *
 * 合併語意由 `Extractor.ts` 負責：
 * - `group` / `sort` 以「正規詞」為鍵，同義詞併入既有條目（既有條目優先，去重）。
 * - `descMarkers` / `ascMarkers` 直接併入既有標記陣列。
 *
 * 學來的詞彙**不享有任何特權**：它一樣只能用來「挑選實際存在的欄位」，
 * 永遠不能自己成為比對條件（見 Extractor.ts 檔頭記載的舊 bug）。
 */

/** 學習所得的詞彙增量 */
export interface LearnedVocabulary {
  /** 高階分群語彙增量（正規詞 → 追加的同義詞） */
  readonly group: Readonly<Record<string, readonly string[]>>;
  /** 群內排序語彙增量（正規詞 → 追加的同義詞） */
  readonly sort: Readonly<Record<string, readonly string[]>>;
  /** 追加的降冪語意標記 */
  readonly descMarkers: readonly string[];
  /** 追加的升冪語意標記 */
  readonly ascMarkers: readonly string[];
}

export const LEARNED_VOCABULARY: LearnedVocabulary = """


def collect_vocabulary(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """把所有 vocabulary 判定合併成單一份詞彙增量。"""
    group: dict[str, list[str]] = {}
    sort: dict[str, list[str]] = {}
    desc: list[str] = []
    asc: list[str] = []

    def merge_table(target: dict[str, list[str]], addition: dict[str, Any]) -> None:
        for canonical, synonyms in addition.items():
            bucket = target.setdefault(canonical, [])
            for synonym in synonyms:
                if synonym not in bucket:
                    bucket.append(synonym)

    def merge_list(target: list[str], addition: list[str]) -> None:
        for marker in addition:
            if marker not in target:
                target.append(marker)

    for entry in ledger:
        if entry.get("verdict") != "vocabulary":
            continue
        vocabulary = (entry.get("resolution") or {}).get("vocabulary") or {}
        merge_table(group, vocabulary.get("group") or {})
        merge_table(sort, vocabulary.get("sort") or {})
        merge_list(desc, vocabulary.get("descMarkers") or [])
        merge_list(asc, vocabulary.get("ascMarkers") or [])

    return {"group": group, "sort": sort, "descMarkers": desc, "ascMarkers": asc}


def render_vocabulary_ts(vocabulary: dict[str, Any]) -> str:
    body = json.dumps(vocabulary, ensure_ascii=False, indent=2)
    return f"{VOCAB_HEADER}{body};\n"


# ─── 指令：observe / promote / check ─────────────────────────────


def cmd_observe() -> int:
    probes = load_probes()
    enabled = writes_enabled()

    print("=" * 62)
    print(" 滾動式學習：觀測不完整 Prompt 造成的斷層")
    print("=" * 62)
    if not enabled:
        print(f" 唯讀模式（未設 {ENV_GATE}=1）——只報告，不寫任何檔案")
    print()

    # 判為 ask 的情境「本來就會」持續產生斷層——那正是它被判為 ask 的理由。
    # 不把已升級的探針排除掉，開發者每跑一次就要重審同一批東西。
    already: dict[str, str] = {
        entry["id"]: entry["verdict"] for entry in (read_json(PROMOTED, []) or [])
    }

    with tempfile.TemporaryDirectory(prefix="yt-learn-") as tmp:
        workdir = Path(tmp)
        fixture = workdir / "fixture.json"
        records = build_fixture(fixture)
        sortable = sortable_fields()
        groupable = groupable_fields(records, sortable)

        written = 0
        for probe in probes:
            scenario = observe(probe, fixture, workdir, sortable, groupable)
            gaps = scenario["gaps"]
            verdict = already.get(scenario["id"])

            if verdict:
                marker = "已收錄"
                detail = f"先前已判為 {verdict}，由 --check 負責把關"
            elif gaps:
                marker = "斷層"
                detail = ", ".join(gaps)
            else:
                marker = "通過"
                detail = "引擎已能完整解析，無需收錄"
            print(f"  [{marker}] {scenario['id']:26s} {detail}")

            if verdict or not gaps or not enabled:
                continue

            target = PENDING_DIR / f"{scenario['id']}.json"
            if target.is_file():
                print(f"          （待審區已有 {target.name}，保留人工編輯結果不覆寫）")
                continue

            write_json(target, scenario)
            written += 1

    print()
    if enabled:
        print(f" 已寫入待審區 {written} 筆 → {PENDING_DIR.relative_to(REPO)}")
        print(" 下一步：編輯這些檔案的 verdict 與 resolution，再跑 --promote")
    else:
        print(f" 若要收錄，請設 {ENV_GATE}=1 後重跑")
    return 0


def merge_question(questions: dict[str, Any], scenario: dict[str, Any]) -> None:
    """同型情境併成同一個問題模板，只累積探針，不覆寫人工寫好的問法。

    「同型」預設就是「同一種斷層」，但這樣太粗：「完全沒指名排序欄位」與
    「說了新舊卻沒說是哪個日期」同屬 missing_sort_field，該問的話卻不一樣。
    所以人工可以在 resolution 填 scenario_key 把變體區分開來，並用 when
    寫下它適用的時機，讓 Agent 查表時知道該挑哪一個。
    """
    gap = scenario["gaps"][0] if scenario["gaps"] else "unknown"
    resolution = scenario.get("resolution") or {}
    key = resolution.get("scenario_key") or gap

    for existing in questions["scenarios"]:
        if existing.get("key") == key:
            probes = existing.setdefault("probes", [])
            if scenario["probe"] not in probes:
                probes.append(scenario["probe"])
            return

    questions["scenarios"].append({
        "key": key,
        "gap": gap,
        "when": resolution.get("when", ""),
        "probes": [scenario["probe"]],
        "header": resolution.get("header", ""),
        "question": resolution.get("question", ""),
        "options": resolution.get("options", []),
    })


def empty_question_library() -> dict[str, Any]:
    return {
        "version": 1,
        "note": (
            "Agent 反問使用者時的問題模板庫。由 tests/dev/learn_scenarios.py --promote "
            "產生，每一筆都經過人工核可。遇到需求不完整時先依 gap 查表，"
            "有對應情境就照模板發問，不要每次重新想問法與選項。"
        ),
        "scenarios": [],
    }


def cmd_promote() -> int:
    if not writes_enabled():
        fail(f"--promote 會寫檔，必須先設 {ENV_GATE}=1")

    pending = sorted(PENDING_DIR.glob("*.json"))
    if not pending:
        print(f" 待審區是空的（{PENDING_DIR.relative_to(REPO)}）")
        return 0

    ledger: list[dict[str, Any]] = read_json(PROMOTED, []) or []
    questions = read_json(QUESTIONS, None) or empty_question_library()

    promoted, skipped = 0, 0
    for path in pending:
        scenario = read_json(path)
        verdict = scenario.get("verdict")

        if verdict not in ("ask", "vocabulary"):
            print(f"  · 略過 {path.name}：verdict 尚未判定")
            skipped += 1
            continue

        if verdict == "ask":
            merge_question(questions, scenario)
        elif not (scenario.get("resolution") or {}).get("vocabulary"):
            fail(f"{path.name} 判定為 vocabulary，但 resolution.vocabulary 是空的")

        ledger = [entry for entry in ledger if entry.get("id") != scenario["id"]]
        ledger.append({
            "id": scenario["id"],
            "probe": scenario["probe"],
            "verdict": verdict,
            "resolution": scenario.get("resolution"),
            "promoted_at": now_iso(),
            # ask：本質模糊，補詞彙也救不了，斷層應該持續存在。
            # vocabulary：詞彙補上後，同一個探針就不該再卡住。
            "expected_gaps": scenario["gaps"] if verdict == "ask" else [],
        })
        path.unlink()
        promoted += 1
        print(f"  ✓ 升級 {scenario['id']}（{verdict}）")

    write_json(PROMOTED, ledger)
    write_json(QUESTIONS, questions)
    VOCAB_TS.write_text(render_vocabulary_ts(collect_vocabulary(ledger)), encoding="utf-8")

    print()
    print(f" 升級 {promoted} 筆，略過 {skipped} 筆")
    print(f"   問題模板庫 → {QUESTIONS.relative_to(REPO)}")
    print(f"   引擎詞彙   → {VOCAB_TS.relative_to(REPO)}")
    if promoted:
        print()
        print(" 詞彙異動需重新建置才會生效：npm run build")
    return 0


def cmd_check() -> int:
    """回歸模式：已升級的情境，行為必須與當初記錄的一致。"""
    ledger: list[dict[str, Any]] = read_json(PROMOTED, []) or []

    print("=" * 62)
    print(" 滾動式學習：已升級情境的回歸檢查")
    print("=" * 62)

    if not ledger:
        print(" 尚無已升級的情境，略過。")
        return 0

    failures = 0
    with tempfile.TemporaryDirectory(prefix="yt-learn-check-") as tmp:
        workdir = Path(tmp)
        fixture = workdir / "fixture.json"
        build_fixture(fixture)

        for entry in ledger:
            code, payload = run_engine(
                fixture, workdir / f"{entry['id']}.json", entry["probe"]
            )
            actual = classify_gaps(code, payload)
            expected = entry.get("expected_gaps", [])

            if actual == expected:
                note = ("詞彙已補上，斷層消失"
                        if entry["verdict"] == "vocabulary"
                        else "本質模糊，斷層仍在（正確）")
                print(f"  ✓ {entry['id']:26s} {note}")
                continue

            failures += 1
            print(f"  ✗ {entry['id']:26s} 預期 {expected}，實際 {actual}")
            if entry["verdict"] == "vocabulary":
                print("      學到的詞彙沒有生效——確認 learned-vocabulary.ts 已重新建置")
            else:
                print("      本來該問使用者的情境現在被引擎自行猜掉了，可能誤判")

    print()
    if failures:
        print(f" {failures} 筆不符，滾動式學習出現回歸")
        return 1
    print(f" {len(ledger)} 筆全數符合")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="滾動式學習：從不完整 Prompt 累積預設情境（開發者工具）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--promote", action="store_true",
                      help="把待審區中已判定的情境升級為正式")
    mode.add_argument("--check", action="store_true",
                      help="回歸模式：驗證已升級情境的行為未改變（CI 用）")
    args = parser.parse_args()

    if args.promote:
        return cmd_promote()
    if args.check:
        return cmd_check()
    return cmd_observe()


if __name__ == "__main__":
    raise SystemExit(main())
