"""治理平面端点：/check /reviews /revert。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import ProjectRegistry, get_registry
from ..models import (
    ApproveRequest, ApproveResponse,
    BatchApproveRequest, BatchApproveResponse,
    CheckRequest,
    RejectRequest,
    ReviewQueueItem,
    RevertRequest, RevertResponse,
)
from ...contracts import FactCandidate, RunContext
from ...governance.revert import revert_fact
from ...db.write import with_retry
from ...governance.commit import commit_canon

router = APIRouter(tags=["governance"])


# ── /check/continuity ─────────────────────────────────────────────────────────

@router.post("/{project_id}/check/continuity")
def check_continuity(
    project_id: str,
    req: CheckRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        from ...control_plane.skill_base import SkillContext
        from ...control_plane.llm.fake_provider import FakeProvider
        from ...control_plane.llm.gateway import LLMGateway
        from ...control_plane.budget import BudgetLedger
        from ...skills.continuity_check_skill import ContinuityCheckSkill

        ws = {"draft_text": req.draft_text, "beats": req.beats, "proposals": req.proposals}
        ledger = BudgetLedger()
        gw = LLMGateway(provider=FakeProvider(), ledger=ledger, model_map={})
        ctx = SkillContext(
            project_id=project_id, target_chapter=req.chapter_no, mode="human_gate",
            as_of_chapter=req.chapter_no - 1, budget=ledger,
            llm=gw, conn=conn, workspace=ws,
        )
        result = ContinuityCheckSkill().run(ctx)
        return {
            "ok": result.ok,
            "hard_issues": [i for i in ws.get("continuity_issues", [])
                            if i.get("severity") == "block"],
            "soft_issues": [i for i in ws.get("continuity_issues", [])
                            if i.get("severity") != "block"],
            "state_reachable": result.ok,
        }
    finally:
        conn.close()


# ── /check/craft ──────────────────────────────────────────────────────────────

@router.post("/{project_id}/check/craft")
def check_craft(
    project_id: str,
    req: CheckRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        from ...control_plane.skill_base import SkillContext
        from ...control_plane.llm.fake_provider import FakeProvider
        from ...control_plane.llm.gateway import LLMGateway
        from ...control_plane.budget import BudgetLedger
        from ...skills.craft_check_skill import CraftCheckSkill

        ws = {"draft_text": req.draft_text, "beats": req.beats, "proposals": req.proposals}
        ledger = BudgetLedger()
        gw = LLMGateway(provider=FakeProvider(), ledger=ledger, model_map={})
        ctx = SkillContext(
            project_id=project_id, target_chapter=req.chapter_no, mode="human_gate",
            as_of_chapter=req.chapter_no - 1, budget=ledger,
            llm=gw, conn=conn, workspace=ws,
        )
        result = CraftCheckSkill().run(ctx)
        return {
            "ok": result.ok,
            "craft_issues": ws.get("craft_issues", []),
        }
    finally:
        conn.close()


# ── /reviews ──────────────────────────────────────────────────────────────────

@router.get("/{project_id}/reviews", response_model=list[ReviewQueueItem])
def list_reviews(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT fc.candidate_id, fc.fact_type, fc.risk_tier, fc.status,"
            "  rq.reason, fc.proposal_json, fc.source_chapter, fc.created_at"
            " FROM review_queue rq"
            " JOIN fact_candidates fc ON fc.candidate_id=rq.candidate_id"
            " WHERE rq.status='pending'"
            " ORDER BY rq.priority ASC, rq.enqueued_at ASC"
        ).fetchall()
        return [
            ReviewQueueItem(
                candidate_id=r["candidate_id"],
                fact_type=r["fact_type"],
                risk_tier=r["risk_tier"],
                status=r["status"],
                reason=r["reason"],
                proposal_json=r["proposal_json"],
                source_chapter=r["source_chapter"] or 0,
                created_at=r["created_at"],
            )
            for r in rows
        ]
    finally:
        conn.close()


@router.get("/{project_id}/staging", response_model=list[ReviewQueueItem])
def list_staging(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """暂存待审：列 fact_candidates 中 status IN ('proposed','pending_review') 的条目。

    seed 的非低风险 / 自动晋升失败条目停在此处（不进 review_queue）。
    与 /reviews（review_queue pending，来自 pipeline gate）互补；
    approve/reject 端点对二者皆可操作。
    """
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT candidate_id, fact_type, risk_tier, status,"
            "  proposal_json, source_chapter, created_at"
            " FROM fact_candidates"
            " WHERE status IN ('proposed','pending_review')"
            " ORDER BY created_at ASC"
        ).fetchall()
        return [
            ReviewQueueItem(
                candidate_id=r["candidate_id"],
                fact_type=r["fact_type"],
                risk_tier=r["risk_tier"],
                status=r["status"],
                reason="待人工处理（暂存区）",
                proposal_json=r["proposal_json"],
                source_chapter=r["source_chapter"] or 0,
                created_at=r["created_at"],
            )
            for r in rows
        ]
    finally:
        conn.close()


@router.post("/{project_id}/reviews/{candidate_id}/approve", response_model=ApproveResponse)
def approve_review(
    project_id: str,
    candidate_id: str,
    req: ApproveRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM fact_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"候选不存在: {candidate_id}")
        if row["status"] not in ("proposed", "pending_review"):
            raise HTTPException(409, f"候选状态不可晋升: {row['status']}")

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
        ctx = RunContext(conn=conn, policy_mode="human_gate", actor=req.actor)
        try:
            fact_id = with_retry(
                lambda c=cand: commit_canon(c, conn, policy_mode="human_gate", actor=req.actor)
            )
        except HTTPException:
            raise
        except Exception as e:
            # commit_canon 失败（如 item 需先有 entity）→ 收敛为 422，
            # 而非裸 500，便于前端展示可读错误。
            conn.rollback()
            raise HTTPException(422, f"晋升失败：{e}")
        # 更新 review_queue
        conn.execute(
            "UPDATE review_queue SET status='approved', resolved_at=datetime('now')"
            " WHERE candidate_id=?", (candidate_id,)
        )
        conn.commit()
        return ApproveResponse(candidate_id=candidate_id, fact_id=fact_id)
    finally:
        conn.close()


@router.post("/{project_id}/reviews/{candidate_id}/reject", status_code=204)
def reject_review(
    project_id: str,
    candidate_id: str,
    req: RejectRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT candidate_id FROM fact_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"候选不存在: {candidate_id}")
        conn.execute(
            "UPDATE fact_candidates SET status='rejected', decided_at=datetime('now')"
            " WHERE candidate_id=?", (candidate_id,)
        )
        conn.execute(
            "UPDATE review_queue SET status='rejected', resolved_at=datetime('now')"
            " WHERE candidate_id=?", (candidate_id,)
        )
        from ...governance.commit import _insert_promotion_log
        _insert_promotion_log(
            conn, candidate_id=candidate_id, fact_id=None, entity_id=None,
            decision="reject", policy_mode="human_gate", risk_tier="low",
            reason=req.reason, actor=req.actor,
        )
        conn.commit()
    finally:
        conn.close()


@router.post("/{project_id}/reviews/batch_approve", response_model=BatchApproveResponse)
def batch_approve(
    project_id: str,
    req: BatchApproveRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    approved: list[ApproveResponse] = []
    skipped: list[dict] = []
    try:
        for cid in req.candidate_ids:
            row = conn.execute(
                "SELECT * FROM fact_candidates WHERE candidate_id=?", (cid,)
            ).fetchone()
            if row is None:
                skipped.append({"candidate_id": cid, "reason": "not_found"})
                continue
            if row["status"] not in ("proposed", "pending_review"):
                skipped.append({"candidate_id": cid, "reason": f"status={row['status']}"})
                continue
            # 检查冲突
            if req.require_no_conflict and row["conflict_flags"]:
                import json as _json
                flags = _json.loads(row["conflict_flags"] or "[]")
                if flags:
                    skipped.append({"candidate_id": cid, "reason": "has_conflict_flags"})
                    continue
            try:
                cand = FactCandidate(
                    candidate_id=row["candidate_id"], entity_id=row["entity_id"],
                    fact_type=row["fact_type"], proposal_json=row["proposal_json"],
                    status=row["status"], risk_tier=row["risk_tier"],
                    source_chapter=row["source_chapter"] or 0,
                    target_fact_id=row["target_fact_id"],
                    evidence_refs=row["evidence_refs"],
                )
                fact_id = with_retry(
                    lambda c=cand: commit_canon(c, conn, policy_mode="human_gate", actor=req.actor)
                )
                conn.execute(
                    "UPDATE review_queue SET status='approved', resolved_at=datetime('now')"
                    " WHERE candidate_id=?", (cid,)
                )
                approved.append(ApproveResponse(candidate_id=cid, fact_id=fact_id))
            except Exception as e:
                skipped.append({"candidate_id": cid, "reason": str(e)})
        conn.commit()
    finally:
        conn.close()
    return BatchApproveResponse(approved=approved, skipped=skipped)


# ── /revert ───────────────────────────────────────────────────────────────────

@router.post("/{project_id}/facts/{fact_id}/revert", response_model=RevertResponse)
def revert(
    project_id: str,
    fact_id: str,
    req: RevertRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        result = revert_fact(fact_id, conn, actor=req.actor, reason=req.reason,
                             revert_to_revision_id=req.revert_to_revision_id)
        conn.commit()
        return RevertResponse(**result)
    except ValueError as e:
        raise HTTPException(404, str(e))
    finally:
        conn.close()
