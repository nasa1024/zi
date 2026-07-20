"""Transactional planning domain service.

Both the legacy volume endpoint and the choice-driven Agent use this service,
so a chapter plan has one storage meaning regardless of how it was created.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..control_plane.llm.runtime import build_project_config, build_project_gateway
from ..control_plane.skill_base import SkillContext
from ..control_plane.skill_registry import SkillRegistry
from ..db.connection import get_meta
from ..ids import new_id
from ..skills import register_default_skills
from ..writing_profiles import get_project_writing_profile
from .context import gather_plan_context
from .generation import generate_chapter_route_options
from .models import ChapterPlanDraft, ChapterRoutePayload, DecisionOptionDraft
from .repository import (
    AppliedPlanWindow,
    apply_plan_window,
    create_decision,
    resolve_decision,
    selected_option,
)


@dataclass
class GeneratedPlanWindow:
    project_id: str
    start_chapter: int
    horizon: int
    source: str
    hints: str | None = None
    options: list[DecisionOptionDraft] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def chapters(self) -> list[dict[str, Any]]:
        if not self.options:
            return []
        payload = self.options[0].payload
        return list(payload.get("chapters") or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "start_chapter": self.start_chapter,
            "horizon": self.horizon,
            "source": self.source,
            "hints": self.hints,
            "candidates": [option.model_dump(mode="json") for option in self.options],
            "metadata": self.metadata,
            "error": self.error,
        }


class PlanningService:
    def __init__(self, *, db_path: str | None = None, route_generator=None) -> None:
        self.db_path = db_path
        self._route_generator = route_generator or generate_chapter_route_options

    def generate_window(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        start_chapter: int,
        horizon: int,
        source: str,
        hints: str | None = None,
        volume_no: int | None = None,
        candidate_count: int = 3,
    ) -> GeneratedPlanWindow:
        """Generate validated candidates without mutating planning tables."""
        horizon = max(1, min(10, int(horizon)))
        if start_chapter < 1:
            raise ValueError("start_chapter must be positive")
        if source == "volume_api":
            return self._generate_volume_window(
                conn,
                project_id=project_id,
                start_chapter=start_chapter,
                horizon=horizon,
                volume_no=volume_no,
                hints=hints,
            )

        context = gather_plan_context(conn, start_chapter, lookahead_chapters=horizon)
        db_path = self._db_path(conn)
        cfg = build_project_config(project_id, db_path, conn)
        entity_ids = _planning_entity_ids(
            conn, start_chapter=start_chapter, horizon=horizon, limit=20
        )
        from ..memory import recall as recall_mod

        vector_on = bool(cfg.recall.enable_vector)
        embedder = None
        if vector_on:
            from ..memory.embedding import build_embedder

            embedder = build_embedder(cfg.memory, dim=cfg.memory.embedding_dim)
        profile = get_project_writing_profile(conn, chapter=start_chapter)
        content_language = str(profile.get("content_language") or "zh-CN")
        scene_query = (
            hints or context.render_for_planner(content_language)
        )[:2000]

        pack = recall_mod.gather_hard_context(
            entity_ids,
            start_chapter - 1,
            conn,
            keyword_query=hints or None,
            context_window=5,
            summary_window=cfg.recall.summary_window_chapters,
            enable_summaries=cfg.recall.enable_summaries,
            embedder=embedder,
            enable_vector=vector_on,
            scene_query=scene_query,
            vector_top_k=cfg.memory.vector_top_k,
            rrf_k=cfg.memory.rrf_k,
        )
        options, meta = self._route_generator(
            conn,
            project_id=project_id,
            db_path=db_path,
            start_chapter=start_chapter,
            horizon=horizon,
            plan_context=context.to_dict(),
            history_context=pack.to_context_str(content_language),
            hints=hints,
        )
        normalized = self._validate_options(
            conn, options[: max(1, min(3, candidate_count))], start_chapter, horizon
        )
        return GeneratedPlanWindow(
            project_id=project_id,
            start_chapter=start_chapter,
            horizon=horizon,
            source=source,
            hints=hints,
            options=normalized,
            metadata=meta,
        )

    def apply_window(
        self,
        conn: sqlite3.Connection,
        *,
        window: GeneratedPlanWindow,
        decision_id: str | None,
        lock: bool,
        replace_policy: str,
        option_payload: dict[str, Any] | None = None,
        based_on_chapter: int | None = None,
        reason: str | None = None,
        divergence_score: float | None = None,
        source_alignment_report_id: str | None = None,
    ) -> AppliedPlanWindow:
        """Validate the full window, then project it in one transaction."""
        payload = option_payload or (
            window.options[0].payload if window.options else {"chapters": []}
        )
        plans = [ChapterPlanDraft.model_validate(item) for item in payload.get("chapters") or []]
        self._validate_window(conn, plans, window.start_chapter, window.horizon)

        if conn.in_transaction:
            savepoint = f"planning_{new_id('sp').replace('-', '')}"
            conn.execute(f"SAVEPOINT {savepoint}")
            owns_transaction = False
        else:
            conn.execute("BEGIN IMMEDIATE")
            savepoint = ""
            owns_transaction = True
        try:
            applied = apply_plan_window(
                conn,
                plans,
                decision_id=decision_id,
                lock=lock,
                replace_policy=replace_policy,
                based_on_chapter=based_on_chapter,
                reason=reason or window.source,
                divergence_score=divergence_score,
                source_alignment_report_id=source_alignment_report_id,
            )
            if owns_transaction:
                conn.commit()
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return applied
        except Exception:
            if owns_transaction:
                conn.rollback()
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def apply_decision(
        self,
        conn: sqlite3.Connection,
        decision: dict[str, Any],
        *,
        lock: bool,
        replace_policy: str,
        source: str,
        based_on_chapter: int | None = None,
        reason: str | None = None,
        divergence_score: float | None = None,
        source_alignment_report_id: str | None = None,
    ) -> AppliedPlanWindow:
        option = selected_option(decision)
        payload = option.get("payload") or {}
        plans = payload.get("chapters") or []
        if not plans:
            raise ValueError("selected planning option has no chapters")
        start = min(int(item["chapter"]) for item in plans)
        window = GeneratedPlanWindow(
            project_id="",
            start_chapter=start,
            horizon=len(plans),
            source=source,
            options=[DecisionOptionDraft(
                title=option.get("title") or "已选路线",
                summary=option.get("summary") or "",
                payload=payload,
            )],
        )
        return self.apply_window(
            conn,
            window=window,
            decision_id=decision.get("decision_id"),
            lock=lock,
            replace_policy=replace_policy,
            option_payload=payload,
            based_on_chapter=based_on_chapter,
            reason=reason,
            divergence_score=divergence_score,
            source_alignment_report_id=source_alignment_report_id,
        )

    def create_compatibility_decision(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        window: GeneratedPlanWindow,
        actor: str,
    ) -> dict[str, Any]:
        """Create a durable source decision for non-Agent planning entry points."""
        if not window.options:
            raise ValueError("cannot create decision for an empty window")
        decision = self.create_pending_decision(
            conn,
            project_id=project_id,
            window=window,
            actor=actor,
            question="兼容入口生成的章节窗口",
        )
        return resolve_decision(
            conn,
            decision["decision_id"],
            decision["recommended_option_id"],
            auto=True,
            commit=False,
        )

    def create_pending_decision(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        window: GeneratedPlanWindow,
        actor: str,
        question: str = "请选择要采用的章节路线",
    ) -> dict[str, Any]:
        if not window.options:
            raise ValueError("cannot create decision for an empty window")
        session_id = new_id("sess")
        conn.execute(
            "INSERT INTO sessions(id, client, mode, actor, interaction_mode)"
            " VALUES(?, 'api', 'human_gate', ?, 'guided')",
            (session_id, actor),
        )
        conn.commit()
        return create_decision(
            conn,
            session_id=session_id,
            stage="chapter_route",
            scope_type="chapter",
            scope_id=str(window.start_chapter),
            question=question,
            options=window.options,
            context={
                "source": window.source,
                "project_id": project_id,
                "generation": window.metadata,
            },
            recommended_index=0,
        )

    def _generate_volume_window(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        start_chapter: int,
        horizon: int,
        volume_no: int | None,
        hints: str | None,
    ) -> GeneratedPlanWindow:
        if volume_no is None:
            raise ValueError("volume_no is required for volume_api planning")
        volume = conn.execute(
            "SELECT volume_no, title, synopsis, objective, key_results, start_chapter,"
            " end_chapter, rolling_summary FROM volumes WHERE volume_no=?",
            (volume_no,),
        ).fetchone()
        if volume is None:
            raise KeyError(f"volume {volume_no} not found")

        profile = get_project_writing_profile(conn, chapter=start_chapter)
        is_english = str(profile.get("content_language") or "").startswith("en")

        db_path = self._db_path(conn)
        cfg = build_project_config(project_id, db_path, conn)
        gateway = build_project_gateway(
            project_id, db_path, conn, allow_missing=True
        )
        if gateway is None:
            return GeneratedPlanWindow(
                project_id=project_id,
                start_chapter=start_chapter,
                horizon=horizon,
                source="volume_api",
                hints=hints,
                error=(
                    "The creative model is unavailable because no API key is configured."
                    if is_english else "LLM 网关不可用（缺 API key）"
                ),
                metadata={"degraded": True, "reason": "no_llm_key"},
            )

        entity_ids = _planning_entity_ids(
            conn, start_chapter=start_chapter, horizon=horizon, limit=20
        )
        from ..memory import recall as recall_mod

        pack = recall_mod.gather_hard_context(
            entity_ids,
            start_chapter - 1,
            conn,
            keyword_query=hints or None,
            summary_window=cfg.recall.summary_window_chapters,
            enable_summaries=cfg.recall.enable_summaries,
        )
        plan_to = start_chapter + horizon - 1
        foreshadow = conn.execute(
            "SELECT label, due_chapter FROM foreshadow"
            " WHERE state IN ('planted','reinforced','misled','overdue')"
            " AND due_chapter IS NOT NULL AND due_chapter<=?"
            " ORDER BY due_chapter LIMIT 20",
            (plan_to,),
        ).fetchall()
        registry = SkillRegistry()
        register_default_skills(registry)
        context = gather_plan_context(conn, start_chapter, lookahead_chapters=horizon)
        content_language = str(profile.get("content_language") or "zh-CN")
        stable_recall = pack.to_stable_context_str(content_language)
        skill_ctx = SkillContext(
            project_id=project_id,
            target_chapter=start_chapter,
            mode="human_gate",
            as_of_chapter=start_chapter - 1,
            budget=gateway.ledger,
            llm=gateway,
            conn=conn,
            workspace={
                "volume_brief": {
                    **dict(volume),
                    "key_results": _safe_json(volume["key_results"], []),
                },
                "plan_from": start_chapter,
                "plan_to": plan_to,
                "foreshadow_due": [dict(row) for row in foreshadow],
                "stable_context": (
                    (
                        f"## Stable World Context\n{stable_recall}"
                        if is_english else
                        f"## 世界设定（稳定）\n{stable_recall}"
                    )
                    if stable_recall else ""
                ),
                "future_plan_context": context.render_for_planner(content_language),
                "planning_hints": hints or "",
                "writing_profile": profile,
            },
        )
        result = registry.invoke("volume_plan", skill_ctx)
        plans = skill_ctx.workspace.get("chapter_plans") or []
        error = None if result.ok else (
            "Planning could not produce a complete chapter window."
            if is_english else "规划未能产出完整章节窗"
        )
        if error:
            metadata: dict[str, Any] = {
                "degraded": True,
                "reason": "volume_plan_failed",
            }
            return GeneratedPlanWindow(
                project_id=project_id,
                start_chapter=start_chapter,
                horizon=horizon,
                source="volume_api",
                hints=hints,
                error=error,
                metadata=metadata,
            )
        route = ChapterRoutePayload.model_validate({
            "route_title": (
                f"Volume {volume_no} Chapter Plan"
                if is_english else f"第{volume_no}卷兼容规划"
            ),
            "route_summary": str(
                volume["objective"] or volume["synopsis"] or (
                    "Rolling chapter progression" if is_english else "滚动推进"
                )
            ),
            "chapters": plans,
        })
        option = DecisionOptionDraft(
            title=route.route_title,
            summary=route.route_summary,
            payload=route.model_dump(mode="json"),
            rationale=(
                "Generated by the unified planning service and applied as one transactional window."
                if is_english else "由统一规划服务生成并按整窗事务应用"
            ),
            impact={"chapter_range": [start_chapter, plan_to], "source": "volume_api"},
            score=8.0,
        )
        self._validate_window(conn, route.chapters, start_chapter, horizon)
        return GeneratedPlanWindow(
            project_id=project_id,
            start_chapter=start_chapter,
            horizon=horizon,
            source="volume_api",
            hints=hints,
            options=[option],
            metadata={
                "degraded": bool(skill_ctx.workspace.get("volume_plan_degraded")),
                "tier_used": skill_ctx.workspace.get(
                    "volume_plan_tier_used", result.usage_summary,
                ),
                **(
                    {"reason": skill_ctx.workspace["volume_plan_fallback_reason"]}
                    if skill_ctx.workspace.get("volume_plan_fallback_reason") else {}
                ),
            },
        )

    def _validate_options(
        self,
        conn: sqlite3.Connection,
        options: Iterable[DecisionOptionDraft],
        start_chapter: int,
        horizon: int,
    ) -> list[DecisionOptionDraft]:
        normalized: list[DecisionOptionDraft] = []
        for option in options:
            route = ChapterRoutePayload.model_validate(option.payload)
            self._validate_window(conn, route.chapters, start_chapter, horizon)
            normalized.append(DecisionOptionDraft(
                **{
                    **option.model_dump(),
                    "payload": route.model_dump(mode="json"),
                }
            ))
        if not normalized:
            raise ValueError("planning generator returned no valid candidates")
        return normalized

    @staticmethod
    def _validate_window(
        conn: sqlite3.Connection,
        plans: Iterable[ChapterPlanDraft],
        start_chapter: int,
        horizon: int,
    ) -> None:
        values = list(plans)
        expected = list(range(start_chapter, start_chapter + horizon))
        chapters = [item.chapter for item in values]
        if chapters != expected:
            raise ValueError(
                f"planning window must exactly cover {expected}; got {chapters}"
            )
        for item in values:
            for beat in item.beats:
                if beat.constraint_level != "hard" or not beat.arc_id:
                    continue
                row = conn.execute(
                    "SELECT 1 FROM story_arcs WHERE id=?"
                    " AND status IN ('planned','active') AND start_chapter<=?"
                    " AND (end_chapter IS NULL OR end_chapter>=?)",
                    (beat.arc_id, item.chapter, item.chapter),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"hard beat arc_id is not active at chapter {item.chapter}: "
                        f"{beat.arc_id}"
                    )

    def _db_path(self, conn: sqlite3.Connection) -> str:
        if self.db_path:
            return self.db_path
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is not None:
            # sqlite3.Row uses name at index 2; tuple connection does too.
            return str(row[2] or "novel.db")
        return "novel.db"


def _planning_entity_ids(
    conn: sqlite3.Connection,
    *,
    start_chapter: int,
    horizon: int,
    limit: int,
) -> list[str]:
    """Rank entities by narrative relevance instead of arbitrary row order."""
    texts: list[str] = []
    pinned_ids: set[str] = set()
    protagonist = str(get_meta(conn, "protagonist") or "").strip()
    if protagonist:
        texts.append(protagonist)
    volume = conn.execute(
        "SELECT synopsis, objective FROM volumes WHERE start_chapter<=?"
        " AND (end_chapter IS NULL OR end_chapter>=?) ORDER BY volume_no LIMIT 1",
        (start_chapter, start_chapter),
    ).fetchone()
    if volume:
        texts.extend(str(volume[key] or "") for key in ("synopsis", "objective"))
    for row in conn.execute(
        "SELECT title, summary, objective FROM story_arcs"
        " WHERE status IN ('planned','active') AND start_chapter<=?"
        " AND (end_chapter IS NULL OR end_chapter>=?)",
        (start_chapter + horizon, start_chapter),
    ).fetchall():
        texts.extend(str(row[key] or "") for key in ("title", "summary", "objective"))
    for row in conn.execute(
        "SELECT summary FROM chapter_summaries WHERE chapter<?"
        " ORDER BY chapter DESC LIMIT 5",
        (start_chapter,),
    ).fetchall():
        texts.append(str(row["summary"] or ""))
    for row in conn.execute(
        "SELECT label, description, related_entity_id FROM foreshadow"
        " WHERE state IN ('planted','reinforced','misled','overdue')"
        " AND (due_chapter IS NULL OR due_chapter<=?)"
        " ORDER BY importance DESC, COALESCE(due_chapter,2147483647) LIMIT 20",
        (start_chapter + horizon - 1,),
    ).fetchall():
        texts.extend((str(row["label"] or ""), str(row["description"] or "")))
        if row["related_entity_id"]:
            pinned_ids.add(str(row["related_entity_id"]))
    haystack = "\n".join(texts)

    rows = conn.execute(
        "SELECT e.id, e.canonical_name, e.entity_type, e.first_appear_chapter,"
        " MAX(COALESCE(f.valid_from_chapter,0)) AS latest_fact"
        " FROM entities e LEFT JOIN facts f ON f.entity_id=e.id AND f.status='canon'"
        " GROUP BY e.id ORDER BY"
        " CASE WHEN e.entity_type='character' THEN 0 ELSE 1 END,"
        " latest_fact DESC, COALESCE(e.first_appear_chapter,999999), e.canonical_name"
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda row: (
            0 if str(row["id"]) in pinned_ids else 1,
            0 if protagonist and str(row["canonical_name"]) in protagonist else 1,
            0 if str(row["canonical_name"]) in haystack else 1,
            0 if row["entity_type"] == "character" else 1,
            -int(row["latest_fact"] or 0),
        ),
    )
    return [str(row["id"]) for row in ranked[:limit]]


def _safe_json(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback
