"""控制平面端点：/pipeline/run + 历史 + SSE 流式。"""
from __future__ import annotations

import json as _json
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
from ...control_plane.llm.runtime import build_configured_gateway
from ...control_plane.orchestrator import Orchestrator
from ...control_plane.skill_registry import SkillRegistry
from ...ids import new_id
from ...skills import register_default_skills

router = APIRouter(tags=["orchestrator"])


def _build_orch(cfg: NovelForgeConfig) -> Orchestrator:
    gw = build_configured_gateway(cfg)
    reg = SkillRegistry()
    register_default_skills(reg)
    return Orchestrator(gw, reg, cfg)


def _make_cfg(project_id: str, req: PipelineRunRequest, registry: ProjectRegistry) -> NovelForgeConfig:
    from ...control_plane.llm.runtime import build_project_config

    project = registry.get(project_id)
    db_path = project.db_path if project else "novel.db"
    settings_conn = registry.open_conn(project_id) if project else None
    try:
        cfg = build_project_config(project_id, db_path, settings_conn)
    finally:
        if settings_conn is not None:
            settings_conn.close()
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
    return cfg


def _apply_embedding_env(cfg: NovelForgeConfig) -> None:
    """兼容入口：委托集中式 config.apply_embedding_env。"""
    from ...config import apply_embedding_env
    apply_embedding_env(cfg)


def _resolved_chapter_goal(conn, req: PipelineRunRequest) -> str:
    """Manual API calls get the same plan-aware default as Autopilot/Agent."""
    from ..chapter_suggest import assemble_chapter_goal
    from ..security import sanitize_user_text

    explicit = sanitize_user_text(req.chapter_goal or "")
    if explicit:
        return explicit
    goal, _ = assemble_chapter_goal(conn, req.chapter_no)
    return goal


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
            chapter_goal=_resolved_chapter_goal(conn, req),
            entity_ids=req.entity_ids,
            keyword_query=sanitize_user_text(req.keyword_query or "") if req.keyword_query else None,
            interaction_mode=req.interaction_mode,
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
            "state_degraded" if getattr(outcome, "state_degraded", False)
            else "committed_canon" if outcome.fact_ids_committed
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
            quality_dimensions=getattr(outcome, "quality_dimensions", None),
            cache_read_tokens=getattr(outcome, "cache_read_tokens", 0),
            alignment_report=getattr(outcome, "alignment_report", None),
            recommended_plan_action=getattr(
                outcome, "recommended_plan_action", None
            ),
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
                chapter_goal=_resolved_chapter_goal(conn, req),
                entity_ids=req.entity_ids,
                keyword_query=sanitize_user_text(req.keyword_query or "") if req.keyword_query else None,
                interaction_mode=req.interaction_mode,
                progress_cb=on_stage,
            )
            final_gate = (
                "state_degraded" if getattr(outcome, "state_degraded", False)
                else "committed_canon" if outcome.fact_ids_committed
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
                "quality_dimensions": getattr(outcome, "quality_dimensions", None),
                "alignment_report": getattr(outcome, "alignment_report", None),
                "recommended_plan_action": getattr(
                    outcome, "recommended_plan_action", None
                ),
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


def _payoff_closed_chapters(conn) -> set[int]:
    """P1#10 爽点闭环章集合（纯确定性证据，零 LLM）。

    任一即闭环：伏笔 payoff / 正向实力变动（breakthrough/unseal）/ 道具入手（acquire/craft）。
    injury_drop/seal/init、transfer/consume/destroy/lose 不算爽点。
    """
    closed: set[int] = set()
    for sql in (
        "SELECT DISTINCT chapter AS c FROM foreshadow_log WHERE action='payoff'",
        "SELECT DISTINCT change_chapter AS c FROM character_power_log"
        " WHERE change_type IN ('breakthrough','unseal')",
        "SELECT DISTINCT change_chapter AS c FROM item_log"
        " WHERE change_type IN ('acquire','craft')",
    ):
        try:
            closed |= {r["c"] for r in conn.execute(sql).fetchall()}
        except Exception:
            continue   # 老库缺表 → 该证据源退出口径
    return closed


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
            "SELECT pr.chapter, pr.quality_score, pr.finished_at, di.word_count,"
            "       pr.tokens_spent, pr.usd_spent"
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

        closed = _payoff_closed_chapters(conn)
        series = [ChapterStat(
            chapter=r["chapter"], word_count=r["word_count"],
            quality_score=r["quality_score"], finished_at=r["finished_at"],
            tokens_spent=r["tokens_spent"], usd_spent=r["usd_spent"],
            payoff_closed=r["chapter"] in closed,
        ) for r in rows]
        scores = [s.quality_score for s in series if s.quality_score is not None]
        return PipelineStats(
            series=series,
            chapters_completed=len(series),
            total_words=sum(s.word_count or 0 for s in series),
            avg_quality_score=round(sum(scores) / len(scores), 2) if scores else None,
            low_quality_count=sum(1 for s in scores if s < threshold),
            min_score_threshold=threshold,
            total_tokens_spent=sum(s.tokens_spent or 0 for s in series),
            total_usd_spent=round(sum(s.usd_spent or 0.0 for s in series), 4),
            payoff_loop_rate=(round(sum(1 for s in series if s.payoff_closed)
                                    / len(series), 3) if series else None),
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
            "       pr.quality_score, pr.tokens_spent, pr.usd_spent, di.word_count"
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
                tokens_spent=r["tokens_spent"],
                usd_spent=r["usd_spent"],
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
        "       pr.quality_score, pr.tokens_spent, pr.usd_spent, pr.detail_json,"
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

    candidates: list[CandidateInfo] = []
    winner_index = None
    selected_by = None
    patch_stats = None
    quality_dimensions = None
    state_degraded = False
    foreshadow_settle = None
    plan_reconcile = None
    alignment_report = None
    recommended_plan_action = None
    if row["detail_json"]:
        try:
            detail = _json.loads(row["detail_json"])
            winner_index = detail.get("winner")
            selected_by = detail.get("selected_by")
            patch_stats = detail.get("patch_stats")
            quality_dimensions = detail.get("quality_dimensions")
            state_degraded = bool(detail.get("state_degraded"))
            foreshadow_settle = detail.get("foreshadow_settle")
            plan_reconcile = detail.get("plan_reconcile")
            alignment_report = detail.get("alignment_report")
            recommended_plan_action = detail.get("recommended_plan_action")
            scores = detail.get("scores") or []
            hard_blocks = detail.get("hard_blocks") or []
            continuity_blocks = detail.get("continuity_blocks") or []
            plan_blocks = detail.get("plan_blocks") or []
            contract_scores = detail.get("contract_scores") or []
            arc_progress_scores = detail.get("arc_progress_scores") or []
            quality_scores = detail.get("quality_scores") or scores
            for i, c in enumerate(detail.get("candidates") or []):
                text = c.get("draft_text", "")
                candidates.append(CandidateInfo(
                    index=i,
                    draft_text=text,
                    length=len(text),
                    score=scores[i] if i < len(scores) else None,
                    hard_blocks=hard_blocks[i] if i < len(hard_blocks) else 0,
                    continuity_blocks=(
                        continuity_blocks[i] if i < len(continuity_blocks) else 0
                    ),
                    plan_blocks=plan_blocks[i] if i < len(plan_blocks) else 0,
                    contract_score=(
                        contract_scores[i] if i < len(contract_scores) else None
                    ),
                    arc_progress_score=(
                        arc_progress_scores[i]
                        if i < len(arc_progress_scores) else None
                    ),
                    quality_score=(
                        quality_scores[i] if i < len(quality_scores) else None
                    ),
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
        tokens_spent=row["tokens_spent"],
        usd_spent=row["usd_spent"],
        draft_text=draft_text,
        candidates=candidates,
        winner_index=winner_index,
        selected_by=selected_by,
        patch_stats=patch_stats,
        quality_dimensions=quality_dimensions,
        state_degraded=state_degraded,
        foreshadow_settle=foreshadow_settle,
        plan_reconcile=plan_reconcile,
        alignment_report=alignment_report,
        recommended_plan_action=recommended_plan_action,
    )


@router.post("/{project_id}/pipeline/runs/{run_id}/select-candidate",
             response_model=PipelineRunDetail)
def select_candidate(
    project_id: str,
    run_id: str,
    req: SelectCandidateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """人工确认候选，并以最终正文重跑完整校验与计划对账。"""
    from ...control_plane.budget import BudgetLedger
    from ...control_plane.llm.runtime import (
        build_project_config,
        build_project_gateway,
    )
    from ...control_plane.orchestrator import (
        _persist_chapter_summary,
        _persist_draft,
        _proposals_to_candidates,
        validate_final_candidate,
    )
    from ...control_plane.skill_base import SkillContext
    from ...control_plane.skill_registry import SkillRegistry
    from ...craft.pacing import PacingController
    from ...memory.l1_atoms import persist_proposal_atoms
    from ...memory.recall import gather_hard_context
    from ...planning.alignment import (
        evaluate_and_persist_alignment,
        reconcile_plan,
    )
    from ...planning.context import gather_plan_context
    from ...planning.service import _planning_entity_ids
    from ...skills import register_default_skills
    from ...world.replay import get_world_state
    from ...writing_profiles import get_project_writing_profile

    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT chapter, draft_id, detail_json FROM pipeline_run"
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

        if idx == detail.get("winner"):
            # Confirming the already-durable winner is a metadata-only author
            # acknowledgement.  Re-running today's validators against an old
            # run could reject prose that has already been committed.
            if detail.get("selected_by") != "human":
                detail["selected_by"] = "human"
                conn.execute(
                    "UPDATE pipeline_run SET detail_json=? WHERE run_id=?",
                    (_json.dumps(detail, ensure_ascii=False), run_id),
                )
                conn.commit()
            return _load_run_detail(conn, registry, project_id, run_id)

        chapter = row["chapter"]
        chosen = cands[idx]
        chosen_text = chosen.get("draft_text", "")
        if not chosen_text:
            raise HTTPException(422, "选中候选正文为空")

        db_entry = registry.get(project_id)
        db_path = db_entry.db_path if db_entry else "novel.db"
        cfg = build_project_config(project_id, db_path, conn)
        gateway = build_project_gateway(
            project_id, db_path, conn, allow_missing=True
        )
        ledger = gateway.ledger if gateway is not None else BudgetLedger(
            max_tokens=cfg.budget.max_tokens_per_chapter,
            max_usd=cfg.budget.max_usd_per_chapter,
        )
        plan_context = gather_plan_context(conn, chapter, lookahead_chapters=5)
        selected_interaction_mode = str(
            ((detail.get("alignment_report") or {}).get("detail") or {}).get(
                "interaction_mode"
            )
            or "guided"
        )
        entity_ids = _planning_entity_ids(
            conn, start_chapter=chapter, horizon=1, limit=20
        )
        recall_pack = gather_hard_context(
            entity_ids,
            chapter - 1,
            conn,
            max_keywords=cfg.recall.max_keywords,
            context_window=cfg.recall.context_window_chapters,
            summary_window=cfg.recall.summary_window_chapters,
            enable_summaries=cfg.recall.enable_summaries,
        )
        writing_profile = get_project_writing_profile(conn, chapter=chapter)
        content_language = writing_profile["content_language"]
        stable = recall_pack.to_stable_context_str(content_language)
        validation_beats = list(
            detail.get("beats") or plan_context.selected_beats or []
        )
        validation_workspace = {
            "chapter_goal": (
                (plan_context.current_contract or {}).get("goal") or ""
            ),
            "beats": validation_beats,
            "plan_context": plan_context,
            "world_state": get_world_state(chapter - 1, conn),
            "recall_pack": recall_pack,
            "stable_context": (
                (f"## Stable world context\n{stable}"
                 if content_language.startswith("en")
                 else f"## 世界设定（稳定）\n{stable}")
                if stable else ""
            ),
            "dynamic_context": recall_pack.to_dynamic_context_str(content_language),
            "pacing_state": PacingController().get_state(conn),
            "proposals": list(chosen.get("proposals") or []),
            "writing_profile": writing_profile,
        }
        skill_registry = SkillRegistry()
        register_default_skills(skill_registry)
        check_ctx = SkillContext(
            project_id=project_id,
            target_chapter=chapter,
            mode=cfg.governance.mode,
            as_of_chapter=chapter - 1,
            budget=ledger,
            llm=gateway,
            conn=conn,
            workspace=validation_workspace,
            extra={"writing_profile": writing_profile},
        )

        # New candidates are pure prose.  Extract state-change proposals once,
        # and only for the author-selected final text, before payoff validation.
        if not validation_workspace["proposals"] and gateway is not None:
            validation_workspace["source_text"] = chosen_text
            validation_workspace["chapter_no"] = chapter
            try:
                skill_registry.invoke("cold_extract", check_ctx)
            except Exception:
                validation_workspace["proposals"] = []

        validation = validate_final_candidate(
            check_ctx,
            chosen_text,
            plan_context=plan_context,
            proposals=validation_workspace.get("proposals") or [],
            registry=skill_registry,
            score_quality=gateway is not None,
            judge_tier=getattr(cfg.candidates, "judge_tier", "mid"),
        )
        if not validation_beats:
            # Legacy runs did not persist their generated beat sheet.  Missing
            # evidence is not proof that the prose violated a beat contract;
            # retain every check we can actually evaluate.
            unavailable = [
                issue for issue in validation["craft_issues"]
                if issue.get("check") == "beat_contract"
                and "beats 数量=0" in str(issue.get("detail") or "")
            ]
            if unavailable:
                validation["craft_issues"] = [
                    issue for issue in validation["craft_issues"]
                    if issue not in unavailable
                ]
                validation["issues"] = [
                    issue for issue in validation["issues"] if issue not in unavailable
                ]
                validation["hard_blocks"] = [
                    issue for issue in validation["hard_blocks"]
                    if issue not in unavailable
                ]
                validation["hard_block_count"] = len(validation["hard_blocks"])
                validation["ok"] = not validation["hard_blocks"]

        def _set_candidate_metric(name: str, value, default=None) -> None:
            values = list(detail.get(name) or [])
            values.extend(default for _ in range(len(cands) - len(values)))
            values[idx] = value
            detail[name] = values

        continuity_block_count = sum(
            1 for issue in validation["continuity_issues"]
            if issue.get("severity") == "block"
        )
        craft_block_count = sum(
            1 for issue in validation["craft_issues"]
            if issue.get("severity") == "block"
        )
        plan_block_count = len(validation["plan_issues"])
        plan_evaluation = validation["plan_evaluation"]
        _set_candidate_metric(
            "hard_blocks", continuity_block_count + craft_block_count, 0
        )
        _set_candidate_metric("continuity_blocks", continuity_block_count, 0)
        _set_candidate_metric("plan_blocks", plan_block_count, 0)
        _set_candidate_metric(
            "contract_scores", plan_evaluation.get("contract_score"), None
        )
        _set_candidate_metric(
            "arc_progress_scores", plan_evaluation.get("arc_progress_score"), None
        )
        _set_candidate_metric(
            "quality_scores", validation.get("quality_score"), None
        )
        candidate_validations = list(detail.get("candidate_validations") or [])
        candidate_validations.extend(
            None for _ in range(len(cands) - len(candidate_validations))
        )
        candidate_validations[idx] = {
            "ok": validation["ok"],
            "issues": validation["issues"],
            "quality": validation.get("quality"),
            "plan_evaluation": plan_evaluation,
        }
        detail["candidate_validations"] = candidate_validations

        if not validation["ok"]:
            conn.execute(
                "UPDATE pipeline_run SET detail_json=? WHERE run_id=?",
                (_json.dumps(detail, ensure_ascii=False), run_id),
            )
            conn.commit()
            raise HTTPException(422, {
                "message": "候选稿存在硬阻断，未替换当前正文",
                "candidate_index": idx,
                "hard_blocks": validation["hard_blocks"],
                "issues": validation["issues"],
            })

        did_replace = idx != detail.get("winner")
        if did_replace:
            draft_id = _persist_draft(conn, chosen_text, chapter, project_id,
                                      db_path)
            if draft_id is None:
                raise HTTPException(500, "换稿落盘失败")

            # The durable new revision now exists.  Supersede review work tied
            # to the old automatic winner and undo only its milestone effects.
            if row["draft_id"]:
                conn.execute(
                    "UPDATE review_queue SET status='expired',"
                    " resolved_at=datetime('now') WHERE status IN ('pending','in_review')"
                    " AND candidate_id IN (SELECT candidate_id FROM fact_candidates"
                    " WHERE source_draft_id=?)",
                    (row["draft_id"],),
                )
                conn.execute(
                    "UPDATE fact_candidates SET status='superseded'"
                    " WHERE source_draft_id=?"
                    " AND status IN ('proposed','pending_review')",
                    (row["draft_id"],),
                )
            previous_reconcile = detail.get("plan_reconcile") or {}
            for milestone_id in (
                str(item) for item in previous_reconcile.get("completed") or [] if item
            ):
                conn.execute(
                    "UPDATE arc_milestones SET status='planned', completed_chapter=NULL,"
                    " updated_at=datetime('now') WHERE id=? AND status='completed'"
                    " AND completed_chapter=?",
                    (milestone_id, chapter),
                )

            selected_proposals = list(validation.get("proposals") or [])
            candidates = _proposals_to_candidates(
                selected_proposals, chapter, conn, source_draft_id=draft_id
            )
            persist_proposal_atoms(
                conn,
                chapter=chapter,
                draft_id=draft_id,
                draft_text=chosen_text,
                proposals=selected_proposals,
                candidate_ids=[c.candidate_id for c in candidates],
                extracted_by="human_selected_draft_v1",
                commit=False,
            )
            conn.execute(
                "UPDATE pipeline_run SET draft_id=? WHERE run_id=?",
                (draft_id, run_id),
            )
            alignment = evaluate_and_persist_alignment(
                conn,
                plan_context,
                chosen_text,
                chapter,
                draft_id=draft_id,
                seed_proposals=selected_proposals,
                interaction_mode=selected_interaction_mode,
            )
            plan_reconcile = reconcile_plan(
                conn,
                plan_context,
                chosen_text,
                chapter,
                actuals=alignment.actuals,
                report=alignment.report,
                report_id=alignment.report_id,
                interaction_mode=selected_interaction_mode,
                commit=False,
            )
            detail["plan_issues"] = validation["plan_issues"]
            detail["plan_reconcile"] = plan_reconcile
            detail["alignment_report"] = plan_reconcile["alignment_report"]
            detail["alignment_report_id"] = plan_reconcile.get(
                "alignment_report_id"
            )
            detail["recommended_plan_action"] = plan_reconcile[
                "recommended_plan_action"
            ]
            detail["chapter_actuals_id"] = alignment.actuals_id
            if validation.get("quality_score") is not None:
                detail["quality_dimensions"] = (
                    validation.get("quality") or {}
                ).get("dimensions") or {}

        detail["winner"] = idx
        detail["selected_by"] = "human"
        conn.execute(
            "UPDATE pipeline_run SET detail_json=?,"
            " quality_score=COALESCE(?,quality_score) WHERE run_id=?",
            (
                _json.dumps(detail, ensure_ascii=False),
                validation.get("quality_score"),
                run_id,
            ),
        )
        conn.commit()

        # 章摘要与 L2 随稿更新（best-effort）。_persist_draft 已使旧 L2 失效，
        # 因此人工换稿必须立即重建本章，不能等到下一个 N 章触发点。
        try:
            if cfg.recall.enable_summaries and gateway is not None:
                _persist_chapter_summary(conn, gateway, chapter, chosen_text)
            if did_replace and cfg.memory.enable_l2:
                from ...memory.embedding import build_embedder
                from ...memory.l2_aggregate import aggregate_chapter_scenes

                embedder = (
                    build_embedder(cfg.memory, dim=cfg.memory.embedding_dim)
                    if cfg.recall.enable_vector else None
                )
                aggregate_chapter_scenes(
                    conn,
                    gateway,
                    chapter,
                    chosen_text,
                    max_scenes=cfg.memory.l2_max_scenes_per_chapter,
                    tier=cfg.memory.l2_tier,
                    embedder=embedder,
                    preserve_existing_summaries=False,
                    writing_profile=writing_profile,
                )
        except Exception:
            pass

        return _load_run_detail(conn, registry, project_id, run_id)
    finally:
        conn.close()
