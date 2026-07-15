"""Planning-window, chapter-contract, alignment, and revision APIs."""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...planning.context import gather_plan_context
from ...planning.replanner import build_replan_context, render_replan_hints
from ...planning.repository import load_decision, resolve_decision
from ...planning.service import PlanningService
from ..deps import ProjectRegistry, get_registry
from ..security import sanitize_user_text


router = APIRouter(tags=["planning"])


class GenerateWindowRequest(BaseModel):
    start_chapter: int = Field(ge=1)
    horizon: int = Field(default=5, ge=1, le=10)
    hints: str | None = None
    candidate_count: int = Field(default=3, ge=1, le=3)


class ApproveWindowRequest(BaseModel):
    option_id: str | None = None
    lock: bool = True
    replace_policy: Literal[
        "preserve_locked", "unlocked_only", "replace_unwritten"
    ] = "replace_unwritten"


class ReplanRequest(BaseModel):
    mode: Literal["patch", "replan"] = "patch"
    horizon: int = Field(default=5, ge=1, le=10)
    preserve_locked: bool = True
    reason: str | None = None
    apply: bool = True


class DraftUpdateRequest(BaseModel):
    draft_text: str = Field(min_length=1)
    base_revision_id: str


def _project(registry: ProjectRegistry, project_id: str):
    project = registry.get(project_id)
    if project is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    return project


@router.post("/{project_id}/planning/windows/generate", status_code=201)
def generate_planning_window(
    project_id: str,
    req: GenerateWindowRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    project = _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        service = PlanningService(db_path=project.db_path)
        window = service.generate_window(
            conn,
            project_id=project_id,
            start_chapter=req.start_chapter,
            horizon=req.horizon,
            source="planning_api",
            hints=(sanitize_user_text(req.hints, max_length=2000) if req.hints else None),
            candidate_count=req.candidate_count,
        )
        if window.error:
            raise HTTPException(503, window.error)
        decision = service.create_pending_decision(
            conn,
            project_id=project_id,
            window=window,
            actor="planning:web",
        )
        return {**window.to_dict(), "decision": decision}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@router.post("/{project_id}/planning/windows/{decision_id}/approve")
def approve_planning_window(
    project_id: str,
    decision_id: str,
    req: ApproveWindowRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        decision = load_decision(conn, decision_id)
        if (
            decision.get("stage") != "chapter_route"
            or decision.get("scope_type") != "chapter"
            or (decision.get("context") or {}).get("project_id") != project_id
        ):
            # A decision id is not a bearer capability: only a planning-route
            # decision created for this project may mutate its chapter plans.
            raise HTTPException(404, "planning decision not found for this project")
        option_id = req.option_id or decision.get("recommended_option_id")
        if not option_id:
            raise HTTPException(422, "decision has no selectable option")
        if decision["status"] == "awaiting_choice":
            # Keep resolution and plan projection in one transaction.  If any
            # chapter fails validation/write, the decision remains selectable.
            decision = resolve_decision(
                conn, decision_id, option_id, commit=False
            )
        elif decision.get("selected_option_id") != option_id:
            raise HTTPException(409, "decision already approved with another option")
        applied = PlanningService().apply_decision(
            conn,
            decision,
            lock=req.lock,
            replace_policy=req.replace_policy,
            source="planning_api",
        )
        conn.commit()
        return {
            "decision_id": decision_id,
            "selected_option_id": option_id,
            "planned": applied.planned,
            "skipped": applied.skipped,
            "version_ids": applied.version_ids,
        }
    except KeyError as exc:
        conn.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@router.get("/{project_id}/chapters/{chapter}/contract")
def get_chapter_contract(
    project_id: str,
    chapter: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        context = gather_plan_context(conn, chapter)
        if context.current_contract is None:
            raise HTTPException(404, f"第 {chapter} 章没有章节契约")
        version = None
        version_id = context.current_contract.get("current_plan_version_id")
        if version_id:
            row = conn.execute(
                "SELECT id, version, status, source_decision_id,"
                " source_alignment_report_id, based_on_chapter, reason, divergence_score,"
                " created_at, activated_at FROM chapter_plan_versions WHERE id=?",
                (version_id,),
            ).fetchone()
            version = dict(row) if row else None
        return {
            "chapter": chapter,
            "contract": context.current_contract,
            "story_plan": context.story_plan,
            "current_volume": context.current_volume,
            "active_arcs": context.active_arcs,
            "due_milestones": context.due_milestones,
            "plan_version": version,
        }
    finally:
        conn.close()


@router.get("/{project_id}/chapters/{chapter}/alignment")
def get_chapter_alignment(
    project_id: str,
    chapter: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM plan_alignment_reports WHERE chapter=? AND status='current'"
            " ORDER BY created_at DESC LIMIT 1",
            (chapter,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"第 {chapter} 章尚无偏航报告")
        report = dict(row)
        report["detail"] = _load_json(report.pop("detail_json"), {})
        actuals = conn.execute(
            "SELECT id, draft_id, payload_json, extractor_version, created_at"
            " FROM chapter_actuals WHERE id=?",
            (report["actuals_id"],),
        ).fetchone()
        report["actuals"] = None
        if actuals:
            report["actuals"] = {
                **{key: actuals[key] for key in ("id", "draft_id", "extractor_version", "created_at")},
                "payload": _load_json(actuals["payload_json"], {}),
            }
        return report
    finally:
        conn.close()


@router.post("/{project_id}/chapters/{chapter}/replan", status_code=201)
def replan_after_chapter(
    project_id: str,
    chapter: int,
    req: ReplanRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    project = _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        reason = sanitize_user_text(req.reason, max_length=2000) if req.reason else None
        replan_context = build_replan_context(
            conn,
            source_chapter=chapter,
            horizon=req.horizon,
            reason=reason,
        )
        service = PlanningService(db_path=project.db_path)
        window = service.generate_window(
            conn,
            project_id=project_id,
            start_chapter=chapter + 1,
            horizon=req.horizon,
            source="replan",
            hints=render_replan_hints(replan_context),
            candidate_count=3,
        )
        if window.error:
            raise HTTPException(503, window.error)
        if not req.apply:
            decision = service.create_pending_decision(
                conn,
                project_id=project_id,
                window=window,
                actor="planning:replan",
                question=f"第 {chapter} 章偏航后采用哪一套未来路线？",
            )
            return {**window.to_dict(), "decision": decision, "applied": None}

        decision = service.create_compatibility_decision(
            conn,
            project_id=project_id,
            window=window,
            actor="planning:replan:auto",
        )
        report = replan_context.get("alignment_report") or {}
        applied = service.apply_decision(
            conn,
            decision,
            lock=True,
            replace_policy=("preserve_locked" if req.preserve_locked else "replace_unwritten"),
            source="replan",
            based_on_chapter=chapter,
            reason=reason or req.mode,
            divergence_score=report.get("divergence_score"),
            source_alignment_report_id=report.get("id"),
        )
        # create_compatibility_decision intentionally leaves resolution open;
        # resolution and every plan projection become durable together.
        conn.commit()
        return {
            **window.to_dict(),
            "decision": decision,
            "applied": {
                "planned": applied.planned,
                "skipped": applied.skipped,
                "version_ids": applied.version_ids,
            },
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@router.get("/{project_id}/chapters/{chapter}/plan-versions")
def get_plan_versions(
    project_id: str,
    chapter: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        out = []
        for row in conn.execute(
            "SELECT * FROM chapter_plan_versions WHERE chapter=?"
            " ORDER BY version DESC",
            (chapter,),
        ).fetchall():
            item = dict(row)
            item["payload"] = _load_json(item.pop("payload_json"), {})
            item["beats"] = [dict(beat) for beat in conn.execute(
                "SELECT seq, beat_type, summary, value_axis, arc_id, constraint_level"
                " FROM chapter_plan_version_beats WHERE plan_version_id=? ORDER BY seq",
                (item["id"],),
            ).fetchall()]
            out.append(item)
        return out
    finally:
        conn.close()


@router.get("/{project_id}/chapters/{chapter}/draft")
def get_chapter_draft(
    project_id: str,
    chapter: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT id, revision_round, word_count, status, created_at FROM draft_index"
            " WHERE chapter=? ORDER BY revision_round DESC LIMIT 1",
            (chapter,),
        ).fetchone()
        if row is None:
            return {
                "chapter": chapter,
                "revision_id": None,
                "revision_round": -1,
                "draft_text": "",
                "word_count": 0,
                "status": "empty",
            }
        from ...db.l0 import load_latest_draft_text

        return {
            "chapter": chapter,
            "revision_id": row["id"],
            "revision_round": row["revision_round"],
            "draft_text": load_latest_draft_text(conn, chapter) or "",
            "word_count": row["word_count"] or 0,
            "status": row["status"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


@router.patch("/{project_id}/chapters/{chapter}/draft")
def update_chapter_draft(
    project_id: str,
    chapter: int,
    req: DraftUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    project = _project(registry, project_id)
    conn = registry.open_conn(project_id)
    try:
        latest = conn.execute(
            "SELECT id FROM draft_index WHERE chapter=?"
            " ORDER BY revision_round DESC LIMIT 1",
            (chapter,),
        ).fetchone()
        if latest is None:
            raise HTTPException(404, f"第 {chapter} 章尚无可编辑草稿")
        if latest["id"] != req.base_revision_id:
            raise HTTPException(
                409,
                {
                    "message": "草稿已产生新修订，请刷新后再保存",
                    "expected_revision_id": req.base_revision_id,
                    "current_revision_id": latest["id"],
                },
            )
        from ...control_plane.orchestrator import (
            DraftRevisionConflict,
            _persist_draft,
        )

        try:
            draft_id = _persist_draft(
                conn,
                req.draft_text,
                chapter,
                project_id,
                project.db_path,
                expected_base_revision_id=req.base_revision_id,
            )
        except DraftRevisionConflict as exc:
            raise HTTPException(
                409,
                {
                    "message": "草稿已产生新修订，请刷新后再保存",
                    "expected_revision_id": exc.expected_id,
                    "current_revision_id": exc.current_id,
                },
            ) from exc
        if draft_id is None:
            raise HTTPException(500, "草稿修订落盘失败")
        # Alignment is derived from the exact draft revision.  Mark the prior
        # report stale until the next validation pass instead of presenting it
        # as evidence for the edited text.
        conn.execute(
            "UPDATE chapter_actuals SET status='superseded', superseded_at=datetime('now')"
            " WHERE chapter=? AND status='current'",
            (chapter,),
        )
        conn.execute(
            "UPDATE plan_alignment_reports SET status='superseded',"
            " superseded_at=datetime('now') WHERE chapter=? AND status='current'",
            (chapter,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT revision_round, word_count, status, created_at FROM draft_index WHERE id=?",
            (draft_id,),
        ).fetchone()
        return {
            "chapter": chapter,
            "revision_id": draft_id,
            "revision_round": row["revision_round"],
            "draft_text": req.draft_text,
            "word_count": row["word_count"],
            "status": row["status"],
            "created_at": row["created_at"],
            "alignment_status": "stale",
        }
    finally:
        conn.close()


def _load_json(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback
