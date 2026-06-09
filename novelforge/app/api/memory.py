"""数据平面端点：/capture /recall /state /search/facts /bible。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import ProjectConn, ProjectRegistry, get_registry
from ..models import (
    BibleRenderResponse,
    CaptureRequest, CaptureResponse,
    RecallItem, RecallRequest, RecallResponse,
    StateQueryRequest, WorldStateSnapshot,
)
from ...ids import new_id
from ...memory.recall import gather_hard_context
from ...memory.bible_render import render_bible
from ...world.replay import get_world_state

router = APIRouter(tags=["memory"])


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
        for p in req.proposals:
            cid = new_id("cand")
            prop_json = json.dumps(p.model_dump(), ensure_ascii=False)
            eid = _resolve_entity(p.entity, conn) if p.entity else None
            conn.execute(
                "INSERT OR IGNORE INTO fact_candidates"
                "(candidate_id, op, entity_id, fact_type, proposal_json,"
                " status, risk_tier, source_chapter)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (cid, p.op, eid, p.fact_type, prop_json,
                 "proposed", p.risk_tier or "low", req.source_chapter),
            )
            candidate_ids.append(cid)
        conn.commit()
        return CaptureResponse(
            candidate_ids=candidate_ids,
            indexed={"fact_candidates": len(candidate_ids)},
        )
    finally:
        conn.close()


# ── /recall ───────────────────────────────────────────────────────────────────

@router.post("/{project_id}/recall", response_model=RecallResponse)
def recall(
    project_id: str,
    req: RecallRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        entity_ids = [_resolve_entity(e, conn) for e in req.entities if e]
        entity_ids = [e for e in entity_ids if e]
        pack = gather_hard_context(
            entity_ids, req.as_of_chapter, conn,
            keyword_query=req.keyword_query,
            max_keywords=req.top_k,
            context_window=5,
        )
        ws = get_world_state(req.as_of_chapter, conn)

        items: list[RecallItem] = []
        for fact_row in pack.canon_facts:
            items.append(RecallItem(
                source="structured_sql",
                fact_id=fact_row.get("id"),
                content=f"{fact_row.get('predicate','')}: {fact_row.get('object','')}",
                entity=fact_row.get("subject"),
                valid_from_chapter=fact_row.get("valid_from_chapter", 0),
            ))
        for kw in pack.keyword_hits:
            items.append(RecallItem(
                source="bm25",
                content=str(kw),
                valid_from_chapter=0,
            ))
        return RecallResponse(
            items=items[:req.top_k],
            world_state_snapshot=vars(ws) if ws else {"as_of_chapter": req.as_of_chapter},
        )
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
        ws = get_world_state(req.as_of_chapter, conn)
        # 组装 snapshot
        power_rows = conn.execute(
            "SELECT e.canonical_name, pr.rank_name"
            " FROM character_power_log cpl"
            " JOIN entities e ON e.id=cpl.entity_id"
            " JOIN power_ranks pr ON pr.id=cpl.rank_id"
            " WHERE cpl.change_chapter<=?"
            " ORDER BY cpl.change_chapter DESC",
            (req.as_of_chapter,),
        ).fetchall()
        power_ranks: dict[str, str] = {}
        for row in power_rows:
            name = row["canonical_name"]
            if name not in power_ranks:
                power_ranks[name] = row["rank_name"]

        if req.entity_filter:
            power_ranks = {k: v for k, v in power_ranks.items()
                           if k in req.entity_filter}

        return WorldStateSnapshot(
            as_of_chapter=req.as_of_chapter,
            power_ranks=power_ranks,
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
    q: str = Query(...),
    top_k: int = Query(default=20),
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT f.id, f.subject, f.predicate, f.object, f.valid_from_chapter"
            " FROM facts_fts fts"
            " JOIN facts f ON f.id=fts.fact_id"
            " WHERE fts.facts_fts MATCH ?"
            " ORDER BY bm25(fts.facts_fts) LIMIT ?",
            (_fts_escape(q), top_k),
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


def _fts_escape(q: str) -> str:
    for ch in ('"', "'", "*", "^", "(", ")"):
        q = q.replace(ch, " ")
    return q.strip() or '""'
