"""Autopilot 连写端点（§13 模式2）：start / status / degrade + bible seed。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..autopilot_manager import AutopilotSession, get_autopilot_manager
from ..deps import ProjectRegistry, get_registry
from ..models import (
    AutopilotDegradeRequest, AutopilotSessionInfo, AutopilotStartRequest,
    BudgetSpent,
    SeedRequest, SeedResponse,
)

router = APIRouter(tags=["autopilot"])


# ── 转换辅助 ──────────────────────────────────────────────────────────────────

def _session_to_info(s: AutopilotSession, conn=None) -> AutopilotSessionInfo:
    pending = 0
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM review_queue WHERE status='pending'"
            ).fetchone()
            pending = row["n"] if row else 0
        except Exception:
            pass
    return AutopilotSessionInfo(
        session_id=s.session_id,
        project_id=s.project_id,
        from_chapter=s.from_chapter,
        to_chapter=s.to_chapter,
        current_chapter=s.current_chapter,
        status=s.status,
        policy_mode=s.policy_mode,
        chapters_done=s.chapters_done,
        chapters_total=s.chapters_total,
        budget_tokens_total=s.budget_tokens_total,
        budget_usd_total=round(s.budget_usd_total, 6),
        pending_reviews=pending,
        consecutive_hard_issues=s.consecutive_hard_issues,
        last_error=s.last_error,
        started_at=s.started_at,
        finished_at=s.finished_at,
    )


# ── POST /{project_id}/autopilot/start ───────────────────────────────────────

@dataclass
class _StartReq:
    """内部：携带 project_id 的启动请求对象。"""
    project_id: str
    from_chapter: int
    to_chapter: int
    chapter_goals: dict
    mode: str
    budget_max_tokens_per_chapter: Optional[int]
    budget_max_usd_per_chapter: Optional[float]
    auto_degrade_after_consecutive_issues: int
    budget_session_max_tokens: Optional[int] = None
    budget_session_max_usd: Optional[float] = None


@router.post("/{project_id}/autopilot/start", response_model=AutopilotSessionInfo, status_code=202)
def autopilot_start(
    project_id: str,
    req: AutopilotStartRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    if req.from_chapter > req.to_chapter:
        raise HTTPException(422, f"from_chapter ({req.from_chapter}) > to_chapter ({req.to_chapter})")

    mgr = get_autopilot_manager()
    api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")

    internal_req = _StartReq(
        project_id=project_id,
        from_chapter=req.from_chapter,
        to_chapter=req.to_chapter,
        chapter_goals=req.chapter_goals or {},
        mode=req.mode,
        budget_max_tokens_per_chapter=req.budget_max_tokens_per_chapter,
        budget_max_usd_per_chapter=req.budget_max_usd_per_chapter,
        auto_degrade_after_consecutive_issues=req.auto_degrade_after_consecutive_issues,
        budget_session_max_tokens=req.budget_session_max_tokens,
        budget_session_max_usd=req.budget_session_max_usd,
    )
    session = mgr.start(internal_req, registry, api_key=api_key)
    return _session_to_info(session)


# ── GET /{project_id}/autopilot/status ───────────────────────────────────────

@router.get("/{project_id}/autopilot/status", response_model=list[AutopilotSessionInfo])
def autopilot_status(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    mgr = get_autopilot_manager()
    conn = registry.open_conn(project_id)
    try:
        # 内存活跃会话 ∪ DB 历史会话（重启残留的 running/degraded 在此被标 interrupted）
        sessions = mgr.list_for_project(project_id) + mgr.load_db_sessions(project_id, conn)
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return [_session_to_info(s, conn) for s in sessions]
    finally:
        conn.close()


@router.get("/{project_id}/autopilot/{session_id}", response_model=AutopilotSessionInfo)
def autopilot_get_session(
    project_id: str,
    session_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    mgr = get_autopilot_manager()
    s = mgr.get(session_id)
    if s is not None and s.project_id != project_id:
        raise HTTPException(404, f"会话不存在: {session_id}")
    conn = registry.open_conn(project_id)
    try:
        if s is None:
            # 内存没有 → 找 DB 持久化行（进程重启后的历史会话）
            db_sessions = [x for x in mgr.load_db_sessions(project_id, conn)
                           if x.session_id == session_id]
            if not db_sessions:
                raise HTTPException(404, f"会话不存在: {session_id}")
            s = db_sessions[0]
        return _session_to_info(s, conn)
    finally:
        conn.close()


# ── POST /{project_id}/autopilot/{session_id}/resume ─────────────────────────

@router.post("/{project_id}/autopilot/{session_id}/resume",
             response_model=AutopilotSessionInfo, status_code=202)
def autopilot_resume(
    project_id: str,
    session_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """从断点恢复被中断的会话：以「已完成最大章 +1」与 current_chapter 的较大者为新起点，
    沿用原启动参数开新会话（resumed_from 链回旧会话）。已完成的章不会重写。"""
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    mgr = get_autopilot_manager()
    live = mgr.get(session_id)
    if live is not None and live.status in ("running", "degraded"):
        raise HTTPException(409, f"会话仍在运行，无需恢复: {session_id}")

    conn = registry.open_conn(project_id)
    try:
        info = mgr.get_db_session_req(session_id, conn)
        if info is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        if info["status"] not in ("interrupted", "error", "circuit_broken"):
            raise HTTPException(409, f"会话状态 {info['status']} 不可恢复")

        from ...app.chapter_suggest import next_chapter_no
        next_ch, _ = next_chapter_no(conn, project_id)
        from_chapter = max(info["current_chapter"], next_ch)
        if from_chapter > info["to_chapter"]:
            raise HTTPException(409, "目标章已全部完成，无需恢复")
    finally:
        conn.close()

    req = info["req"]
    internal_req = _StartReq(
        project_id=project_id,
        from_chapter=from_chapter,
        to_chapter=info["to_chapter"],
        chapter_goals=req.get("chapter_goals") or {},
        mode=req.get("mode") or "auto_promote",
        budget_max_tokens_per_chapter=req.get("budget_max_tokens_per_chapter"),
        budget_max_usd_per_chapter=req.get("budget_max_usd_per_chapter"),
        auto_degrade_after_consecutive_issues=req.get("auto_degrade_after_consecutive_issues", 2),
        budget_session_max_tokens=req.get("budget_session_max_tokens"),
        budget_session_max_usd=req.get("budget_session_max_usd"),
    )
    api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    session = mgr.start(internal_req, registry, api_key=api_key, resumed_from=session_id)
    return _session_to_info(session)


# ── POST /{project_id}/autopilot/degrade ─────────────────────────────────────

@router.post("/{project_id}/autopilot/{session_id}/degrade", response_model=AutopilotSessionInfo)
def autopilot_degrade(
    project_id: str,
    session_id: str,
    req: AutopilotDegradeRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    mgr = get_autopilot_manager()
    s = mgr.get(session_id)
    if s is None or s.project_id != project_id:
        raise HTTPException(404, f"会话不存在: {session_id}")
    if s.status not in ("running", "degraded"):
        raise HTTPException(409, f"会话状态 {s.status} 不可降级")
    ok = mgr.degrade(session_id, req.reason)
    if not ok:
        raise HTTPException(409, "降级请求发送失败（会话可能已停止）")
    return _session_to_info(s)


# ── POST /{project_id}/autopilot/{session_id}/cancel ─────────────────────────

@router.post("/{project_id}/autopilot/{session_id}/cancel", response_model=AutopilotSessionInfo)
def autopilot_cancel(
    project_id: str,
    session_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """协作式取消正在运行的 autopilot 会话（E7）。"""
    mgr = get_autopilot_manager()
    s = mgr.get(session_id)
    if s is None or s.project_id != project_id:
        raise HTTPException(404, f"会话不存在: {session_id}")
    if s.status not in ("running", "degraded"):
        raise HTTPException(409, f"会话状态 {s.status} 不可取消")
    mgr.cancel(session_id)
    return _session_to_info(s)


# ── POST /{project_id}/autopilot/cleanup ──────────────────────────────────────

@router.post("/{project_id}/autopilot/cleanup")
def autopilot_cleanup(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """清理超过 TTL 的僵尸会话（E8）。返回已清理的 session_id 列表。"""
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    mgr = get_autopilot_manager()
    cleaned = mgr.cleanup_stale_sessions()
    return {"cleaned": cleaned, "count": len(cleaned)}


# ── POST /{project_id}/seed ───────────────────────────────────────────────────

@router.post("/{project_id}/seed", response_model=SeedResponse, status_code=202)
def seed_bible(
    project_id: str,
    req: SeedRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """Bible seed：批量录入世界观 facts 进 staging，可选自动批准低风险条目。

    等价于 POST /capture + 可选 POST /reviews/batch_approve。
    """
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    conn = registry.open_conn(project_id)
    try:
        from ...ids import new_id

        candidate_ids: list[str] = []
        for p in req.proposals:
            cid = new_id("cand")
            prop_dict = {
                "op": p.op,
                "fact_type": p.fact_type,
                "new": p.new or {},
                "valid_from_chapter": p.valid_from_chapter,
            }
            if p.entity:
                prop_dict["entity"] = p.entity
            conn.execute(
                "INSERT INTO fact_candidates"
                "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (cid, p.op, None, p.fact_type, json.dumps(prop_dict, ensure_ascii=False),
                 "proposed", p.risk_tier, p.valid_from_chapter),
            )
            candidate_ids.append(cid)
        conn.commit()

        auto_approved: list[str] = []
        queued: list[str] = []

        if req.auto_approve_low_risk:
            from ...governance.commit import commit_canon
            from ...contracts import FactCandidate
            from ...db.write import with_retry

            for i, cid in enumerate(candidate_ids):
                p = req.proposals[i]
                if p.risk_tier != "low":
                    queued.append(cid)
                    continue
                row = conn.execute(
                    "SELECT * FROM fact_candidates WHERE candidate_id=?", (cid,)
                ).fetchone()
                if row is None:
                    queued.append(cid)
                    continue
                try:
                    cand = FactCandidate(
                        candidate_id=row["candidate_id"],
                        entity_id=row["entity_id"],
                        fact_type=row["fact_type"],
                        proposal_json=row["proposal_json"],
                        status=row["status"],
                        risk_tier=row["risk_tier"],
                        source_chapter=row["source_chapter"] or 0,
                        target_fact_id=row["target_fact_id"],
                        evidence_refs=row["evidence_refs"],
                    )
                    with_retry(lambda c=cand: commit_canon(c, conn, policy_mode="human_gate", actor=req.actor))
                    auto_approved.append(cid)
                except Exception:
                    queued.append(cid)
            conn.commit()
        else:
            queued = candidate_ids[:]

        return SeedResponse(
            candidate_ids=candidate_ids,
            auto_approved=auto_approved,
            queued=queued,
        )
    finally:
        conn.close()
