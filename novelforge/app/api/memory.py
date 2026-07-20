"""数据平面端点：/capture /recall /state /search/facts /bible。"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import ProjectConn, ProjectRegistry, get_registry
from ..models import (
    BibleRenderResponse,
    CaptureRequest, CaptureResponse,
    MemoryHealthResponse, MemoryRebuildRequest, MemoryRebuildResponse,
    MemorySettingsPatch, MemorySettingsResponse,
    RecallItem, RecallRequest, RecallResponse,
    StateQueryRequest, WorldStateSnapshot,
)
from ...ids import new_id
from ...memory.recall import gather_hard_context
from ...memory.bible_render import render_bible
from ...contracts import normalize_proposal_fact_type

router = APIRouter(tags=["memory"])


def _memory_cfg(project_id: str, registry: ProjectRegistry, conn):
    from ...config import (
        NovelForgeConfig, apply_project_memory_settings, apply_runtime_env,
    )

    pm = registry.get(project_id)
    cfg = NovelForgeConfig(
        project_id=project_id, db_path=pm.db_path if pm else "novel.db"
    )
    apply_project_memory_settings(cfg, conn)
    apply_runtime_env(cfg)
    return cfg


def _settings_response(cfg) -> MemorySettingsResponse:
    from ...config import effective_memory_settings, memory_env_overrides

    return MemorySettingsResponse(
        **effective_memory_settings(cfg), env_overrides=memory_env_overrides()
    )


def _optional_memory_gateway(cfg):
    """仅在配置了明确的 LLM provider key 时构造维护用 gateway。

    REST 鉴权密钥 NOVELFORGE_API_KEY 绝不作为外部模型凭据使用。
    """
    provider = (os.environ.get("NOVELFORGE_PROVIDER") or "deepseek").strip().lower()
    if provider in {"anthropic", "deepseek_anthropic"}:
        key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    elif provider in {"openai_compat", "openai"}:
        key = os.environ.get("OPENAI_API_KEY")
        provider = "openai_compat"
    else:
        key = os.environ.get("DEEPSEEK_API_KEY")
        provider = "deepseek"
    if not key:
        return None
    try:
        from ...control_plane.llm.runtime import build_configured_gateway

        cfg.provider.provider = provider
        cfg.provider.api_key = key
        return build_configured_gateway(cfg)
    except Exception:
        return None


# ── /capture ──────────────────────────────────────────────────────────────────

@router.post("/{project_id}/capture", response_model=CaptureResponse, status_code=202)
def capture(
    project_id: str,
    req: CaptureRequest,
    conn=Depends(lambda project_id: None),  # replaced below
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        candidate_ids: list[str] = []
        l1_atoms_indexed = 0
        source_draft = None
        source_draft_text = None
        if req.source_kind == "draft":
            from ...db.l0 import load_draft_text_by_id

            source_draft = conn.execute(
                "SELECT id FROM draft_index WHERE chapter=?"
                " ORDER BY revision_round DESC LIMIT 1",
                (req.source_chapter,),
            ).fetchone()
            if source_draft is not None:
                source_draft_text = load_draft_text_by_id(conn, source_draft["id"])
        for p in req.proposals:
            cid = new_id("cand")
            prop = normalize_proposal_fact_type(p.model_dump())
            prop_json = json.dumps(prop, ensure_ascii=False)
            eid = _resolve_entity(p.entity, conn) if p.entity else None
            conn.execute(
                "INSERT OR IGNORE INTO fact_candidates"
                "(candidate_id, op, entity_id, fact_type, proposal_json,"
                " status, risk_tier, source_chapter, source_draft_id)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, p.op, eid, prop["fact_type"], prop_json,
                 "proposed", p.risk_tier or "low", req.source_chapter,
                 source_draft["id"] if source_draft is not None else None),
            )
            candidate_ids.append(cid)
        if source_draft is not None and source_draft_text is not None:
            from ...memory.l1_atoms import persist_proposal_atoms

            l1_stats = persist_proposal_atoms(
                conn,
                chapter=req.source_chapter,
                draft_id=source_draft["id"],
                draft_text=source_draft_text,
                proposals=[p.model_dump() for p in req.proposals],
                candidate_ids=candidate_ids,
                extracted_by="capture_api_v2",
            )
            l1_atoms_indexed = l1_stats["atoms"]
        conn.commit()
        return CaptureResponse(
            candidate_ids=candidate_ids,
            indexed={
                "fact_candidates": len(candidate_ids),
                "l1_atoms": l1_atoms_indexed,
            },
        )
    finally:
        conn.close()


# ── /recall ───────────────────────────────────────────────────────────────────

_RECALL_SOURCE_ORDER = (
    "bm25",
    "l2_scene",
    "structured_sql",
    "chapter_summary",
    "volume_summary",
    "global_synopsis",
)


def _sanitize_keyword_query(value: str | None) -> str | None:
    """Recall 边界净化：限长、去注入片段、移除控制字符并折叠空白。"""
    if not value:
        return None
    from ..security import sanitize_user_text

    cleaned = sanitize_user_text(value, max_length=500)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _interleave_recall_items(
    source_items: dict[str, list[RecallItem]], top_k: int,
) -> list[RecallItem]:
    """按来源轮转分配名额，避免一个大 canon 集合挤掉 BM25/L2。"""
    ordered_sources = [
        source for source in _RECALL_SOURCE_ORDER if source_items.get(source)
    ]
    ordered_sources.extend(sorted(
        source for source, items in source_items.items()
        if items and source not in _RECALL_SOURCE_ORDER
    ))
    cursors = {source: 0 for source in ordered_sources}
    out: list[RecallItem] = []
    while len(out) < top_k:
        progressed = False
        for source in ordered_sources:
            cursor = cursors[source]
            bucket = source_items[source]
            if cursor >= len(bucket):
                continue
            out.append(bucket[cursor])
            cursors[source] = cursor + 1
            progressed = True
            if len(out) >= top_k:
                break
        if not progressed:
            break
    return out


def _build_world_state_snapshot(
    as_of_chapter: int,
    conn,
    entity_filter: list[str] | None = None,
) -> WorldStateSnapshot:
    """组装 /state 与 /recall 共用的 JSON 可序列化快照契约。"""
    power_rows = conn.execute(
        "SELECT e.canonical_name, pr.rank_name"
        " FROM character_power_log cpl"
        " JOIN entities e ON e.id=cpl.entity_id"
        " JOIN power_ranks pr ON pr.id=cpl.rank_id"
        " WHERE cpl.change_chapter<=?"
        " ORDER BY cpl.change_chapter DESC",
        (as_of_chapter,),
    ).fetchall()
    power_ranks: dict[str, str] = {}
    for row in power_rows:
        name = row["canonical_name"]
        if name not in power_ranks:
            power_ranks[name] = row["rank_name"]
    if entity_filter:
        power_ranks = {
            name: rank for name, rank in power_ranks.items()
            if name in entity_filter
        }
    return WorldStateSnapshot(
        as_of_chapter=as_of_chapter,
        power_ranks=power_ranks,
    )


@router.post("/{project_id}/recall", response_model=RecallResponse)
def recall(
    project_id: str,
    req: RecallRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        keyword_query = _sanitize_keyword_query(req.keyword_query)
        entity_ids = [_resolve_entity(e, conn) for e in req.entities if e]
        entity_ids = [e for e in entity_ids if e]
        cfg = _memory_cfg(project_id, registry, conn)
        embedder = None
        if cfg.recall.enable_vector and keyword_query:
            from ...memory.embedding import build_embedder
            embedder = build_embedder(cfg.memory, dim=cfg.memory.embedding_dim)
        pack = gather_hard_context(
            entity_ids, req.as_of_chapter, conn,
            keyword_query=keyword_query,
            max_keywords=req.top_k,
            context_window=5,
            summary_window=cfg.recall.summary_window_chapters,
            enable_summaries=cfg.recall.enable_summaries,
            embedder=embedder,
            enable_vector=cfg.recall.enable_vector,
            scene_query=keyword_query,
            vector_top_k=cfg.memory.vector_top_k,
            rrf_k=cfg.memory.rrf_k,
        )

        source_items: dict[str, list[RecallItem]] = {}

        def add_item(item: RecallItem) -> None:
            source_items.setdefault(item.source, []).append(item)

        for fact_row in pack.canon_facts:
            add_item(RecallItem(
                source="structured_sql",
                fact_id=fact_row.get("id"),
                content=f"{fact_row.get('predicate','')}: {fact_row.get('object','')}",
                entity=fact_row.get("subject"),
                valid_from_chapter=fact_row.get("valid_from_chapter", 0),
                valid_to_chapter=fact_row.get("valid_to_chapter"),
            ))
        for kw in pack.keyword_hits:
            add_item(RecallItem(
                source="bm25",
                draft_id=kw.get("draft_id"),
                content=kw.get("snippet") or str(kw),
                valid_from_chapter=int(kw.get("chapter") or 0),
                score=kw.get("bm25_score"),
                line_start=kw.get("line_start"),
                line_end=kw.get("line_end"),
            ))
        if pack.global_synopsis:
            add_item(RecallItem(
                source="global_synopsis", content=pack.global_synopsis,
                valid_from_chapter=0,
            ))
        if pack.volume_summary:
            add_item(RecallItem(
                source="volume_summary", content=pack.volume_summary,
                valid_from_chapter=0,
            ))
        for summary in pack.chapter_summaries:
            add_item(RecallItem(
                source="chapter_summary", content=summary["summary"],
                valid_from_chapter=summary["chapter"],
            ))
        for scene in pack.similar_scenes:
            add_item(RecallItem(
                source="l2_scene", content=scene["summary"],
                scene_id=scene.get("scene_id"),
                valid_from_chapter=scene["chapter"], score=scene.get("score"),
            ))
        return RecallResponse(
            items=_interleave_recall_items(source_items, req.top_k),
            world_state_snapshot=_build_world_state_snapshot(
                req.as_of_chapter, conn,
            ),
        )
    finally:
        conn.close()


# ── /memory/settings / health / rebuild ──────────────────────────────────────

@router.get("/{project_id}/memory/settings", response_model=MemorySettingsResponse)
def memory_settings(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        return _settings_response(_memory_cfg(project_id, registry, conn))
    finally:
        conn.close()


@router.patch("/{project_id}/memory/settings", response_model=MemorySettingsResponse)
def update_memory_settings(
    project_id: str,
    req: MemorySettingsPatch,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...config import save_project_memory_settings

    conn = registry.open_conn(project_id)
    try:
        save_project_memory_settings(conn, req.model_dump(exclude_none=True))
        return _settings_response(_memory_cfg(project_id, registry, conn))
    finally:
        conn.close()


@router.get("/{project_id}/memory/health", response_model=MemoryHealthResponse)
def get_memory_health(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...memory.maintenance import memory_health

    conn = registry.open_conn(project_id)
    try:
        cfg = _memory_cfg(project_id, registry, conn)
        return MemoryHealthResponse(**memory_health(conn, cfg))
    finally:
        conn.close()


@router.post("/{project_id}/memory/rebuild", response_model=MemoryRebuildResponse)
def rebuild_project_memory(
    project_id: str,
    req: MemoryRebuildRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...memory.maintenance import rebuild_memory

    conn = registry.open_conn(project_id)
    try:
        cfg = _memory_cfg(project_id, registry, conn)
        result = rebuild_memory(
            conn,
            cfg,
            rebuild_l0=req.rebuild_l0,
            rebuild_l1=req.rebuild_l1,
            rebuild_l2=req.rebuild_l2,
            rebuild_vectors=req.rebuild_vectors,
            force=req.force,
            gateway=_optional_memory_gateway(cfg),
        )
        return MemoryRebuildResponse(result=result)
    finally:
        conn.close()


# ── /state ────────────────────────────────────────────────────────────────────

@router.post("/{project_id}/state", response_model=WorldStateSnapshot)
def state(
    project_id: str,
    req: StateQueryRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        return _build_world_state_snapshot(
            req.as_of_chapter, conn, req.entity_filter,
        )
    finally:
        conn.close()


# ── /bible ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/bible", response_model=BibleRenderResponse)
def bible(
    project_id: str,
    as_of_chapter: int = Query(default=99999),
    fmt: str = Query(default="markdown", alias="format"),
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        content, stats = render_bible(conn, as_of_chapter=as_of_chapter, fmt=fmt)
        return BibleRenderResponse(
            content=content,
            rendered_from=stats,
            is_readonly=True,
        )
    finally:
        conn.close()


# ── /search/facts ─────────────────────────────────────────────────────────────

@router.get("/{project_id}/search/facts")
def search_facts(
    project_id: str,
    q: str = Query(..., min_length=1, max_length=500),
    top_k: int = Query(default=20, ge=1, le=100),
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        keyword_query = _sanitize_keyword_query(q)
        if not keyword_query:
            return {"hits": [], "mode": "bm25"}
        from ...db.connection import project_tokenizer_context

        language, terms = project_tokenizer_context(conn)
        rows = conn.execute(
            "SELECT f.id, f.subject, f.predicate, f.object, f.valid_from_chapter"
            " FROM facts_fts fts"
            " JOIN facts f ON f.id=fts.fact_id"
            " WHERE fts.facts_fts MATCH ?"
            " ORDER BY bm25(fts.facts_fts) LIMIT ?",
            (_fts_escape(keyword_query, language, user_terms=terms), top_k),
        ).fetchall()
        hits = [
            {"id": r["id"], "snippet": f"{r['predicate']}: {r['object']}",
             "chapter": r["valid_from_chapter"]}
            for r in rows
        ]
        return {"hits": hits, "mode": "bm25"}
    except Exception:
        return {"hits": [], "mode": "bm25"}
    finally:
        conn.close()


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _resolve_entity(ref: str, conn) -> str | None:
    row = conn.execute(
        "SELECT id FROM entities WHERE id=? OR canonical_name=? LIMIT 1", (ref, ref)
    ).fetchone()
    return row["id"] if row else None


def _fts_escape(
    q: str,
    language: str = "zh-CN",
    *,
    user_terms=None,
) -> str:
    """将用户关键词转为纯文本 token 查询，不允许注入 FTS5 操作符。"""
    from ...tokenizer import tokenize

    tokens: list[str] = []
    for token in tokenize(
        (q or "")[:500], language, user_terms=user_terms,
    ).split():
        token = "".join(
            ch for ch in token.strip() if ch.isalnum() or ch == "_"
        )[:64]
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        return '""'
    return " OR ".join(f'"{token}"' for token in tokens[:30])
