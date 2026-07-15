"""Evidence-backed plan checks, actuals extraction, and rolling reconciliation."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from ..ids import new_id
from .actuals import (
    ActualsExtraction,
    extract_chapter_actuals,
    find_evidence_span,
    persist_chapter_actuals,
)
from .context import PlanContext
from .models import ChapterActuals, PlanAlignmentReport
from .scoring import (
    divergence_level,
    evaluate_candidate_against_plan,
    persist_alignment_report,
    recommend_plan_action,
    score_plan_alignment,
)


@dataclass(frozen=True)
class AlignmentEvaluation:
    actuals: ChapterActuals
    report: PlanAlignmentReport
    proposals: list[dict[str, Any]]
    actuals_id: str | None = None
    report_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "actuals": self.actuals.model_dump(mode="json"),
            "alignment_report": self.report.model_dump(mode="json"),
            "proposals": self.proposals,
            "actuals_id": self.actuals_id,
            "report_id": self.report_id,
            "recommended_plan_action": self.report.decision,
        }


def _keywords(milestone: dict[str, Any]) -> list[str]:
    target = milestone.get("target_state") or {}
    raw = target.get("keywords") or []
    return [str(k).strip() for k in raw if str(k).strip()]


def check_plan_alignment(
    context: PlanContext,
    draft_text: str,
    chapter: int,
) -> list[dict[str, Any]]:
    """Return actionable plan hard-blocks plus overdue milestone warnings.

    Exact matches remain mandatory for forbidden reveals.  This preserves the
    conservative v16 safety rule while adding the expanded chapter prohibitions.
    """
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # Keep the richer legacy milestone message/id for API compatibility.
    for milestone in context.forbidden_reveals:
        hits = [keyword for keyword in _keywords(milestone) if keyword in draft_text]
        if not hits:
            continue
        issue = {
            "source": "plan",
            "code": "PREMATURE_PLAN_REVEAL",
            "severity": "block",
            "message": (
                f"里程碑《{milestone['title']}》不得早于第"
                f"{milestone['not_before_chapter']}章兑现；正文命中：{'、'.join(hits)}"
            ),
            "milestone_id": milestone["id"],
            "evidence": hits,
        }
        issues.append(issue)
        seen.update((issue["code"], hit) for hit in hits)

    candidate = evaluate_candidate_against_plan(
        draft_text,
        chapter_contract=context.current_contract,
        plan_context=context,
    )
    for issue in candidate["plan_blocks"]:
        target = str(issue.get("target") or "")
        key = (str(issue.get("code") or ""), target)
        if key in seen or (
            issue.get("code") == "PREMATURE_PLAN_REVEAL"
            and any(existing[0] == "PREMATURE_PLAN_REVEAL" and existing[1] == target for existing in seen)
        ):
            continue
        issues.append(issue)
        seen.add(key)

    for milestone in context.due_milestones:
        due_end = milestone.get("due_end_chapter")
        if due_end is not None and int(due_end) < chapter:
            issues.append({
                "source": "plan",
                "code": "PLAN_MILESTONE_OVERDUE",
                "severity": "warn",
                "message": f"里程碑《{milestone['title']}》已超过计划窗口第{due_end}章",
                "milestone_id": milestone["id"],
            })
    return issues


def evaluate_and_persist_alignment(
    conn: sqlite3.Connection,
    context: PlanContext,
    draft_text: str,
    chapter: int,
    *,
    draft_id: str | None,
    seed_proposals: Sequence[dict[str, Any]] | None = None,
    interaction_mode: str = "guided",
) -> AlignmentEvaluation:
    """Extract final actuals, score the locked plan, and persist both snapshots.

    The helper never commits: callers can include the two current-row switches
    in their own transaction.  On pre-v20 databases the in-memory report is
    still returned and persistence ids are ``None``.
    """
    extraction: ActualsExtraction = extract_chapter_actuals(
        chapter,
        draft_text,
        context,
        draft_id=draft_id,
        seed_proposals=seed_proposals,
    )
    actuals_id = persist_chapter_actuals(conn, extraction.actuals)
    report = score_plan_alignment(
        extraction.actuals,
        context,
        draft_text=draft_text,
        interaction_mode=interaction_mode,
        actuals_id=actuals_id,
    )
    report_id = persist_alignment_report(conn, report)
    return AlignmentEvaluation(
        actuals=extraction.actuals,
        report=report,
        proposals=extraction.proposals,
        actuals_id=actuals_id,
        report_id=report_id,
    )


def _coerce_actuals(
    value: ChapterActuals | dict[str, Any] | None,
) -> ChapterActuals | None:
    if value is None or isinstance(value, ChapterActuals):
        return value
    return ChapterActuals.model_validate(value)


def _coerce_report(
    value: PlanAlignmentReport | dict[str, Any] | None,
) -> PlanAlignmentReport | None:
    if value is None or isinstance(value, PlanAlignmentReport):
        return value
    return PlanAlignmentReport.model_validate(value)


def reconcile_plan(
    conn: sqlite3.Connection,
    context: PlanContext,
    draft_text: str,
    chapter: int,
    *,
    actuals: ChapterActuals | dict[str, Any] | None = None,
    report: PlanAlignmentReport | dict[str, Any] | None = None,
    report_id: str | None = None,
    interaction_mode: str = "guided",
    commit: bool = True,
) -> dict[str, Any]:
    """Reconcile milestone state and expose the structured plan decision.

    Optional ``actuals``/``report`` let the Orchestrator reuse the post-selection
    extraction.  Calling the legacy four-argument form remains supported.
    """
    actuals_model = _coerce_actuals(actuals)
    if actuals_model is None:
        actuals_model = extract_chapter_actuals(chapter, draft_text, context).actuals
    report_model = _coerce_report(report)
    if report_model is None:
        report_model = score_plan_alignment(
            actuals_model,
            context,
            draft_text=draft_text,
            interaction_mode=interaction_mode,
        )

    completed: list[str] = []
    deferred: list[str] = []
    considered = 0
    for milestone in context.due_milestones:
        considered += 1
        keywords = _keywords(milestone)
        due_start = milestone.get("due_start_chapter")
        due_end = milestone.get("due_end_chapter")
        in_window = (
            (due_start is None or int(due_start) <= chapter)
            and (due_end is None or chapter <= int(due_end))
        )
        # Completion requires sentence-level evidence for every explicit target
        # keyword.  Empty keyword lists remain undecidable instead of guessing.
        evidence = [find_evidence_span(draft_text, keyword) for keyword in keywords]
        if keywords and in_window and all(span is not None for span in evidence):
            conn.execute(
                "UPDATE arc_milestones SET status='completed', completed_chapter=?,"
                " updated_at=datetime('now') WHERE id=? AND status!='completed'",
                (chapter, milestone["id"]),
            )
            completed.append(milestone["id"])
        elif due_end is not None and int(due_end) < chapter:
            conn.execute(
                "UPDATE arc_milestones SET status='deferred', updated_at=datetime('now')"
                " WHERE id=? AND status IN ('planned','active')",
                (milestone["id"],),
            )
            deferred.append(milestone["id"])

    milestone_divergence = round(len(deferred) / max(1, considered), 4)
    divergence = max(report_model.divergence_score, milestone_divergence)
    if divergence != report_model.divergence_score:
        action = recommend_plan_action(
            divergence,
            interaction_mode=interaction_mode,
            hard_reveal=bool(report_model.detail.get("hard_reveal")),
        )
        report_model = report_model.model_copy(update={
            "divergence_score": divergence,
            "divergence_level": divergence_level(divergence),
            "decision": action,
            "detail": {
                **report_model.detail,
                "milestone_divergence": milestone_divergence,
                "deferred_milestones": deferred,
            },
        })

    # ``evaluate_and_persist_alignment`` stores the first weighted snapshot.
    # Milestone reconciliation can raise divergence afterwards; update the same
    # unique report row before callers/API readers observe the result.
    refreshed_report_id = persist_alignment_report(conn, report_model)
    if refreshed_report_id is not None:
        report_id = refreshed_report_id

    plan_id = context.story_plan.get("id") if context.story_plan else None
    action = report_model.decision or recommend_plan_action(
        divergence, interaction_mode=interaction_mode,
    )
    if (deferred or action in {"replan", "pause"}) and plan_id:
        conn.execute(
            "INSERT INTO plan_revisions(id, plan_id, source_chapter, reason, divergence_score,"
            " before_json, after_json) VALUES(?,?,?,?,?,?,?)",
            (
                new_id("prev"),
                plan_id,
                chapter,
                "milestone_overdue" if deferred else "chapter_plan_divergence",
                divergence,
                json.dumps({
                    "plan_version_id": report_model.plan_version_id,
                    "deferred": deferred,
                }, ensure_ascii=False),
                json.dumps({
                    "action": action,
                    "alignment_report_id": report_id,
                }, ensure_ascii=False),
            ),
        )
    if commit:
        conn.commit()
    return {
        "completed": completed,
        "deferred": deferred,
        "divergence_score": divergence,
        "divergence_level": report_model.divergence_level,
        "needs_replan": divergence >= 0.25,
        "alignment_report": report_model.model_dump(mode="json"),
        "alignment_report_id": report_id,
        "actuals_id": report_model.actuals_id,
        "recommended_plan_action": action,
    }
