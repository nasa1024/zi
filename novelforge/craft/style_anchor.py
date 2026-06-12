"""文风锚点选取与渲染（P1#9，oh-story §3.2.4）。

按本章 target_emotion 选 1-2 段 enabled 锚点做 few-shot：
精确情绪匹配 → bigram Jaccard ≥0.5 近似匹配 → 空（fail-fast 不瞎编）。
注入位置在 dynamic 段（draft/polish prompt），不碰稳定前缀（缓存无损）。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

_FUZZY_THRESHOLD = 0.5
_LIMIT = 2


def pick_style_anchors(
    conn: sqlite3.Connection, emotion: Optional[str], limit: int = _LIMIT,
) -> list[dict]:
    """按情绪选锚点。无 emotion / 无匹配 → []（不退化为随机选段）。"""
    if not emotion:
        return []
    try:
        rows = conn.execute(
            "SELECT id, emotion, title, content FROM style_anchors"
            " WHERE enabled=1 ORDER BY created_at DESC").fetchall()
    except Exception:
        return []   # 老库无表 → 静默跳过
    if not rows:
        return []
    exact = [dict(r) for r in rows if r["emotion"] == emotion]
    if exact:
        return exact[:limit]
    # 近似：字符 bigram overlap 系数（交集/较短者——情绪标签多为包含关系，
    # "紧张" vs "紧张刺激" 得 1.0；Jaccard 对短标签太苛刻）
    scored = [(s, dict(r)) for r in rows
              if (s := _similarity(emotion, r["emotion"])) >= _FUZZY_THRESHOLD]
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def render_anchor_block(anchors: list[dict]) -> str:
    """渲染注入块；空列表返回空串（调用方据此决定是否拼接）。"""
    if not anchors:
        return ""
    lines = ["## 文风参考（仿其笔触与节奏，禁止照搬内容/人名/情节）"]
    for i, a in enumerate(anchors, 1):
        lines.append(f"【参考段 {i}】（情绪：{a['emotion']}）\n{a['content']}")
    return "\n".join(lines)


def _bigrams(s: str) -> set[str]:
    s = "".join(s.split())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _similarity(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / min(len(ba), len(bb))
