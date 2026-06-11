"""控制平面端点：/pipeline/run + 历史 + SSE 流式。"""
from __future__ import annotations

import json as _json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..deps import ProjectRegistry, get_registry
from ..models import (
    BudgetSpent,
    NextChapterSuggestion,
    PipelineRunDetail, PipelineRunRecord,
    PipelineRunRequest, PipelineRunResponse,
    StageResult,
)
from ...config import NovelForgeConfig
from ...control_plane.budget import CircuitTripped
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


def _make_cfg(project_id: str, req: PipelineRunRequest, registry: ProjectRegistry) -> NovelForgeConfig:
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
    if req.n_candidates:
        cfg.candidates.n_candidates = max(1, min(3, req.n_candidates))
    if req.quality_check is not None:
        cfg.quality.enabled = req.quality_check
    api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        cfg.provider.api_key = api_key
        cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
    return cfg


@router.post("/{project_id}/pipeline/run", response_model=PipelineRunResponse)
def pipeline_run(
    project_id: str,
    req: PipelineRunRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ..security import sanitize_user_text

    cfg = _make_cfg(project_id, req, registry)
    conn = registry.open_conn(project_id)
    stages: list[StageResult] = []
    stage_acc: list[StageResult] = []

    def on_stage(stage: str, status: str, detail: dict) -> None:
        stage_acc.append(StageResult(stage=stage, status=status, detail=detail))

    try:
        orch = _build_orch(cfg)
        outcome = orch.generate_chapter(
            req.chapter_no, conn,
            chapter_goal=sanitize_user_text(req.chapter_goal or ""),
            entity_ids=req.entity_ids,
            keyword_query=sanitize_user_text(req.keyword_query or "") if req.keyword_query else None,
            progress_cb=on_stage,
        )

        stages = stage_acc or [
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
            run_id=outcome.run_id or new_id("run"),
            chapter_no=req.chapter_no,
            stages=stages,
            final_gate=final_gate,
            draft_text=outcome.draft_text,
            budget_spent=BudgetSpent(tokens=outcome.usage_tokens, usd=outcome.usage_usd),
            circuit_breaker_tripped=False,
            quality_score=getattr(outcome, "quality_score", None),
            cache_read_tokens=getattr(outcome, "cache_read_tokens", 0),
            error=outcome.error,
        )
    except CircuitTripped as e:
        return PipelineRunResponse(
            run_id=new_id("run"),
            chapter_no=req.chapter_no,
            stages=stage_acc + [StageResult(stage="circuit_breaker",
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
            run_id=new_id("run"), chapter_no=req.chapter_no, stages=stage_acc,
            final_gate="error",
            budget_spent=BudgetSpent(tokens=0, usd=0.0),
            error=str(e),
        )
    finally:
        conn.close()


@router.post("/{project_id}/pipeline/run/stream")
async def pipeline_run_stream(
    project_id: str,
    req: PipelineRunRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """SSE 流式生成端点：每个 stage 完成后即时推送，最后推送 done 事件（含完整 draft_text）。"""
    import asyncio
    from ..security import sanitize_user_text

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _push(data: dict) -> None:
        line = f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"
        loop.call_soon_threadsafe(queue.put_nowait, line)

    def run_sync() -> None:
        cfg = _make_cfg(project_id, req, registry)
        conn = registry.open_conn(project_id)

        def on_stage(stage: str, status: str, detail: dict) -> None:
            _push({"event": "stage", "stage": stage, "status": status, "detail": detail})

        try:
            orch = _build_orch(cfg)
            outcome = orch.generate_chapter(
                req.chapter_no, conn,
                chapter_goal=sanitize_user_text(req.chapter_goal or ""),
                entity_ids=req.entity_ids,
                keyword_query=sanitize_user_text(req.keyword_query or "") if req.keyword_query else None,
                progress_cb=on_stage,
            )
            final_gate = (
                "committed_canon" if outcome.fact_ids_committed
                else "enqueued_review" if outcome.candidates_queued
                else "no_candidates"
            )
            _push({
                "event": "done",
                "run_id": outcome.run_id or new_id("run"),
                "chapter_no": req.chapter_no,
                "draft_text": outcome.draft_text,
                "final_gate": final_gate,
                "tokens": outcome.usage_tokens,
                "usd": outcome.usage_usd,
                "cache_read_tokens": getattr(outcome, "cache_read_tokens", 0),
                "quality_score": getattr(outcome, "quality_score", None),
                "error": outcome.error,
            })
        except CircuitTripped as e:
            _push({"event": "error", "message": str(e), "type": "circuit_broken"})
        except Exception as e:
            _push({"event": "error", "message": str(e)})
        finally:
            conn.close()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    executor = ThreadPoolExecutor(max_workers=1)
    loop.run_in_executor(executor, run_sync)

    async def event_gen():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{project_id}/pipeline/next", response_model=NextChapterSuggestion)
def pipeline_next(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """「下一章」自动建议：算出下一章号 + 最优 chapter_goal。

    章号 = 已完成生成（pipeline_run completed ∪ draft_index）的最大章 + 1；
    目标按优先级拼装：本章章节卡 → 上一章钩子 → 所属卷大纲 → 到期伏笔 → 已计划节拍。
    节拍器（pacing）建议由 generate_chapter 内部自动追加，此处不重复。
    """
    from ..chapter_suggest import assemble_chapter_goal, next_chapter_no

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        nxt, last = next_chapter_no(conn, project_id)
        goal, sources = assemble_chapter_goal(conn, nxt)
        return NextChapterSuggestion(
            next_chapter=nxt,
            last_completed_chapter=last,
            suggested_goal=goal,
            sources=sources,
        )
    finally:
        conn.close()


@router.get("/{project_id}/pipeline/runs", response_model=list[PipelineRunRecord])
def list_pipeline_runs(
    project_id: str,
    limit: int = 30,
    registry: ProjectRegistry = Depends(get_registry),
):
    """列出生成历史（按时间倒序，最多 limit 条）。"""
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT pr.run_id, pr.chapter, pr.status, pr.started_at, pr.finished_at,"
            "       pr.quality_score, di.word_count"
            " FROM pipeline_run pr"
            " LEFT JOIN draft_index di ON di.id = pr.draft_id"
            " WHERE pr.project_id = ?"
            " ORDER BY pr.started_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [
            PipelineRunRecord(
                run_id=r["run_id"],
                chapter=r["chapter"],
                status=r["status"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                word_count=r["word_count"],
                quality_score=r["quality_score"],
            )
            for r in rows
        ]
    finally:
        conn.close()


@router.get("/{project_id}/pipeline/runs/{run_id}", response_model=PipelineRunDetail)
def get_pipeline_run(
    project_id: str,
    run_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """查看单次生成详情（含完整草稿正文）。"""
    from pathlib import Path

    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT pr.run_id, pr.chapter, pr.status, pr.started_at, pr.finished_at,"
            "       di.word_count, di.file_path"
            " FROM pipeline_run pr"
            " LEFT JOIN draft_index di ON di.id = pr.draft_id"
            " WHERE pr.run_id = ? AND pr.project_id = ?",
            (run_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "run not found")

        draft_text = ""
        if row["file_path"]:
            db_entry = registry.get(project_id)
            if db_entry:
                l0_path = Path(db_entry.db_path).parent / row["file_path"]
                if l0_path.exists():
                    draft_text = l0_path.read_text(encoding="utf-8")

        return PipelineRunDetail(
            run_id=row["run_id"],
            chapter=row["chapter"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            word_count=row["word_count"],
            draft_text=draft_text,
        )
    finally:
        conn.close()
