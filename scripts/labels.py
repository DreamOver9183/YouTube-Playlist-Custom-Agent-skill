"""
labels.py — Agent 標註協定（v4 M3）

``label-request`` 把需要 Agent 判斷的歌寫成精簡的 ``label_request.jsonl``，Agent 讀完寫出
``labels.json``，``optimize --labels`` 驗證後存進標籤快取，再交給 ``resolver.resolve``。

- 每一行用歌在快照裡的位置 ``pos`` 當編號：比 playlist_item_id 短得多，Agent 抄寫不會抄錯，
  也省 token。答案裡的鍵就是這個 ``pos``。
- 標籤快取（``SKILL_HOME/label_cache.json``）：``items`` 以 ``platform:media_id`` 為鍵，存 Agent（A）
  與使用者在檢查頁的更正（U）；``aliases`` 存 Agent 給過的別名。快取裡已經有答案的歌不再送出。
- 預算模式：待標註的歌超過 ``max_items`` 時，先用備援權重（只有 P／T／C）算一次，只把 margin
  最低（最沒把握）的 ``max_items`` 首送 Agent；其餘的歌在 ``optimize`` 時由備援公式決定。
- Spotify：每首歌本來就有結構化藝人，只送「不同的藝人名稱」，請 Agent 合併別名。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from scripts import resolver
from scripts.schemas import EnrichedPlaylistItem

REQUEST_FILE = "label_request.jsonl"
CACHE_FILE = "label_cache.json"
CONFS = ("high", "medium")

INSTRUCTIONS = (
    "label_request.jsonl 每一行是一首歌：pos（編號）、title、channel、p（平台資料給的藝人，可能沒有）。"
    "請判斷每首歌的主要藝人：合作曲取第一位掛名；頻道是廠牌或轉載者時以標題為準；p 正確時沿用 p 的寫法。"
    "同一位藝人在整份清單只用一種寫法，其他寫法（中英文名、大小寫、團名變體）寫進 aliases。"
    "有把握 conf 填 high，推測填 medium；完全無法判斷 artist 填 unknown。"
    '輸出 labels.json：{"labels": {"<pos>": {"artist": "...", "conf": "high"}}, "aliases": {"正式名稱": ["其他寫法"]}}，'
    "每個 pos 都要有答案。"
)
INSTRUCTIONS_SPOTIFY = (
    "label_request.jsonl 每一行是這份清單裡的一個藝人名稱與首數。請找出其實是同一位藝人的不同寫法"
    "（例如中英文名、歌唱企劃與本名），寫成 labels.json："
    '{"labels": {}, "aliases": {"正式名稱": ["其他寫法"]}}；沒有要合併的就輸出空的 aliases。'
)


class LabelError(ValueError):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.extra = code, extra


def cache_key(item: EnrichedPlaylistItem) -> str:
    return f"{item.platform}:{item.video_id}"


def load_cache(path: Path) -> dict:
    """``{"items": {platform:media_id: {artist, conf, source}}, "aliases": {正式名稱: [寫法]}}``。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"items": data.get("items", {}), "aliases": data.get("aliases", {})}


def save_cache(
    path: Path, items: list[EnrichedPlaylistItem], answers: dict[str, dict], source: str,
    aliases: dict[str, list[str]] | None = None,
) -> int:
    """把 ``{playlist_item_id: {artist, conf}}`` 併進快取；source 是 A（Agent）或 U（使用者更正）。"""
    cache = load_cache(path)
    by_key = {item.playlist_item_id: item for item in items}
    written = 0
    for key, answer in answers.items():
        item = by_key.get(key)
        if item is None or not answer.get("artist") or answer["artist"] == resolver.UNKNOWN:
            continue
        old = cache["items"].get(cache_key(item), {})
        if old.get("source") == "U" and source != "U":
            continue  # 使用者的更正不會被 Agent 蓋掉
        cache["items"][cache_key(item)] = {"artist": answer["artist"], "conf": answer.get("conf", "high"),
                                           "source": source}
        written += 1
    cache["aliases"] = merge_aliases(cache["aliases"], aliases or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return written


def merge_aliases(*tables: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for table in tables:
        for name, variants in table.items():
            merged[name] = sorted(set(merged.get(name, [])) | set(variants))
    return merged


def from_cache(items: list[EnrichedPlaylistItem], cache: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """快取裡這份清單的答案：``(Agent 標註 {pid: {artist, conf}}, 使用者更正 {pid: 藝人})``。"""
    labels: dict[str, dict] = {}
    user: dict[str, str] = {}
    for item in items:
        entry = cache["items"].get(cache_key(item))
        if not entry or not item.is_available:
            continue
        if entry.get("source") == "U":
            user[item.playlist_item_id] = entry["artist"]
        else:
            labels[item.playlist_item_id] = {"artist": entry["artist"], "conf": entry.get("conf", "high")}
    return labels, user


def write_request(items: list[EnrichedPlaylistItem], out_dir: Path, cache: dict, max_items: int) -> dict:
    """寫出 ``label_request.jsonl``，回傳摘要。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    movable = [item for item in items if item.is_available]
    if movable and all(item.platform == "spotify" for item in movable):
        names = Counter(item.artists[0].name for item in movable if item.artists)
        rows = [{"artist": name, "count": count} for name, count in names.most_common()]
        return _finish(out_dir, rows, len(movable), 0, 0, INSTRUCTIONS_SPOTIFY)

    pending = [item for item in movable if cache_key(item) not in cache["items"]]
    by_formula = 0
    if len(pending) > max_items:
        found = resolver.resolve(pending)
        ranked = sorted(zip(pending, found), key=lambda pair: (pair[1].margin, pair[0].position))
        by_formula = len(pending) - max_items
        pending = sorted((item for item, _ in ranked[:max_items]), key=lambda item: item.position)
    rows = []
    for item in pending:
        row = {"pos": item.position, "title": item.title, "channel": item.channel_title}
        if p := resolver.platform_artist(item):
            row["p"] = p
        rows.append(row)
    cached = len(movable) - len(pending) - by_formula
    return _finish(out_dir, rows, len(movable), cached, by_formula, INSTRUCTIONS)


def _finish(out_dir: Path, rows: list[dict], movable: int, cached: int, by_formula: int, how: str) -> dict:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (out_dir / REQUEST_FILE).write_text(text, encoding="utf-8")
    return {
        "request_file": str(out_dir / REQUEST_FILE),
        "movable": movable, "cached": cached, "pending": len(rows),
        "by_formula": by_formula,  # 超過預算、最有把握的歌：不送 Agent，optimize 時由備援公式決定
        "est_tokens": math.ceil(len(text.encode("utf-8")) / 3),  # 粗估：中日韓約 1 字 1 token、英文約 3 字元 1 token
        "instructions": how,
    }


def validate(doc: object, items: list[EnrichedPlaylistItem], request_file: Path | None) -> tuple[dict, dict]:
    """驗證 Agent 的答案，回傳 ``({playlist_item_id: {artist, conf}}, aliases)``。

    - ``LABELS_INVALID``：格式不對、pos 不在快照裡（或是釘選項目）、conf 不是 high／medium、aliases 不是 ``{名稱: [寫法]}``。
    - ``LABELS_INCOMPLETE``：請求裡列出的 pos 沒有答案（快取命中與預先處理的歌本來就不在請求裡）。
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("labels", {}), dict):
        raise LabelError("LABELS_INVALID", 'labels.json 必須是 {"labels": {...}, "aliases": {...}}。')
    aliases = doc.get("aliases", {})
    if not isinstance(aliases, dict) or not all(
        isinstance(name, str) and isinstance(variants, list) and all(isinstance(v, str) for v in variants)
        for name, variants in aliases.items()
    ):
        raise LabelError("LABELS_INVALID", "aliases 必須是 {正式名稱: [其他寫法]}。")

    by_pos = {str(item.position): item for item in items if item.is_available}
    answers: dict[str, dict] = {}
    bad = []
    for pos, answer in doc.get("labels", {}).items():
        item = by_pos.get(str(pos))
        if (item is None or not isinstance(answer, dict) or not isinstance(answer.get("artist"), str)
                or not answer["artist"].strip() or answer.get("conf") not in CONFS):
            bad.append(pos)
            continue
        answers[item.playlist_item_id] = {"artist": answer["artist"].strip(), "conf": answer["conf"]}
    if bad:
        raise LabelError("LABELS_INVALID", f"{len(bad)} 筆答案的 pos、artist 或 conf 不合格。", invalid=bad[:20])

    if request_file is not None and request_file.is_file():
        asked = [json.loads(line).get("pos") for line in request_file.read_text(encoding="utf-8").splitlines() if line]
        answered = {str(pos) for pos in doc.get("labels", {})}
        missing = [pos for pos in asked if pos is not None and str(pos) not in answered]
        if missing:
            raise LabelError("LABELS_INCOMPLETE", f"請求裡有 {len(missing)} 首沒有答案。", missing=missing[:50])
    return answers, aliases
