"""LangGraph-backed choice-driven author Agent runtime.

LangGraph owns durable control-flow checkpoints.  NovelForge's own SQLite tables
remain authoritative for plans, decisions, drafts, canon, and audit history.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..ids import new_id
from ..planning.context import gather_plan_context
from ..planning.generation import (
    generate_chapter_route_options,
    generate_story_options,
    generate_volume_options,
    recommend_decision_option,
)
from ..planning.service import PlanningService
from ..planning.models import DecisionOptionDraft
from ..planning.repository import (
    apply_chapter_route,
    apply_story_plan,
    apply_volume_plan,
    create_decision,
    load_decision,
    patch_decision_context,
    resolve_decision,
    selected_option,
    story_map,
    supersede_decision,
)
from ..writing_profiles import get_project_writing_profile
from .deps import ProjectRegistry


class AgentState(TypedDict, total=False):
    project_id: str
    db_path: str
    session_id: str
    interaction_mode: Literal["guided", "copilot", "autonomous"]
    governance_mode: Literal["human_gate", "auto_promote", "hybrid"]
    stage: str
    status: str
    hints: str
    planning_horizon: int
    auto_chapters: int
    chapters_generated: int
    pending_decision_id: str | None
    choice: dict[str, Any] | None
    last_run_id: str | None
    current_chapter: int | None
    last_error: str | None
    generation_meta: dict[str, Any]
    alignment_report: dict[str, Any] | None
    recommended_plan_action: Literal["keep", "patch", "replan", "pause"] | None


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _next_unwritten_chapter(conn: sqlite3.Connection, project_id: str) -> int:
    from .chapter_suggest import next_chapter_no
    return next_chapter_no(conn, project_id)[0]


def _has_volume(conn: sqlite3.Connection, chapter: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM volumes WHERE start_chapter IS NOT NULL AND start_chapter<=?"
        " AND (end_chapter IS NULL OR end_chapter>=?) LIMIT 1",
        (chapter, chapter),
    ).fetchone() is not None


def _has_locked_contract(conn: sqlite3.Connection, chapter: int) -> bool:
    row = conn.execute(
        "SELECT locked FROM chapter_cards WHERE chapter=? AND status='planned'",
        (chapter,),
    ).fetchone()
    if row is None or not bool(row["locked"]):
        return False
    return conn.execute(
        "SELECT 1 FROM beats WHERE chapter=? AND status='planned' LIMIT 1",
        (chapter,),
    ).fetchone() is not None


def _infer_stage(conn: sqlite3.Connection, state: AgentState) -> str:
    explicit = state.get("stage")
    if explicit and explicit not in ("bootstrap", ""):
        return explicit
    if conn.execute(
        "SELECT 1 FROM story_plans WHERE status='active' LIMIT 1"
    ).fetchone() is None:
        return "story_plan"
    chapter = _next_unwritten_chapter(conn, state["project_id"])
    if not _has_volume(conn, chapter):
        return "volume_plan"
    if not _has_locked_contract(conn, chapter):
        return "chapter_route"
    return "chapter_action"


def _fixed_action_options(
    stage: str,
    state: AgentState,
    *,
    content_language: str = "zh-CN",
) -> list[DecisionOptionDraft]:
    english = content_language.startswith("en")
    if stage == "chapter_action":
        chapter = state.get("current_chapter") or 1
        if english:
            return [
                DecisionOptionDraft(
                    title=f"Generate Chapter {chapter}",
                    summary="Generate three drafts from the selected chapter contract and preselect the strongest one.",
                    payload={"action": "generate_chapter", "chapter": chapter},
                    rationale="The current route is ready for drafting.",
                    impact={"candidate_count": 3, "uses_future_guardrails": True},
                    score=9.2,
                ),
                DecisionOptionDraft(
                    title="Replan the future route",
                    summary="Keep established facts and regenerate the future chapter window.",
                    payload={"action": "replan"},
                    rationale="Use this when the current route no longer fits the story.",
                    impact={"canon_changed": False},
                    score=7.5,
                ),
                DecisionOptionDraft(
                    title="Finish for now",
                    summary="Save the session and all plans so work can resume from this point.",
                    payload={"action": "finish"},
                    impact={"reversible": True},
                    score=5.0,
                ),
            ]
        return [
            DecisionOptionDraft(
                title=f"生成第 {chapter} 章",
                summary="按照已选择的章节契约生成 3 个候选稿，并自动预选一稿。",
                payload={"action": "generate_chapter", "chapter": chapter},
                rationale="当前路线已经准备好，可以进入正文生成。",
                impact={"candidate_count": 3, "uses_future_guardrails": True},
                score=9.2,
            ),
            DecisionOptionDraft(
                title="重排未来路线",
                summary="保留已发生事实，重新生成未来章节窗口。",
                payload={"action": "replan"},
                rationale="适合对当前路线不满意或计划已经偏航的情况。",
                impact={"canon_changed": False},
                score=7.5,
            ),
            DecisionOptionDraft(
                title="暂时结束",
                summary="保存当前会话和全部计划，之后可从同一节点恢复。",
                payload={"action": "finish"},
                impact={"reversible": True},
                score=5.0,
            ),
        ]
    if stage == "post_chapter":
        report = state.get("alignment_report") or {}
        divergence = report.get("divergence_score")
        if english:
            divergence_note = (
                f" Current divergence: {float(divergence) * 100:.0f}%."
                if isinstance(divergence, (int, float)) else ""
            )
            return [
                DecisionOptionDraft(
                    title="Continue to the next chapter",
                    summary="Keep the current route and use the latest world state." + divergence_note,
                    payload={"action": "continue"},
                    score=9.2 if state.get("recommended_plan_action") == "keep" else 7.0,
                ),
                DecisionOptionDraft(
                    title="Accept and replan",
                    summary="Keep this chapter and replan future chapters from its actual outcome.",
                    payload={"action": "replan"},
                    impact={"canon_changed": False, "future_plan_changed": True},
                    score=(9.2 if state.get("recommended_plan_action") in {"patch", "replan"}
                           else 7.8),
                ),
                DecisionOptionDraft(
                    title="End this writing session",
                    summary="Save the checkpoint and continue later.",
                    payload={"action": "finish"},
                    score=9.5 if state.get("recommended_plan_action") == "pause" else 5.0,
                ),
            ]
        divergence_note = (
            f" 当前偏航 {float(divergence) * 100:.0f}%。"
            if isinstance(divergence, (int, float)) else ""
        )
        return [
            DecisionOptionDraft(
                title="继续下一章",
                summary="保持当前路线，使用最新世界状态进入下一章。" + divergence_note,
                payload={"action": "continue"},
                score=9.2 if state.get("recommended_plan_action") == "keep" else 7.0,
            ),
            DecisionOptionDraft(
                title="接受本章并重排后续",
                summary="保留本章正文，根据实际落点重新规划未来章节窗口。",
                payload={"action": "replan"},
                impact={"canon_changed": False, "future_plan_changed": True},
                score=(9.2 if state.get("recommended_plan_action") in {"patch", "replan"}
                       else 7.8),
            ),
            DecisionOptionDraft(
                title="结束本次创作",
                summary="保存 checkpoint，稍后继续。",
                payload={"action": "finish"},
                score=9.5 if state.get("recommended_plan_action") == "pause" else 5.0,
            ),
        ]
    raise ValueError(f"unsupported fixed option stage: {stage}")


def _draft_options(conn: sqlite3.Connection, state: AgentState) -> list[DecisionOptionDraft]:
    from .api.orchestrator import _load_run_detail
    from .deps import get_registry

    run_id = state.get("last_run_id")
    if not run_id:
        return []
    registry = get_registry()
    detail = _load_run_detail(conn, registry, state["project_id"], run_id)
    english = get_project_writing_profile(conn)["content_language"].startswith("en")
    options = []
    for candidate in detail.candidates:
        options.append(DecisionOptionDraft(
            title=(
                f"Draft {candidate.index + 1}"
                if english else f"候选稿 {candidate.index + 1}"
            ),
            summary=candidate.draft_text[:360] + ("…" if len(candidate.draft_text) > 360 else ""),
            payload={
                "action": "select_draft",
                "run_id": run_id,
                "candidate_index": candidate.index,
            },
            rationale=(
                "Pipeline-selected draft" if candidate.is_winner
                else "Alternative draft available for manual selection"
            ) if english else (
                "流水线自动择优稿" if candidate.is_winner else "可人工改选的备选稿"
            ),
            impact={
                "length": candidate.length,
                "score": candidate.score,
                "hard_blocks": candidate.hard_blocks,
                "proposal_count": candidate.proposal_count,
                "is_current_winner": candidate.is_winner,
            },
            score=candidate.score,
        ))
    return options


def _prepare_node(state: AgentState) -> dict[str, Any]:
    conn = _connect(state["db_path"])
    try:
        stage = _infer_stage(conn, state)
        writing_profile = get_project_writing_profile(conn)
        content_language = writing_profile["content_language"]
        english = content_language.startswith("en")
        project = conn.execute("SELECT 1").fetchone()  # force early DB validation
        del project
        meta: dict[str, Any] = {}
        scope_type = "project"
        scope_id = state["project_id"]
        question = "Choose the next step." if english else "请选择下一步"
        recommended_index = 0

        if stage == "story_plan":
            registry = _registry_for_state(state)
            pm = registry.get(state["project_id"])
            options, meta = generate_story_options(
                conn, project_id=state["project_id"], db_path=state["db_path"],
                genre=pm.genre if pm else "default", project_name=pm.name if pm else "未命名小说",
                hints=state.get("hints") or None,
            )
            question = (
                "Which whole-story direction should this novel follow?"
                if english else "这本小说接下来采用哪一条全书路线？"
            )
            scope_type = "story"
        elif stage == "volume_plan":
            options, meta = generate_volume_options(
                conn, project_id=state["project_id"], db_path=state["db_path"],
                hints=state.get("hints") or None,
            )
            question = (
                "Which conflict and pacing plan should the next volume use?"
                if english else "下一卷采用哪一种冲突与节奏方案？"
            )
            scope_type = "volume"
        elif stage == "chapter_route":
            chapter = _next_unwritten_chapter(conn, state["project_id"])
            window = PlanningService(
                db_path=state["db_path"],
                route_generator=generate_chapter_route_options,
            ).generate_window(
                conn,
                project_id=state["project_id"],
                start_chapter=chapter,
                horizon=int(state.get("planning_horizon") or 5),
                source="agent",
                hints=state.get("hints") or None,
                candidate_count=3,
            )
            options, meta = window.options, window.metadata
            question = (
                f"Which future route should begin with Chapter {chapter}?"
                if english else f"从第 {chapter} 章开始，未来路线选择哪一套？"
            )
            scope_type = "chapter"
            scope_id = str(chapter)
        elif stage == "chapter_action":
            chapter = _next_unwritten_chapter(conn, state["project_id"])
            state = {**state, "current_chapter": chapter}
            options = _fixed_action_options(
                stage, state, content_language=content_language,
            )
            question = (
                f"Chapter {chapter} is planned. What should happen next?"
                if english else f"第 {chapter} 章已经规划完成，下一步怎么做？"
            )
            scope_type = "chapter"
            scope_id = str(chapter)
        elif stage == "draft_choice":
            options = _draft_options(conn, state)
            if not options:
                # Single-candidate or degraded detail: skip the fake choice and
                # immediately ask the real post-chapter decision instead.
                stage = "post_chapter"
                options = _fixed_action_options(
                    stage, state, content_language=content_language,
                )
                question = (
                    "The chapter is saved. Continue, replan, or finish?"
                    if english else "本章已经落盘，接下来继续、重排还是结束？"
                )
                scope_type = "replan"
                scope_id = str(state.get("current_chapter") or "")
                meta = {"draft_choice_skipped": True}
            else:
                current_chapter = state.get("current_chapter") or "?"
                question = (
                    f"Which draft should Chapter {current_chapter} use?"
                    if english else f"第 {current_chapter} 章采用哪一稿？"
                )
                scope_type = "draft"
                scope_id = state.get("last_run_id")
                winner = next((i for i, o in enumerate(options)
                               if o.impact.get("is_current_winner")), 0)
                decision = create_decision(
                    conn, session_id=state["session_id"], stage=stage,
                    scope_type=scope_type, scope_id=scope_id, question=question,
                    options=options, context={"run_id": state.get("last_run_id")},
                    recommended_index=winner,
                )
                return {
                    "stage": stage,
                    "pending_decision_id": decision["decision_id"],
                    "choice": None,
                    "generation_meta": meta,
                }
        elif stage == "post_chapter":
            options = _fixed_action_options(
                stage, state, content_language=content_language,
            )
            action = state.get("recommended_plan_action") or "keep"
            recommended_index = 0 if action == "keep" else 1 if action in {"patch", "replan"} else 2
            question = (
                "The chapter has been reconciled with the plan. Continue, replan, or pause?"
                if english else "本章已经完成计划与实际对账，接下来保持、重排还是暂停？"
            )
            scope_type = "replan"
            scope_id = str(state.get("current_chapter") or "")
        elif stage == "finished":
            return {"status": "finished", "pending_decision_id": None}
        else:
            raise ValueError(f"unknown agent stage: {stage}")

        decision = create_decision(
            conn,
            session_id=state["session_id"],
            stage=stage,
            scope_type=scope_type,
            scope_id=scope_id,
            question=question,
            options=options,
            context={
                "generation": meta,
                "hints": state.get("hints") or None,
                "alignment_report": state.get("alignment_report"),
                "recommended_plan_action": state.get("recommended_plan_action"),
            },
            recommended_index=recommended_index,
        )
        return {
            "stage": stage,
            "pending_decision_id": decision["decision_id"],
            "choice": None,
            "generation_meta": meta,
            "current_chapter": (
                int(scope_id) if scope_type == "chapter" and str(scope_id).isdigit()
                else state.get("current_chapter")
            ),
        }
    finally:
        conn.close()


def _registry_for_state(state: AgentState) -> ProjectRegistry:
    from .deps import get_registry
    return get_registry()


def _safety_auto_choice(decision: dict[str, Any], state: AgentState) -> str | None:
    if state.get("stage") == "chapter_action" and state.get("last_error"):
        finish = next(
            (o for o in decision["options"] if o["payload"].get("action") == "finish"), None
        )
        if finish:
            return finish["id"]
    if state.get("stage") == "post_chapter" and (
        int(state.get("chapters_generated") or 0) >= int(state.get("auto_chapters") or 1)
    ):
        finish = next(
            (o for o in decision["options"] if o["payload"].get("action") == "finish"), None
        )
        if finish:
            return finish["id"]
    if state.get("stage") == "post_chapter":
        recommended = state.get("recommended_plan_action") or "keep"
        target_action = (
            "continue" if recommended == "keep"
            else "replan" if recommended in {"patch", "replan"}
            else "finish"
        )
        option = next(
            (o for o in decision["options"]
             if o["payload"].get("action") == target_action),
            None,
        )
        if option:
            return option["id"]
    return None


def _choose_node(state: AgentState) -> dict[str, Any]:
    conn = _connect(state["db_path"])
    try:
        decision = load_decision(conn, state["pending_decision_id"])
    finally:
        conn.close()
    if (
        state.get("interaction_mode") == "autonomous"
        and state.get("stage") == "post_chapter"
        and state.get("recommended_plan_action") == "pause"
        and int(state.get("chapters_generated") or 0)
        < int(state.get("auto_chapters") or 1)
    ):
        # Severe divergence or a hard reveal is a safety boundary, not a
        # creative preference. Autonomous mode must hand control back to the
        # author with the evidence-bearing decision still awaiting choice.
        return {"choice": interrupt(decision)}
    if state.get("interaction_mode") == "autonomous":
        return {
            "choice": {
                "action": "auto",
                # Safety exits stay deterministic. Normal creative choices are
                # delegated to the structured option judge in _apply_node.
                "option_id": _safety_auto_choice(decision, state),
            }
        }
    answer = interrupt(decision)
    return {"choice": answer}


def _run_agent_chapter(state: AgentState, conn: sqlite3.Connection) -> dict[str, Any]:
    from .api.orchestrator import _build_orch, _load_run_detail, _make_cfg
    from .chapter_suggest import assemble_chapter_goal
    from .models import PipelineRunRequest

    registry = _registry_for_state(state)
    chapter = int(state.get("current_chapter") or _next_unwritten_chapter(conn, state["project_id"]))
    goal, _ = assemble_chapter_goal(conn, chapter)
    req = PipelineRunRequest(
        chapter_no=chapter,
        chapter_goal=goal,
        mode=state.get("governance_mode", "human_gate"),
        interaction_mode=state.get("interaction_mode", "guided"),
        n_candidates=3,
        quality_check=True,
    )
    cfg = _make_cfg(state["project_id"], req, registry)
    outcome = _build_orch(cfg).generate_chapter(
        chapter,
        conn,
        chapter_goal=goal,
        interaction_mode=state.get("interaction_mode", "guided"),
    )
    if not outcome.ok:
        raise RuntimeError(outcome.error or "chapter generation failed")
    detail = _load_run_detail(conn, registry, state["project_id"], outcome.run_id)
    return {
        "last_run_id": outcome.run_id,
        "current_chapter": chapter,
        "chapters_generated": int(state.get("chapters_generated") or 0) + 1,
        "stage": "draft_choice" if len(detail.candidates) > 1 else "post_chapter",
        # `outcome.error` may contain a non-fatal settlement degradation even
        # when the draft was produced successfully.  Agent `error` is reserved
        # for failures that keep the workflow on chapter_action.
        "last_error": None,
        "alignment_report": getattr(outcome, "alignment_report", None),
        "recommended_plan_action": getattr(
            outcome, "recommended_plan_action", None
        ),
    }


def _apply_node(state: AgentState) -> dict[str, Any]:
    choice = state.get("choice") or {}
    decision_id = state.get("pending_decision_id")
    if not decision_id:
        return {"stage": state.get("stage", "bootstrap")}
    conn = _connect(state["db_path"])
    try:
        if choice.get("action") == "reroll":
            supersede_decision(conn, decision_id)
            return {
                "pending_decision_id": None,
                "choice": None,
                "hints": choice.get("hints") or state.get("hints", ""),
            }

        decision = load_decision(conn, decision_id)
        option_id = choice.get("option_id")
        if choice.get("action") == "auto":
            if decision.get("status") != "awaiting_choice":
                # Durable replay after the decision row was committed: reuse
                # the original selection rather than asking the model again.
                option_id = decision.get("selected_option_id")
            elif not option_id:
                option_id, selection_meta = recommend_decision_option(
                    decision,
                    project_id=state["project_id"],
                    db_path=state["db_path"],
                    state_context={
                        "stage": state.get("stage"),
                        "current_chapter": state.get("current_chapter"),
                        "chapters_generated": state.get("chapters_generated", 0),
                        "hints": state.get("hints", ""),
                    },
                )
                decision = patch_decision_context(
                    conn, decision_id, {"auto_selection": selection_meta}
                )
        if not option_id:
            raise ValueError("option_id is required")
        # Validate an alternate draft before resolving the durable decision.
        # Otherwise a rejected unsafe candidate would leave the decision row
        # committed while the graph checkpoint still waits at the interrupt.
        if state.get("stage") == "draft_choice":
            candidate_option = next(
                (item for item in decision.get("options", []) if item["id"] == option_id),
                None,
            )
            if candidate_option is None:
                raise KeyError(f"option {option_id} not in decision {decision_id}")
            from ..planning.alignment import check_plan_alignment
            from ..planning.context import gather_plan_context

            payload = candidate_option.get("payload") or {}
            run_id = payload.get("run_id")
            candidate_index = int(payload.get("candidate_index", -1))
            row = conn.execute(
                "SELECT chapter, detail_json FROM pipeline_run WHERE run_id=? AND project_id=?",
                (run_id, state["project_id"]),
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            detail = json.loads(row["detail_json"] or "{}")
            candidates = detail.get("candidates") or []
            if candidate_index < 0 or candidate_index >= len(candidates):
                raise ValueError(f"candidate_index out of range: {candidate_index}")
            if candidate_index != detail.get("winner"):
                text = candidates[candidate_index].get("draft_text") or ""
                context = gather_plan_context(conn, int(row["chapter"]))
                blocks = [
                    issue for issue in check_plan_alignment(
                        context, text, int(row["chapter"])
                    )
                    if issue.get("severity") == "block"
                ]
                if blocks:
                    raise ValueError(
                        "候选稿违反未来计划硬约束: "
                        + "；".join(str(i.get("message") or i.get("code")) for i in blocks)
                    )
        resolved = resolve_decision(
            conn, decision_id, option_id, auto=(choice.get("action") == "auto")
        )
        option = selected_option(resolved)
        action = option["payload"].get("action")
        stage = state["stage"]
        updates: dict[str, Any] = {
            "pending_decision_id": None,
            "choice": None,
            "last_error": None,
        }

        if stage == "story_plan":
            apply_story_plan(conn, resolved)
            updates["stage"] = "volume_plan"
        elif stage == "volume_plan":
            apply_volume_plan(conn, resolved)
            updates["stage"] = "chapter_route"
        elif stage == "chapter_route":
            apply_chapter_route(conn, resolved)
            updates["stage"] = "chapter_action"
        elif stage == "chapter_action":
            if action == "generate_chapter":
                try:
                    updates.update(_run_agent_chapter(state, conn))
                except Exception as exc:
                    updates.update({
                        "stage": "chapter_action",
                        "last_error": f"{type(exc).__name__}: {str(exc)[:400]}",
                    })
            elif action == "replan":
                updates["stage"] = "chapter_route"
            elif action == "finish":
                updates.update({"stage": "finished", "status": "finished"})
        elif stage == "draft_choice":
            from .api.orchestrator import select_candidate
            from .models import SelectCandidateRequest

            run_id = option["payload"]["run_id"]
            candidate_index = int(option["payload"]["candidate_index"])
            selected = select_candidate(
                state["project_id"], run_id,
                SelectCandidateRequest(candidate_index=candidate_index),
                _registry_for_state(state),
            )
            updates["stage"] = "post_chapter"
            updates["alignment_report"] = getattr(
                selected, "alignment_report", None
            )
            updates["recommended_plan_action"] = getattr(
                selected, "recommended_plan_action", None
            )
        elif stage == "post_chapter":
            if action == "continue":
                next_chapter = _next_unwritten_chapter(conn, state["project_id"])
                updates["stage"] = (
                    "chapter_action" if _has_locked_contract(conn, next_chapter)
                    else "chapter_route"
                )
                updates["current_chapter"] = next_chapter
            elif action == "replan":
                updates["stage"] = "chapter_route"
                try:
                    from ..planning.replanner import (
                        build_replan_context,
                        render_replan_hints,
                    )

                    source_chapter = int(state.get("current_chapter") or 0)
                    replan_context = build_replan_context(
                        conn,
                        source_chapter=source_chapter,
                        horizon=int(state.get("planning_horizon") or 5),
                        reason="agent_post_chapter_replan",
                    )
                    updates["hints"] = render_replan_hints(replan_context)
                except Exception:
                    pass
            elif action == "finish":
                updates.update({"stage": "finished", "status": "finished"})
        return updates
    finally:
        conn.close()


def _continue_after_apply(state: AgentState) -> str:
    return END if state.get("status") == "finished" or state.get("stage") == "finished" else "prepare"


def build_agent_graph(checkpointer: SqliteSaver):
    graph = StateGraph(AgentState)
    graph.add_node("prepare", _prepare_node)
    graph.add_node("choose", _choose_node)
    graph.add_node("apply", _apply_node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "choose")
    graph.add_edge("choose", "apply")
    graph.add_conditional_edges("apply", _continue_after_apply, {"prepare": "prepare", END: END})
    return graph.compile(checkpointer=checkpointer)


class AgentRuntime:
    def __init__(self, registry: ProjectRegistry) -> None:
        self.registry = registry

    def _with_graph(self, project_id: str):
        pm = self.registry.get(project_id)
        if pm is None:
            raise KeyError(f"project not found: {project_id}")
        saver_ctx = SqliteSaver.from_conn_string(pm.db_path)
        saver = saver_ctx.__enter__()
        saver.conn.execute("PRAGMA journal_mode=WAL")
        saver.conn.execute("PRAGMA busy_timeout=5000")
        graph = build_agent_graph(saver)
        return saver_ctx, graph, pm

    def start(
        self,
        project_id: str,
        *,
        interaction_mode: str,
        actor: str,
        governance_mode: str,
        hints: str | None,
        planning_horizon: int,
        auto_chapters: int,
    ) -> dict[str, Any]:
        pm = self.registry.get(project_id)
        if pm is None:
            raise KeyError(f"project not found: {project_id}")
        session_id = new_id("sess")
        turn_id = new_id("turn")
        conn = self.registry.open_conn(project_id)
        try:
            conn.execute(
                "INSERT INTO sessions(id, client, mode, actor, interaction_mode)"
                " VALUES(?, 'web', ?, ?, ?)",
                (session_id, governance_mode, actor, interaction_mode),
            )
            conn.execute(
                "INSERT INTO turns(id, session_id, seq, kind, intent, request_json,"
                " routed_endpoint, status, stream) VALUES(?,?,1,'long_task','agent_start',?,?,"
                " 'running',0)",
                (turn_id, session_id, json.dumps({
                    "interaction_mode": interaction_mode,
                    "hints": hints,
                    "planning_horizon": planning_horizon,
                    "auto_chapters": auto_chapters,
                }, ensure_ascii=False), f"/v1/{project_id}/agent/start"),
            )
            conn.commit()
        finally:
            conn.close()

        saver_ctx, graph, pm = self._with_graph(project_id)
        try:
            config = {"configurable": {"thread_id": session_id}}
            try:
                graph.invoke({
                    "project_id": project_id,
                    "db_path": pm.db_path,
                    "session_id": session_id,
                    "interaction_mode": interaction_mode,
                    "governance_mode": governance_mode,
                    "stage": "bootstrap",
                    "status": "running",
                    "hints": hints or "",
                    "planning_horizon": planning_horizon,
                    "auto_chapters": auto_chapters,
                    "chapters_generated": 0,
                    "pending_decision_id": None,
                    "choice": None,
                    "last_run_id": None,
                    "current_chapter": None,
                    "last_error": None,
                    "generation_meta": {},
                    "alignment_report": None,
                    "recommended_plan_action": None,
                }, config, durability="sync")
                response = self._response(project_id, session_id, graph, config)
                self._finish_turn(project_id, turn_id, response)
                return response
            except Exception as exc:
                self._fail_turn(project_id, turn_id, exc)
                raise
        finally:
            saver_ctx.__exit__(None, None, None)

    def resume(
        self,
        project_id: str,
        session_id: str,
        *,
        action: str,
        option_id: str | None,
        hints: str | None,
    ) -> dict[str, Any]:
        saver_ctx, graph, _ = self._with_graph(project_id)
        try:
            config = {"configurable": {"thread_id": session_id}}
            snapshot = graph.get_state(config)
            if not snapshot.values:
                raise KeyError(f"agent session checkpoint not found: {session_id}")
            conn = self.registry.open_conn(project_id)
            try:
                seq = int(conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 AS n FROM turns WHERE session_id=?",
                    (session_id,),
                ).fetchone()["n"])
                turn_id = new_id("turn")
                conn.execute(
                    "INSERT INTO turns(id, session_id, seq, kind, intent, request_json,"
                    " routed_endpoint, status, stream) VALUES(?,?,?,'command','agent_choice',?,?,"
                    " 'running',0)",
                    (turn_id, session_id, seq,
                     json.dumps({"action": action, "option_id": option_id, "hints": hints},
                                ensure_ascii=False),
                     f"/v1/{project_id}/agent/{session_id}/choose"),
                )
                conn.commit()
            finally:
                conn.close()

            try:
                graph.invoke(Command(resume={
                    "action": action,
                    "option_id": option_id,
                    "hints": hints,
                }), config, durability="sync")
                response = self._response(project_id, session_id, graph, config)
                self._finish_turn(project_id, turn_id, response)
                return response
            except Exception as exc:
                self._fail_turn(project_id, turn_id, exc)
                raise
        finally:
            saver_ctx.__exit__(None, None, None)

    def get(self, project_id: str, session_id: str) -> dict[str, Any]:
        saver_ctx, graph, _ = self._with_graph(project_id)
        try:
            config = {"configurable": {"thread_id": session_id}}
            snapshot = graph.get_state(config)
            if not snapshot.values:
                raise KeyError(f"agent session checkpoint not found: {session_id}")
            return self._response(project_id, session_id, graph, config)
        finally:
            saver_ctx.__exit__(None, None, None)

    def list_sessions(self, project_id: str) -> list[dict[str, Any]]:
        conn = self.registry.open_conn(project_id)
        try:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE actor LIKE 'agent:%' OR client='web'"
                " ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            try:
                out.append(self.get(project_id, row["id"]))
            except Exception:
                continue
        return out

    def _response(self, project_id: str, session_id: str, graph, config) -> dict[str, Any]:
        snapshot = graph.get_state(config)
        state = dict(snapshot.values or {})
        decision = None
        last_decision = None
        conn = self.registry.open_conn(project_id)
        try:
            decision_id = state.get("pending_decision_id")
            if decision_id:
                try:
                    decision = load_decision(conn, decision_id)
                except KeyError:
                    decision = None
            last_row = conn.execute(
                "SELECT id FROM decision_sets WHERE session_id=?"
                " AND status IN ('selected','auto_selected')"
                # SQLite datetime('now') has one-second precision; a fast
                # autonomous run can resolve several stages in the same second.
                # rowid provides the required insertion-order tie-breaker.
                " ORDER BY resolved_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if last_row is not None:
                try:
                    last_decision = load_decision(conn, last_row["id"])
                except KeyError:
                    last_decision = None
            current_map = story_map(conn)
            if state.get("status") == "finished" or state.get("stage") == "finished":
                conn.execute(
                    "UPDATE sessions SET ended_at=COALESCE(ended_at, datetime('now')) WHERE id=?",
                    (session_id,),
                )
                conn.commit()
        finally:
            conn.close()
        status = (
            "finished" if state.get("status") == "finished" or state.get("stage") == "finished"
            else "awaiting_choice" if snapshot.next
            else state.get("status", "running")
        )
        public_state = {
            key: value for key, value in state.items()
            if key not in {"db_path", "choice"}
        }
        return {
            "session_id": session_id,
            "project_id": project_id,
            "interaction_mode": state.get("interaction_mode", "guided"),
            "stage": state.get("stage", "bootstrap"),
            "status": status,
            "decision": decision,
            "last_decision": last_decision,
            "state": public_state,
            "story_map": current_map,
            "error": state.get("last_error"),
        }

    def _finish_turn(self, project_id: str, turn_id: str, response: dict[str, Any]) -> None:
        conn = self.registry.open_conn(project_id)
        try:
            conn.execute(
                "UPDATE turns SET status='done', result_json=?, finished_at=datetime('now')"
                " WHERE id=?",
                (json.dumps({
                    "stage": response["stage"],
                    "status": response["status"],
                    "decision_id": (response.get("decision") or {}).get("decision_id"),
                }, ensure_ascii=False), turn_id),
            )
            conn.execute(
                "INSERT INTO turn_events(turn_id, event_type, data_json) VALUES(?, 'result', ?)",
                (turn_id, json.dumps(response, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

    def _fail_turn(self, project_id: str, turn_id: str, exc: Exception) -> None:
        """Close the audit record even when graph/model/database execution fails."""
        error = f"{type(exc).__name__}: {str(exc)[:1000]}"
        conn = self.registry.open_conn(project_id)
        try:
            conn.execute(
                "UPDATE turns SET status='error', result_json=?, finished_at=datetime('now')"
                " WHERE id=?",
                (json.dumps({"error": error}, ensure_ascii=False), turn_id),
            )
            conn.execute(
                "INSERT INTO turn_events(turn_id, event_type, data_json) VALUES(?, 'error', ?)",
                (turn_id, json.dumps({"error": error}, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
