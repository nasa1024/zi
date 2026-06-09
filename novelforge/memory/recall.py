"""Recall：结构化 SQL 召回 + FTS5 关键词召回 + 常驻 taboo（§06.3）。

gather_hard_context(entity_ids, as_of, conn) → RecallPack
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RecallPack:
    """一次召回的全量上下文，传入 ChapterDraftSkill。"""
    entities: list[dict] = field(default_factory=list)          # entities 行
    power_states: list[dict] = field(default_factory=list)      # character_power_log 最新
    knowledge_edges: list[dict] = field(default_factory=list)   # knowledge_edges
    item_states: list[dict] = field(default_factory=list)       # item_log 最新
    numeric_facts: list[dict] = field(default_factory=list)
    taboos: list[dict] = field(default_factory=list)            # 常驻禁忌（绝对不能写的）
    timeline_events: list[dict] = field(default_factory=list)
    keyword_hits: list[dict] = field(default_factory=list)      # FTS5 关键词命中
    canon_facts: list[dict] = field(default_factory=list)       # facts where status='canon'
    recent_beats: list[dict] = field(default_factory=list)      # 最近 N 章 beats
    gimmick_rules: list[dict] = field(default_factory=list)

    def to_context_str(self) -> str:
        """将召回结果序列化为 prompt 上下文字符串（简化版）。"""
        parts = []
        if self.entities:
            parts.append("## 核心实体\n" + _fmt_rows(self.entities, ["canonical_name", "entity_type"]))
        if self.power_states:
            parts.append("## 当前境界\n" + _fmt_rows(self.power_states, ["entity_id", "rank_name", "rank_order"]))
        if self.knowledge_edges:
            parts.append("## 知情关系\n" + _fmt_rows(self.knowledge_edges, ["knower_entity_id", "secret_key", "knowledge_state"]))
        if self.item_states:
            parts.append("## 道具持有\n" + _fmt_rows(self.item_states, ["item_entity_id", "to_owner_id", "quantity_delta", "change_type"]))
        if self.numeric_facts:
            parts.append("## 数值事实\n" + _fmt_rows(self.numeric_facts, ["entity_id", "metric_key", "value", "unit"]))
        if self.timeline_events:
            parts.append("## 时间线事件\n" + _fmt_rows(self.timeline_events, ["title", "chapter", "story_time_start"]))
        if self.taboos:
            parts.append("## 常驻禁忌（绝对不可违反）\n" + _fmt_rows(self.taboos, ["rule_text", "reason"]))
        if self.gimmick_rules:
            parts.append("## 金手指规则\n" + _fmt_rows(self.gimmick_rules, ["gimmick_name", "cooldown_chapters"]))
        if self.keyword_hits:
            parts.append("## 关键词召回段落\n" + _fmt_rows(self.keyword_hits, ["chapter", "snippet"]))
        if self.recent_beats:
            parts.append("## 近期 beats\n" + _fmt_rows(self.recent_beats, ["chapter", "beat_type", "summary"]))
        return "\n\n".join(parts)


def _fmt_rows(rows: list, keys: list[str]) -> str:
    lines = []
    for r in rows:
        if isinstance(r, dict):
            vals = [f"{k}={r.get(k, '')}" for k in keys if r.get(k) is not None]
        else:
            vals = [f"{k}={getattr(r, k, '')}" for k in keys if getattr(r, k, None) is not None]
        lines.append("  " + ", ".join(vals))
    return "\n".join(lines) if lines else "  (无)"


# ── 召回函数 ──────────────────────────────────────────────────────────────────

def gather_hard_context(
    entity_ids: list[str],
    as_of: int,
    conn: sqlite3.Connection,
    *,
    keyword_query: Optional[str] = None,
    max_keywords: int = 30,
    context_window: int = 5,
) -> RecallPack:
    """结构化 SQL 召回（§06.3 硬上下文）。"""
    pack = RecallPack()

    if entity_ids:
        pack.entities = _fetch_entities(entity_ids, conn)
        pack.power_states = _fetch_power(entity_ids, as_of, conn)
        pack.knowledge_edges = _fetch_knowledge(entity_ids, as_of, conn)
        pack.item_states = _fetch_items(entity_ids, as_of, conn)
        pack.numeric_facts = _fetch_numeric(entity_ids, as_of, conn)

    pack.timeline_events = _fetch_timeline(as_of, conn)
    pack.taboos = _fetch_taboos(conn)
    pack.gimmick_rules = _fetch_gimmicks(as_of, conn)
    pack.recent_beats = _fetch_recent_beats(as_of, conn, context_window)
    pack.canon_facts = _fetch_canon_facts(entity_ids, conn)

    if keyword_query:
        pack.keyword_hits = _fts_search(keyword_query, max_keywords, conn)

    return pack


# ── 私有查询 ──────────────────────────────────────────────────────────────────

def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _fetch_entities(ids: list[str], conn) -> list[dict]:
    ph = ",".join("?" * len(ids))
    return _rows(conn, f"SELECT id, canonical_name, entity_type, detail_json FROM entities WHERE id IN ({ph})", tuple(ids))


def _fetch_power(ids: list[str], as_of: int, conn) -> list[dict]:
    ph = ",".join("?" * len(ids))
    return _rows(
        conn,
        f"SELECT p.entity_id, r.rank_name, p.rank_order, p.change_chapter"
        f" FROM character_power_log p"
        f" LEFT JOIN power_ranks r ON r.id=p.rank_id"
        f" LEFT JOIN facts f ON f.id=p.source_fact_id"
        f" WHERE p.entity_id IN ({ph}) AND p.change_chapter<=?"
        f"   AND (p.source_fact_id IS NULL OR (f.status='canon' AND f.valid_from_chapter<=?))"
        f" ORDER BY p.entity_id, p.change_chapter",
        tuple(ids) + (as_of, as_of),
    )


def _fetch_knowledge(ids: list[str], as_of: int, conn) -> list[dict]:
    ph = ",".join("?" * len(ids))
    return _rows(
        conn,
        f"SELECT k.knower_entity_id, k.secret_key, k.knowledge_state, k.learned_chapter"
        f" FROM knowledge_edges k"
        f" LEFT JOIN facts f ON f.id=k.source_fact_id"
        f" WHERE k.knower_entity_id IN ({ph}) AND k.learned_chapter<=?"
        f"   AND (k.source_fact_id IS NULL OR (f.status='canon' AND f.valid_from_chapter<=?))"
        f" ORDER BY k.knower_entity_id, k.secret_key",
        tuple(ids) + (as_of, as_of),
    )


def _fetch_items(ids: list[str], as_of: int, conn) -> list[dict]:
    ph = ",".join("?" * len(ids))
    return _rows(
        conn,
        f"SELECT il.item_entity_id, il.to_owner_id, il.quantity_delta, il.change_type, il.change_chapter"
        f" FROM item_log il"
        f" LEFT JOIN facts f ON f.id=il.source_fact_id"
        f" WHERE (il.to_owner_id IN ({ph}) OR il.from_owner_id IN ({ph}))"
        f"   AND il.change_chapter<=?"
        f"   AND (il.source_fact_id IS NULL OR (f.status='canon' AND f.valid_from_chapter<=?))"
        f" ORDER BY il.item_entity_id, il.change_chapter",
        tuple(ids) + tuple(ids) + (as_of, as_of),
    )


def _fetch_numeric(ids: list[str], as_of: int, conn) -> list[dict]:
    ph = ",".join("?" * len(ids))
    return _rows(
        conn,
        f"SELECT n.entity_id, n.metric_key, n.value, n.unit, n.as_of_chapter"
        f" FROM numeric_facts n"
        f" LEFT JOIN facts f ON f.id=n.source_fact_id"
        f" WHERE n.entity_id IN ({ph}) AND n.as_of_chapter<=?"
        f"   AND (n.source_fact_id IS NULL OR (f.status='canon' AND f.valid_from_chapter<=?))"
        f" ORDER BY n.entity_id, n.metric_key, n.as_of_chapter",
        tuple(ids) + (as_of, as_of),
    )


def _fetch_timeline(as_of: int, conn) -> list[dict]:
    return _rows(
        conn,
        "SELECT te.title, te.chapter, te.story_time_start, te.location_id"
        " FROM timeline_events te"
        " LEFT JOIN facts f ON f.id=te.source_fact_id"
        " WHERE te.chapter<=?"
        "   AND (te.source_fact_id IS NULL OR f.status='canon')"
        " ORDER BY te.story_time_start",
        (as_of,),
    )


def _fetch_taboos(conn) -> list[dict]:
    try:
        return _rows(conn, "SELECT rule_text, reason FROM taboo_rules ORDER BY priority DESC")
    except Exception:
        return []


def _fetch_gimmicks(as_of: int, conn) -> list[dict]:
    return _rows(
        conn,
        "SELECT gr.gimmick_name, gr.cooldown_chapters, gr.valid_from_chapter"
        " FROM gimmick_rules gr"
        " LEFT JOIN facts f ON f.id=gr.source_fact_id"
        " WHERE gr.valid_from_chapter<=?"
        "   AND (gr.source_fact_id IS NULL OR f.status='canon')",
        (as_of,),
    )


def _fetch_recent_beats(as_of: int, conn, window: int) -> list[dict]:
    try:
        return _rows(
            conn,
            "SELECT chapter, beat_type, summary, value_axis FROM beats"
            " WHERE chapter<=? ORDER BY chapter DESC LIMIT ?",
            (as_of, window * 4),
        )
    except Exception:
        return []


def _fetch_canon_facts(ids: list[str], conn) -> list[dict]:
    if not ids:
        base = "SELECT id, entity_id, fact_type, subject, object, valid_from_chapter FROM facts WHERE status='canon' ORDER BY valid_from_chapter DESC LIMIT 50"
        return _rows(conn, base)
    ph = ",".join("?" * len(ids))
    return _rows(
        conn,
        f"SELECT id, entity_id, fact_type, subject, object, valid_from_chapter"
        f" FROM facts WHERE status='canon' AND entity_id IN ({ph})"
        f" ORDER BY valid_from_chapter DESC",
        tuple(ids),
    )


def _fts_search(query: str, limit: int, conn) -> list[dict]:
    try:
        rows = _rows(
            conn,
            "SELECT chapter, snippet(drafts_fts, 0, '<b>', '</b>', '...', 20) AS snippet"
            " FROM drafts_fts WHERE drafts_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        )
        return rows
    except Exception:
        # FTS5 表不存在时静默降级
        return []
