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
    ChapterStat, NextChapterSuggestion,
    PipelineRunDetail, PipelineRunRecord,
    PipelineRunRequest, PipelineRunResponse,
    PipelineStats, SelectCandidateRequest,
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


@router.get("/{project_id}/pipeline/stats", response_model=PipelineStats)
def pipeline_stats(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """M6 质量趋势：逐章序列（每章取最新 completed run）+ 汇总指标。"""
    from ...config import QualityConfig

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    threshold = QualityConfig().min_score
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT pr.chapter, pr.quality_score, pr.finished_at, di.word_count"
            " FROM pipeline_run pr"
            " LEFT JOIN draft_index di ON di.id = pr.draft_id"
            " WHERE pr.project_id=? AND pr.status='completed'"
            "   AND pr.started_at = ("
            "       SELECT MAX(p2.started_at) FROM pipeline_run p2"
            "       WHERE p2.chapter=pr.chapter AND p2.project_id=pr.project_id"
            "         AND p2.status='completed')"
            " ORDER BY pr.chapter",
            (project_id,),
        ).fetchall()

        series = [ChapterStat(
            chapter=r["chapter"], word_count=r["word_count"],
            quality_score=r["quality_score"], finished_at=r["finished_at"],
        ) for r in rows]
        scores = [s.quality_score for s in series if s.quality_score is not None]
        return PipelineStats(
            series=series,
            chapters_completed=len(series),
            total_words=sum(s.word_count or 0 for s in series),
            avg_quality_score=round(sum(scores) / len(scores), 2) if scores else None,
            low_quality_count=sum(1 for s in scores if s < threshold),
            min_score_threshold=threshold,
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
    """查看单次生成详情（含完整草稿正文 + 多候选时的全部候选稿）。"""
    conn = registry.open_conn(project_id)
    try:
        return _load_run_detail(conn, registry, project_id, run_id)
    finally:
        conn.close()


def _load_run_detail(conn, registry: ProjectRegistry, project_id: str, run_id: str) -> PipelineRunDetail:
    from pathlib import Path

    from ..models import CandidateInfo

    row = conn.execute(
        "SELECT pr.run_id, pr.chapter, pr.status, pr.started_at, pr.finished_at,"
        "       pr.quality_score, pr.detail_json, di.word_count, di.file_path"
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

    candidates: list[CandidateInfo] = []
    winner_index = None
    selected_by = None
    patch_stats = None
    if row["detail_json"]:
        try:
            detail = _json.loads(row["detail_json"])
            winner_index = detail.get("winner")
            selected_by = detail.get("selected_by")
            patch_stats = detail.get("patch_stats")
            scores = detail.get("scores") or []
            hard_blocks = detail.get("hard_blocks") or []
            for i, c in enumerate(detail.get("candidates") or []):
                text = c.get("draft_text", "")
                candidates.append(CandidateInfo(
                    index=i,
                    draft_text=text,
                    length=len(text),
                    score=scores[i] if i < len(scores) else None,
                    hard_blocks=hard_blocks[i] if i < len(hard_blocks) else 0,
                    is_winner=(i == winner_index),
                    proposal_count=len(c.get("proposals") or []),
                ))
        except Exception:
            pass

    return PipelineRunDetail(
        run_id=row["run_id"],
        chapter=row["chapter"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        word_count=row["word_count"],
        quality_score=row["quality_score"],
        draft_text=draft_text,
        candidates=candidates,
        winner_index=winner_index,
        selected_by=selected_by,
        patch_stats=patch_stats,
    )


@router.post("/{project_id}/pipeline/runs/{run_id}/select-candidate",
             response_model=PipelineRunDetail)
def select_candidate(
    project_id: str,
    run_id: str,
    req: SelectCandidateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """M6：人工换稿（3 选 1）。

    把选中候选作为本章新修订落盘（draft_index revision_round+1），更新 run 指向；
    选中稿的 BibleChangeProposal 以 'proposed' 入 staging 走人审兜底
    （原胜者已入队的提案仍在审核队列，由审稿人最终裁决——账本只追加不回滚）；
    章摘要 best-effort 重新生成。选中当前胜者 = 幂等 no-op。
    """
    import os

    from ...config import NovelForgeConfig
    from ...control_plane.orchestrator import (
        _persist_chapter_summary, _persist_draft, _proposals_to_candidates,
    )

    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT chapter, detail_json FROM pipeline_run"
            " WHERE run_id=? AND project_id=?",
            (run_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "run not found")
        try:
            detail = _json.loads(row["detail_json"] or "{}")
        except Exception:
            detail = {}
        cands = detail.get("candidates") or []
        if not cands:
            raise HTTPException(422, "该 run 无候选稿（单稿生成或旧版本数据）")
        idx = req.candidate_index
        if idx >= len(cands):
            raise HTTPException(422, f"candidate_index 越界: {idx} >= {len(cands)}")

        if idx == detail.get("winner") and detail.get("selected_by") == "human":
            return _load_run_detail(conn, registry, project_id, run_id)  # 幂等

        chapter = row["chapter"]
        chosen = cands[idx]
        chosen_text = chosen.get("draft_text", "")
        if not chosen_text:
            raise HTTPException(422, "选中候选正文为空")

        if idx != detail.get("winner"):
            db_entry = registry.get(project_id)
            draft_id = _persist_draft(conn, chosen_text, chapter, project_id,
                                      db_entry.db_path if db_entry else "novel.db")
            if draft_id is None:
                raise HTTPException(500, "换稿落盘失败")
            # 选中稿的提案入 staging（人审兜底；原胜者提案仍在队列由审稿人裁决）
            _proposals_to_candidates(chosen.get("proposals") or [], chapter, conn)
            conn.execute(
                "UPDATE pipeline_run SET draft_id=? WHERE run_id=?",
                (draft_id, run_id),
            )

        detail["winner"] = idx
        detail["selected_by"] = "human"
        conn.execute(
            "UPDATE pipeline_run SET detail_json=? WHERE run_id=?",
            (_json.dumps(detail, ensure_ascii=False), run_id),
        )
        conn.commit()

        # 章摘要随稿更新（best-effort，需 LLM key；失败静默）
        try:
            db_entry = registry.get(project_id)
            cfg = NovelForgeConfig(project_id=project_id,
                                   db_path=db_entry.db_path if db_entry else "novel.db")
            api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
            if api_key:
                cfg.provider.api_key = api_key
                cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
            from ...control_plane.llm import factory as llm_factory
            gw = llm_factory.build_gateway(cfg)
            _persist_chapter_summary(conn, gw, chapter, chosen_text)
        except Exception:
            pass

        return _load_run_detail(conn, registry, project_id, run_id)
    finally:
        conn.close()
