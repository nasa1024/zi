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
    chapter_summaries: list[dict] = field(default_factory=list)  # M2-②: 最近 N 章前情摘要（时间正序）
    volume_summary: str = ""                                     # M2-②: 本卷至今滚动摘要
    global_synopsis: str = ""                                    # M2-②: 全书至此梗概（meta_kv）
    similar_scenes: list[dict] = field(default_factory=list)     # §06.4.5 相似桥段（向量+词法 RRF，可选）

    def to_stable_context_str(self) -> str:
        """慢变层：跨章基本不变的设定（taboo/金手指/实体/canon 事实）。

        放在所有 prompt 的最前面作为**稳定前缀**——provider 前缀缓存
        （DeepSeek 自动 / Anthropic cache_control）按字节级前缀匹配命中，
        同章多次调用与多候选生成共享此前缀（M1-⑥）。
        底层 SQL 均带 ORDER BY，序列化输出确定性。
        """
        parts = []
        if self.taboos:
            parts.append("## 常驻禁忌（绝对不可违反）\n" + _fmt_rows(self.taboos, ["rule_text", "reason"]))
        if self.gimmick_rules:
            parts.append("## 金手指规则\n" + _fmt_rows(self.gimmick_rules, ["gimmick_name", "cooldown_chapters"]))
        if self.entities:
            parts.append("## 核心实体\n" + _fmt_rows(self.entities, ["canonical_name", "entity_type"]))
        if self.canon_facts:
            parts.append("## 既定事实（canon）\n" + _fmt_rows(self.canon_facts, ["subject", "fact_type", "object", "valid_from_chapter"]))
        return "\n\n".join(parts)

    def to_dynamic_context_str(self) -> str:
        """快变层：逐章演进的世界状态，放在稳定前缀之后。

        前情摘要置于动态层最前——这是 LLM 最缺的叙事语境
        （硬状态管得住等级数值，管不住上一章的情绪余韵）。
        """
        parts = []
        if self.global_synopsis:
            parts.append(f"## 全书至此\n{self.global_synopsis}")
        if self.volume_summary:
            parts.append(f"## 本卷至今\n{self.volume_summary}")
        if self.chapter_summaries:
            lines = [f"  第{r['chapter']}章：{r['summary']}" for r in self.chapter_summaries]
            parts.append("## 前情摘要（近几章）\n" + "\n".join(lines))
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
        if self.keyword_hits:
            parts.append("## 关键词召回段落\n" + _fmt_rows(self.keyword_hits, ["chapter", "snippet"]))
        if self.recent_beats:
            parts.append("## 近期 beats\n" + _fmt_rows(self.recent_beats, ["chapter", "beat_type", "summary"]))
        if self.similar_scenes:
            parts.append("## 相似历史桥段（参照 / 避免自我重复）\n"
                         + _fmt_rows(self.similar_scenes, ["chapter", "summary"]))
        return "\n\n".join(parts)

    def to_context_str(self) -> str:
        """全量序列化 = 稳定层 + 动态层（保持单入口兼容）。"""
        stable = self.to_stable_context_str()
        dynamic = self.to_dynamic_context_str()
        return "\n\n".join(p for p in (stable, dynamic) if p)


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
    summary_window: int = 5,
    enable_summaries: bool = True,
    embedder=None,
    enable_vector: bool = False,
    scene_query: Optional[str] = None,
    vector_top_k: int = 8,
    rrf_k: int = 60,
) -> RecallPack:
    """结构化 SQL 召回（§06.3 硬上下文）。

    向量召回（§06.4.3）默认关：仅当 enable_vector 且传入 embedder 且 scene_query
    非空时，才检索相似 L2 场景（向量 + 词法 RRF 融合），写入 pack.similar_scenes。
    """
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
    pack.canon_facts = _fetch_canon_facts(entity_ids, as_of, conn)

    if enable_summaries:
        pack.chapter_summaries = _fetch_recent_summaries(as_of, conn, summary_window)
        # volume/global 摘要只保存「当前最新值」，没有历史版本可供 as-of 回放。
        # 历史查询宁可省略，也不将未来章节的滚动结果注入上下文。
        if as_of >= _latest_recall_chapter(conn):
            pack.volume_summary = _fetch_volume_summary(as_of, conn)
            pack.global_synopsis = _fetch_global_synopsis(conn)

    if keyword_query:
        pack.keyword_hits = _fts_search(keyword_query, as_of, max_keywords, conn)

    if enable_vector and embedder is not None and scene_query:
        try:
            pack.similar_scenes = search_similar_scenes(
                conn, embedder, scene_query, as_of,
                top_k=vector_top_k, rrf_k=rrf_k)
        except Exception:
            pack.similar_scenes = []   # 向量为增强、非主路：失败不影响硬召回

    return pack


# ── 向量召回：相似场景（§06.4.3）──────────────────────────────────────────────

def rrf_fuse(rank_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """客户端 Reciprocal Rank Fusion：只用排名（1-based），不归一化原始分（§06.4.3）。"""
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _lexical_rank_scenes(conn, query: str, as_of: int, limit: int) -> list[str]:
    """词法排名：按 query 分词在场景 summary+原文中的命中数排序（BM25 的轻量替身）。"""
    from .embedding import _tokens

    q_tokens = set(_tokens(query))
    if not q_tokens:
        return []
    rows = conn.execute(
        "SELECT id, summary, scene_text FROM l2_scenes WHERE chapter<=?",
        (as_of,),
    ).fetchall()
    scored: list[tuple[str, int]] = []
    for r in rows:
        hay = (r["summary"] or "") + " " + (r["scene_text"] or "")
        hits = sum(1 for t in q_tokens if t in hay)
        if hits:
            scored.append((r["id"], hits))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [sid for sid, _ in scored[:limit]]


def search_similar_scenes(
    conn, embedder, query: str, as_of: int, *, top_k: int = 8, rrf_k: int = 60,
) -> list[dict]:
    """相似 L2 场景召回：向量路 + 词法路 RRF 融合（§06.4.3）。

    Returns [{scene_id, chapter, summary, score}]，按融合分降序，最多 top_k。
    """
    from .vector_store import SceneVectorStore

    pool = max(top_k * 2, top_k)
    vec_ranked = [sid for sid, _ in SceneVectorStore(conn, embedder).search(query, as_of, pool)]
    lex_ranked = _lexical_rank_scenes(conn, query, as_of, pool)
    fused = rrf_fuse([vec_ranked, lex_ranked], k=rrf_k)[:top_k]
    if not fused:
        return []

    ids = [sid for sid, _ in fused]
    ph = ",".join("?" * len(ids))
    meta = {
        r["id"]: r for r in conn.execute(
            f"SELECT id, chapter, summary FROM l2_scenes WHERE id IN ({ph})", tuple(ids)
        ).fetchall()
    }
    out: list[dict] = []
    for sid, score in fused:
        m = meta.get(sid)
        if m is None:
            continue
        out.append({"scene_id": sid, "chapter": m["chapter"],
                    "summary": m["summary"] or "", "score": round(score, 6)})
    return out


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


def _fetch_canon_facts(ids: list[str], as_of: int, conn) -> list[dict]:
    select = (
        "SELECT id, entity_id, fact_type, subject, predicate, object,"
        " valid_from_chapter, valid_to_chapter FROM facts"
        " WHERE status='canon' AND valid_from_chapter<=?"
        " AND (valid_to_chapter IS NULL OR valid_to_chapter>?)"
    )
    if not ids:
        return _rows(
            conn,
            select + " ORDER BY valid_from_chapter DESC, id LIMIT 50",
            (as_of, as_of),
        )
    ph = ",".join("?" * len(ids))
    return _rows(
        conn,
        select + f" AND entity_id IN ({ph})"
        " ORDER BY valid_from_chapter DESC, id LIMIT 50",
        (as_of, as_of) + tuple(ids),
    )


def _latest_recall_chapter(conn) -> int:
    """返回可能已经推进「当前摘要」的最新章节。

    滚动卷摘要和全书梗概没有版本表；因此只要任一可回放层显示故事
    已超过 as-of，历史 Recall 就不应读取这两个最新值。
    """
    latest = 0
    queries = (
        "SELECT MAX(chapter) AS chapter FROM draft_index",
        "SELECT MAX(chapter) AS chapter FROM chapter_summaries",
        "SELECT MAX(chapter) AS chapter FROM l2_scenes",
        "SELECT MAX(chapter) AS chapter FROM timeline_events",
        "SELECT MAX(valid_from_chapter) AS chapter FROM facts WHERE status='canon'",
    )
    for sql in queries:
        try:
            row = conn.execute(sql).fetchone()
        except Exception:
            continue
        if row is not None and row["chapter"] is not None:
            latest = max(latest, int(row["chapter"]))
    return latest


def _fetch_recent_summaries(as_of: int, conn, window: int) -> list[dict]:
    """最近 N 章前情摘要（chapter<=as_of 继承 as-of 防剧透语义），按时间正序返回。"""
    try:
        rows = _rows(
            conn,
            "SELECT chapter, summary FROM chapter_summaries"
            " WHERE chapter<=? ORDER BY chapter DESC LIMIT ?",
            (as_of, window),
        )
        return list(reversed(rows))
    except Exception:
        return []  # 旧库未迁移时静默降级


def _fetch_volume_summary(as_of: int, conn) -> str:
    """as_of 所在卷的滚动摘要。"""
    try:
        row = conn.execute(
            "SELECT rolling_summary FROM volumes"
            " WHERE start_chapter IS NOT NULL AND start_chapter<=?"
            "   AND (end_chapter IS NULL OR end_chapter>=?)"
            " ORDER BY volume_no LIMIT 1",
            (as_of, as_of),
        ).fetchone()
        return (row["rolling_summary"] or "") if row else ""
    except Exception:
        return ""


def _fetch_global_synopsis(conn) -> str:
    """全书梗概（meta_kv，由 orchestrator 每 10 章/卷末刷新）。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta_kv WHERE key='global_synopsis'"
        ).fetchone()
        return (row["value"] or "") if row else ""
    except Exception:
        return ""


def _fts_search(query: str, as_of: int, limit: int, conn) -> list[dict]:
    from ..db.l0 import load_draft_text_by_id
    from ..tokenizer import cut

    try:
        query = (query or "").strip()[:500]
        if not query or limit <= 0:
            return []
        as_of = max(0, int(as_of))
        limit = min(int(limit), 100)
        query_tokens = []
        for token in cut(query):
            token = "".join(
                ch for ch in token.strip() if ch.isalnum() or ch == "_"
            )[:64]
            if token and token not in query_tokens:
                query_tokens.append(token)
        if not query_tokens:
            return []
        # OR 召回优先，后续按 BM25 排序；逐 token 引号包裹避免 FTS 操作符注入。
        match_query = " OR ".join(f'"{token}"' for token in query_tokens[:30])
        rows = _rows(
            conn,
            "SELECT draft_id, CAST(chapter_no AS INTEGER) AS chapter,"
            " line_start, line_end,"
            " snippet(drafts_fts, 4, '<b>', '</b>', '...', 20) AS tokenized_snippet,"
            " bm25(drafts_fts) AS bm25_score"
            " FROM drafts_fts WHERE drafts_fts MATCH ?"
            " AND CAST(chapter_no AS INTEGER)<=?"
            " ORDER BY bm25_score LIMIT ?",
            (match_query, as_of, limit),
        )
        for row in rows:
            text = load_draft_text_by_id(conn, row["draft_id"])
            if text is not None:
                lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                start = max(1, int(row.get("line_start") or 1))
                end = max(start, int(row.get("line_end") or start))
                row["snippet"] = "\n".join(lines[start - 1:end]).strip()
            else:
                row["snippet"] = row.get("tokenized_snippet", "")
        return rows
    except Exception:
        # FTS5 表不存在时静默降级
        return []
