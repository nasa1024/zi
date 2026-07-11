"""Plan alignment checks and rolling reconciliation."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..ids import new_id
from .context import PlanContext


def _keywords(milestone: dict[str, Any]) -> list[str]:
    target = milestone.get("target_state") or {}
    raw = target.get("keywords") or []
    return [str(k).strip() for k in raw if str(k).strip()]


def check_plan_alignment(
    context: PlanContext,
    draft_text: str,
    chapter: int,
) -> list[dict[str, Any]]:
    """Block explicit early reveals and flag milestones that have drifted overdue.

    Keyword evidence is intentionally conservative.  Generated plans may provide
    `target_state.keywords`; without explicit evidence we do not pretend a
    deterministic semantic judgement is possible.
    """
    issues: list[dict[str, Any]] = []
    for milestone in context.forbidden_reveals:
        hits = [keyword for keyword in _keywords(milestone) if keyword in draft_text]
        if hits:
            issues.append({
                "source": "plan",
                "code": "PREMATURE_PLAN_REVEAL",
                "severity": "block",
                "message": (
                    f"里程碑《{milestone['title']}》不得早于第"
                    f"{milestone['not_before_chapter']}章兑现；正文命中：{'、'.join(hits)}"
                ),
                "milestone_id": milestone["id"],
                "evidence": hits,
            })
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


def reconcile_plan(
    conn: sqlite3.Connection,
    context: PlanContext,
    draft_text: str,
    chapter: int,
) -> dict[str, Any]:
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
        if keywords and in_window and all(keyword in draft_text for keyword in keywords):
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

    divergence = round(len(deferred) / max(1, considered), 3)
    plan_id = context.story_plan.get("id") if context.story_plan else None
    if deferred and plan_id:
        conn.execute(
            "INSERT INTO plan_revisions(id, plan_id, source_chapter, reason, divergence_score,"
            " before_json, after_json) VALUES(?,?,?,?,?,?,?)",
            (new_id("prev"), plan_id, chapter, "milestone_overdue", divergence,
             json.dumps({"deferred": deferred}, ensure_ascii=False),
             json.dumps({"action": "await_replan_decision"}, ensure_ascii=False)),
        )
    conn.commit()
    return {
        "completed": completed,
        "deferred": deferred,
        "divergence_score": divergence,
        "needs_replan": divergence >= 0.25,
    }
