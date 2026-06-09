"""控制平面端点：/pipeline/run。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import ProjectRegistry, get_registry
from ..models import (
    BudgetSpent,
    PipelineRunRequest, PipelineRunResponse,
    StageResult,
)
from ...config import NovelForgeConfig
from ...control_plane.budget import BudgetLedger, CircuitTripped
from ...control_plane.llm.factory import build_gateway
from ...control_plane.orchestrator import Orchestrator
from ...control_plane.skill_registry import SkillRegistry
from ...ids import new_id
from ...skills import register_default_skills

router = APIRouter(tags=["orchestrator"])


def _build_orch(cfg: NovelForgeConfig) -> Orchestrator:
    gw = build_gateway(cfg)
    reg = SkillRegistry()
    register_default_skills(reg)
    return Orchestrator(gw, reg, cfg)


@router.post("/{project_id}/pipeline/run", response_model=PipelineRunResponse)
def pipeline_run(
    project_id: str,
    req: PipelineRunRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...config import NovelForgeConfig
    import os

    cfg = NovelForgeConfig(
        project_id=project_id,
        db_path=registry.get(project_id).db_path if registry.get(project_id) else "novel.db",
    )
    if req.mode:
        cfg.governance.mode = req.mode
    if req.budget_max_tokens:
        cfg.budget.max_tokens_per_chapter = req.budget_max_tokens
    if req.budget_max_usd:
        cfg.budget.max_usd_per_chapter = req.budget_max_usd

    # API key from environment
    api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        cfg.provider.api_key = api_key
        provider_name = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
        cfg.provider.provider = provider_name

    from ..security import sanitize_user_text
    conn = registry.open_conn(project_id)
    run_id = new_id("run")
    stages: list[StageResult] = []
    tripped = False

    try:
        orch = _build_orch(cfg)
        outcome = orch.generate_chapter(
            req.chapter_no, conn,
            chapter_goal=sanitize_user_text(req.chapter_goal or ""),
            entity_ids=req.entity_ids,
            keyword_query=sanitize_user_text(req.keyword_query or "") if req.keyword_query else None,
        )

        stages = [
            StageResult(stage="plan",   status="ok", detail={}),
            StageResult(stage="recall", status="ok", detail={}),
            StageResult(stage="draft",  status="ok" if outcome.ok else "blocked",
                        detail={"chars": len(outcome.draft_text)}),
            StageResult(stage="check",  status="ok",
                        detail={"issues": len(outcome.issues)}),
            StageResult(stage="gate",   status="ok",
                        detail={"committed": len(outcome.fact_ids_committed),
                                "queued": len(outcome.candidates_queued)}),
        ]
        final_gate = (
            "committed_canon" if outcome.fact_ids_committed
            else "enqueued_review" if outcome.candidates_queued
            else "no_candidates"
        )
        return PipelineRunResponse(
            run_id=run_id,
            chapter_no=req.chapter_no,
            stages=stages,
            final_gate=final_gate,
            draft_text=outcome.draft_text,
            budget_spent=BudgetSpent(
                tokens=outcome.usage_tokens,
                usd=outcome.usage_usd,
            ),
            circuit_breaker_tripped=False,
            error=outcome.error,
        )
    except CircuitTripped as e:
        return PipelineRunResponse(
            run_id=run_id,
            chapter_no=req.chapter_no,
            stages=stages + [StageResult(stage="circuit_breaker",
                                          status="circuit_broken",
                                          detail={"reason": e.reason,
                                                  "spent": e.spent, "cap": e.cap})],
            final_gate="circuit_broken",
            budget_spent=BudgetSpent(tokens=0, usd=0.0),
            circuit_breaker_tripped=True,
            error=str(e),
        )
    except Exception as e:
        return PipelineRunResponse(
            run_id=run_id, chapter_no=req.chapter_no, stages=stages,
            final_gate="error",
            budget_spent=BudgetSpent(tokens=0, usd=0.0),
            error=str(e),
        )
    finally:
        conn.close()
