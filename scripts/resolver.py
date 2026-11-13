"""
resolver.py — 藝人辨識：多來源加權公式（v4 M3）

每首歌從四個來源各拿到最多一個候選藝人 ``k_s(i)`` 與信心 ``c_s(i)``：

====  ==============================================================  ==========
來源  內容                                                            c
====  ==============================================================  ==========
P     平台結構化欄位：Spotify ``artists[0]``；YouTube Art Track 描述   1.0
      第二行「歌名 · 藝人」，沒有的話用「X - Topic」頻道名
A     Agent 標註（``labels.json``）                                   high 1.0／medium 0.5
T     標題樣式：``【藝人】歌名``、``藝人 - 歌名``                         0.95／0.80
C     頻道名（排除發行商頻道）                                         0.6
====  ==============================================================  ==========

公式（第一輪不含 corr，算出 ĝ⁰ 後再算第二輪）::

    score_i(k) = Σ_s w_s · c_s(i) · [N(k_s(i)) = k]  +  β · corr_i(k)
    corr_i(k)  = |{j≠i : ch(j)=ch(i), ĝ⁰(j)=k}| / max(1, |{j≠i : ch(j)=ch(i)}|)
    g(i)       = U(i)；沒有 U 時取 argmax_k score_i(k)（同分依 P > A > T > C，再依字典序）
    g(i)       = unknown，若 max_k score_i(k) < θ
    margin_i   = (第一高分 − 第二高分) / (w_P + w_A + w_T + w_C + β)；< τ 時在檢查頁標「待確認」

U 是使用者在檢查頁做的更正，直接採用、不參與加權。參數存在 ``resolver_params.json``：
``fused`` 用在有 Agent 標註的歌，``fallback``（w_A = 0）用在沒有標註的歌（預算模式沒送 Agent、
或 Agent 棄權），所以融合權重把 T、C 設成 0 時，沒標註的歌也不會退化成 unknown。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from scripts.schemas import EnrichedPlaylistItem

PARAMS_PATH = Path(__file__).with_name("resolver_params.json")
UNKNOWN = "unknown"
PINNED = "__pinned__"
SOURCES = ("P", "A", "T", "C")  # 同分時的優先順序
CONFIDENCE = {"P": 1.0, "C": 0.6, "high": 1.0, "medium": 0.5}

#: 發行商／廠牌頻道：頻道名不是藝人。比對前先經過同一個 N()，
#: 所以「JYP Entertainment」與表上的「jyp entertainment」會正規化成同一個鍵。
_DISTRIBUTORS = (
    "1theK", "Big Hit Labels", "SMTOWN", "JYP Entertainment", "YG Entertainment",
    "HYBE LABELS", "Stone Music Entertainment", "Ultra Music", "Warner Music Taiwan",
    "Warner Music Japan", "Sony Music", "Universal Music", "avex", "Various Artists",
    "VEVO", "Topic",
)

_SUFFIX = re.compile(
    r"(?:\b(?:official|channel|music|records?|entertainment|ent\.?|labels?|topic)|vevo)\s*$"
)
_TRAILING = re.compile(r"[\s\-–—:_·/|,]+$")
_NOT_WORD = re.compile(r"[\W_]+")
_ATV_HEADER = re.compile(r"^provided to youtube by\b", re.IGNORECASE)
_TOPIC = re.compile(r"\s+-\s+topic$", re.IGNORECASE)
# 「A x B」的 x 兩側都要有空白：「HUNTR/X」是團名，不是合作
_COLLAB = re.compile(r"\s*(?:,|&|＆|、|×|(?<=\s)x(?=\s)|\bfeat\.?|\bft\.|\bwith\b)\s*", re.IGNORECASE)
_TRAILING_PAREN = re.compile(r"\s*[\(（][^)）]*[\)）]\s*$")  # 只剪圓括號：[nZk] 是名字的一部分
_BRACKET_PREFIX = re.compile(r"^[【\[]([^\]】]+)[】\]]\s*(.+)")
_DASH = re.compile(r"^(.+?)\s*[-–—]\s*(.+?)(?:\s*[\(\[].+?[\)\]])*$")
_NOISE = (
    re.compile(r"\b(?:official\s+)?(?:music\s+video|m/?v|lyrics?\s+video|performance\s+ver(?:sion)?\.?|"
               r"live|remix|edit|cover|instrumental|karaoke|teaser|trailer|shorts?)\b", re.IGNORECASE),
    re.compile(r"\b(?:hd|4k|1080p|720p|remastered?)\b", re.IGNORECASE),
    re.compile(r"[\(\[]feat\.?\s+[^)\]]+[\)\]]", re.IGNORECASE),
    re.compile(r"[\(\[【]\s*[\)\]】]"),
)


class Candidate(NamedTuple):
    source: str  # P / A / T / C
    key: str     # N(raw)
    conf: float
    raw: str     # 原始寫法，用來挑顯示名稱


class Resolution(NamedTuple):
    key: str      # 分組鍵：N() 的結果、UNKNOWN 或 PINNED
    name: str     # 顯示名稱（這個鍵最常見的原始寫法）
    margin: float
    source: str   # 勝出鍵的最高優先來源：U／P／A／T／C／corr；unknown 時為空字串
    uncertain: bool = False  # margin < τ：檢查頁標「待確認」


def normalize(name: str, aliases: dict[str, str] | None = None) -> str:
    """N(x)：NFKC → casefold → 去頻道後綴 → 只留字母數字（空白與標點全去掉）→ 套別名表。"""
    text = unicodedata.normalize("NFKC", name or "").casefold().strip()
    for _ in range(3):
        shorter = _TRAILING.sub("", _SUFFIX.sub("", text)).strip()
        if shorter == text:
            break
        text = shorter
    key = _NOT_WORD.sub("", text)
    return aliases.get(key, key) if aliases else key


_DISTRIBUTOR_KEYS = frozenset(normalize(name) for name in _DISTRIBUTORS)


def alias_map(aliases: dict[str, list[str]] | None) -> dict[str, str]:
    """``{正式名稱: [變體]}`` → ``{N(變體): N(正式名稱)}``。"""
    lookup: dict[str, str] = {}
    for canonical, variants in (aliases or {}).items():
        target = normalize(canonical)
        for variant in [canonical, *variants]:
            if normalize(variant) and target:
                lookup[normalize(variant)] = target
    return lookup


def first_artist(text: str) -> str:
    """合作掛名只取第一位：「A feat. B」「A x B」「A & B」「A:B」→ A；去掉結尾括號註記。"""
    head = _COLLAB.split(text.strip(), maxsplit=1)[0]
    head = head.split(":", 1)[0] or head
    return _TRAILING_PAREN.sub("", head).strip() or head.strip()


def platform_artist(item: EnrichedPlaylistItem) -> str:
    """P：平台本來就給的藝人。"""
    if item.platform == "spotify":
        return item.artists[0].name if item.artists else ""
    lines = [line.strip() for line in (item.description or "").splitlines() if line.strip()]
    if len(lines) >= 2 and _ATV_HEADER.match(lines[0]) and " · " in lines[1]:
        return first_artist(lines[1].split(" · ")[1])
    if _TOPIC.search(item.channel_title or ""):
        return _TOPIC.sub("", item.channel_title).strip()
    return ""


def title_artist(title: str) -> tuple[str, float] | None:
    """T：只認兩種樣式，``【藝人】歌名``（0.95）與 ``藝人 - 歌名``（0.80）。"""
    cleaned = title or ""
    for pattern in _NOISE:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"[\s\-–—:,;]+$", "", re.sub(r"\s{2,}", " ", cleaned)).strip()
    for pattern, conf in ((_BRACKET_PREFIX, 0.95), (_DASH, 0.80)):
        match = pattern.match(cleaned)
        if match and len(match.group(1).strip()) <= 80:
            return first_artist(match.group(1)), conf
    return None


def candidates(
    item: EnrichedPlaylistItem,
    label: dict | None = None,
    aliases: dict[str, str] | None = None,
) -> list[Candidate]:
    """一首歌的 P／A／T／C 候選；正規化後為空字串的候選直接丟掉。"""
    found: list[Candidate] = []

    def add(source: str, raw: str, conf: float) -> None:
        key = normalize(raw, aliases)
        if key and key != UNKNOWN and key not in _DISTRIBUTOR_KEYS:  # Agent 填 unknown＝棄權
            found.append(Candidate(source, key, conf, raw.strip()))

    if raw := platform_artist(item):
        add("P", raw, CONFIDENCE["P"])
    if label and label.get("artist"):
        add("A", label["artist"], CONFIDENCE.get(label.get("conf", "high"), 0.5))
    if parsed := title_artist(item.title):
        add("T", *parsed)
    if item.channel_title and item.platform != "spotify":
        add("C", item.channel_title, CONFIDENCE["C"])
    return found


def _rank(scores: dict[str, float], priority: dict[str, int]) -> list[str]:
    return sorted(scores, key=lambda k: (-round(scores[k], 9), priority.get(k, len(SOURCES)), k))


def decide(
    channels: list[str],
    cands: list[list[Candidate] | None],
    params: dict,
) -> list[tuple[str, float, str]]:
    """套公式。``cands[i]`` 為 None 表示釘選項目。回傳每首的 ``(key, margin, source)``。"""
    weight = {s: params.get(f"w_{s}", 0.0) for s in SOURCES}
    beta, theta = params.get("beta", 0.0), params.get("theta", 0.0)
    norm = sum(weight.values()) + beta or 1.0

    base: list[dict[str, float]] = []
    priority: list[dict[str, int]] = []
    for found in cands:
        scores: dict[str, float] = defaultdict(float)
        rank: dict[str, int] = {}
        for cand in found or ():
            if weight[cand.source] > 0:
                scores[cand.key] += weight[cand.source] * cand.conf
                rank[cand.key] = min(rank.get(cand.key, 9), SOURCES.index(cand.source))
        base.append(dict(scores))
        priority.append(rank)

    first = [(_rank(s, r) or [None])[0] for s, r in zip(base, priority)]
    peers: dict[str, list[int]] = defaultdict(list)
    for i, found in enumerate(cands):
        if found is not None and channels[i]:  # Spotify 沒有頻道，不做同頻道佐證
            peers[channels[i]].append(i)

    decided = []
    for i, found in enumerate(cands):
        if found is None:
            decided.append((PINNED, 0.0, ""))
            continue
        scores = dict(base[i])
        others = [j for j in peers[channels[i]] if j != i]
        if beta and others:
            for key, count in Counter(first[j] for j in others if first[j]).items():
                scores[key] = scores.get(key, 0.0) + beta * count / len(others)
        ranked = _rank(scores, priority[i])
        if not ranked or scores[ranked[0]] < theta or scores[ranked[0]] <= 0:
            decided.append((UNKNOWN, 0.0, ""))
            continue
        runner_up = scores[ranked[1]] if len(ranked) > 1 else 0.0
        source = SOURCES[priority[i][ranked[0]]] if ranked[0] in priority[i] else "corr"
        decided.append((ranked[0], (scores[ranked[0]] - runner_up) / norm, source))
    return decided


def load_params(mode: str = "fallback", path: Path = PARAMS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["weights"][mode]


def decide_mixed(
    channels: list[str], cands: list[list[Candidate] | None], fused: dict, fallback: dict,
) -> list[tuple[str, float, str, float]]:
    """有 A 候選的歌用 ``fused``、其餘用 ``fallback``；回傳每首的 ``(key, margin, source, τ)``。"""
    has_a = [any(c.source == "A" for c in found or ()) for found in cands]
    rich = decide(channels, cands, fused) if any(has_a) else []
    plain = decide(channels, cands, fallback)
    return [
        (*(rich if a else plain)[i], (fused if a else fallback).get("tau", 0.0)) for i, a in enumerate(has_a)
    ]


def resolve(
    items: list[EnrichedPlaylistItem],
    labels: dict[str, dict] | None = None,
    aliases: dict[str, list[str]] | None = None,
    user: dict[str, str] | None = None,
    fused: dict | None = None,
    fallback: dict | None = None,
) -> list[Resolution]:
    """整份清單的辨識結果，與 ``items`` 同順序。

    ``labels``：``{playlist_item_id: {"artist", "conf"}}``（Agent 標註）；
    ``user``：``{playlist_item_id: 藝人}``（使用者更正，直接採用）。
    有 A 候選的歌用 ``fused`` 參數，其餘用 ``fallback``；沒給就讀 ``resolver_params.json``。
    """
    lookup = alias_map(aliases)
    labels = labels or {}
    cands = [
        candidates(item, labels.get(item.playlist_item_id), lookup) if item.is_available else None
        for item in items
    ]
    mixed = decide_mixed(
        [item.channel_title for item in items], cands,
        fused or (load_params("fused") if labels else {}), fallback or load_params("fallback"),
    )

    decided = []
    for item, (key, margin, source, tau) in zip(items, mixed):
        if user and item.playlist_item_id in user and item.is_available:
            key, margin, source = normalize(user[item.playlist_item_id], lookup) or UNKNOWN, 1.0, "U"
        decided.append((key, margin, source, key not in (UNKNOWN, PINNED) and margin < tau))

    spellings: dict[str, Counter] = defaultdict(Counter)
    for found in cands:
        for cand in found or ():
            spellings[cand.key][cand.raw] += 1
    for item in items:
        if user and item.playlist_item_id in user:
            spellings[normalize(user[item.playlist_item_id], lookup)][user[item.playlist_item_id]] += 5

    return [
        Resolution(key, spellings[key].most_common(1)[0][0] if spellings.get(key) else key, margin, source, flag)
        for key, margin, source, flag in decided
    ]
