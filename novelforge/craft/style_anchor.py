"""文风锚点选取与渲染（P1#9，oh-story §3.2.4）。

按本章 target_emotion 选 1-2 段 enabled 锚点做 few-shot：
精确情绪匹配 → bigram Jaccard ≥0.5 近似匹配 → 空（fail-fast 不瞎编）。
注入位置在 dynamic 段（draft/polish prompt），不碰稳定前缀（缓存无损）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

_FUZZY_THRESHOLD = 0.5
_LIMIT = 2


def pick_style_anchors(
    conn: sqlite3.Connection,
    emotion: Optional[str],
    limit: int = _LIMIT,
    *,
    language: Optional[str] = None,
    profile_tags: Optional[list[str]] = None,
) -> list[dict]:
    """Pick licensed anchors inside one language/profile boundary.

    ``language=None`` preserves the old direct-call contract.  Creative
    generation passes an explicit language, in which case legacy tables are
    treated as Chinese and can never leak into an English prompt.
    """
    if not emotion or limit <= 0:
        return []
    try:
        rows = conn.execute(
            "SELECT id, emotion, title, content, language, source_type,"
            " source_note, rights_confirmed, profile_tags"
            " FROM style_anchors WHERE enabled=1 AND rights_confirmed=1"
            " ORDER BY created_at DESC"
        ).fetchall()
    except Exception:
        # Pre-v21 compatibility: old rows are grandfathered Chinese anchors.
        if language is not None and language != "zh-CN":
            return []
        try:
            rows = conn.execute(
                "SELECT id, emotion, title, content FROM style_anchors"
                " WHERE enabled=1 ORDER BY created_at DESC"
            ).fetchall()
        except Exception:
            return []   # 老库无表 → 静默跳过
    if not rows:
        return []

    requested_tags = {
        str(tag).strip().casefold() for tag in (profile_tags or [])
        if str(tag).strip()
    }
    candidates: list[dict] = []
    for row in rows:
        item = dict(row)
        item.setdefault("language", "zh-CN")
        item.setdefault("source_type", "legacy")
        item.setdefault("source_note", None)
        item.setdefault("rights_confirmed", 1)
        raw_tags = item.get("profile_tags", "[]")
        if isinstance(raw_tags, str):
            try:
                raw_tags = json.loads(raw_tags)
            except json.JSONDecodeError:
                raw_tags = []
        item["profile_tags"] = raw_tags if isinstance(raw_tags, list) else []
        if language is not None and item["language"] != language:
            continue
        if not bool(item["rights_confirmed"]):
            continue
        if profile_tags is not None:
            anchor_tags = {
                str(tag).strip().casefold() for tag in item["profile_tags"]
                if str(tag).strip()
            }
            # Untagged anchors are general-purpose; tagged anchors must share
            # at least one project tag.
            if anchor_tags and not (anchor_tags & requested_tags):
                continue
        candidates.append(item)

    exact = [item for item in candidates if item["emotion"] == emotion]
    if exact:
        return exact[:limit]
    # 近似：字符 bigram overlap 系数（交集/较短者——情绪标签多为包含关系，
    # "紧张" vs "紧张刺激" 得 1.0；Jaccard 对短标签太苛刻）
    scored = [(score, item) for item in candidates
              if (score := _similarity(emotion, item["emotion"])) >= _FUZZY_THRESHOLD]
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def render_anchor_block(
    anchors: list[dict], *, language: str = "zh-CN",
) -> str:
    """渲染注入块；空列表返回空串（调用方据此决定是否拼接）。"""
    if not anchors:
        return ""
    if language.startswith("en"):
        lines = [
            "## Authorized style references (learn abstract rhythm, tone, and "
            "technique only; never copy wording, names, characters, or plot)"
        ]
        for i, anchor in enumerate(anchors, 1):
            lines.append(
                f"[Reference {i}] (target emotion: {anchor['emotion']})\n"
                f"{anchor['content']}"
            )
    else:
        lines = ["## 文风参考（只学习抽象笔触、节奏与语气，禁止照搬措辞/人名/角色/情节）"]
        for i, anchor in enumerate(anchors, 1):
            lines.append(
                f"【参考段 {i}】（情绪：{anchor['emotion']}）\n"
                f"{anchor['content']}"
            )
    return "\n".join(lines)


def _bigrams(s: str) -> set[str]:
    s = "".join(s.split())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _similarity(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / min(len(ba), len(bb))
