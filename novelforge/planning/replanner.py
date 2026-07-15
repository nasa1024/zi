"""Build auditable inputs for future-window replanning."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def build_replan_context(
    conn: sqlite3.Connection,
    *,
    source_chapter: int,
    horizon: int,
    reason: str | None,
) -> dict[str, Any]:
    start = source_chapter + 1
    end = source_chapter + max(1, horizon)
    versions = []
    for row in conn.execute(
        "SELECT chapter, id, version, payload_json, status FROM chapter_plan_versions"
        " WHERE chapter BETWEEN ? AND ? AND status='active' ORDER BY chapter",
        (start, end),
    ).fetchall():
        item = dict(row)
        item["payload"] = _load(item.pop("payload_json"), {})
        versions.append(item)

    actuals_row = conn.execute(
        "SELECT id, draft_id, payload_json FROM chapter_actuals"
        " WHERE chapter=? AND status='current' ORDER BY created_at DESC LIMIT 1",
        (source_chapter,),
    ).fetchone()
    actuals = None
    if actuals_row:
        actuals = {
            "id": actuals_row["id"],
            "draft_id": actuals_row["draft_id"],
            "payload": _load(actuals_row["payload_json"], {}),
        }

    report_row = conn.execute(
        "SELECT id, divergence_score, divergence_level, decision, detail_json"
        " FROM plan_alignment_reports WHERE chapter=? AND status='current'"
        " ORDER BY created_at DESC LIMIT 1",
        (source_chapter,),
    ).fetchone()
    report = None
    if report_row:
        report = dict(report_row)
        report["detail"] = _load(report.pop("detail_json"), {})

    immutable = [dict(row) for row in conn.execute(
        "SELECT c.chapter, c.status, c.locked, c.current_plan_version_id,"
        " CASE WHEN d.id IS NULL THEN 0 ELSE 1 END AS drafted"
        " FROM chapter_cards c LEFT JOIN draft_index d ON d.chapter=c.chapter"
        " WHERE c.chapter BETWEEN ? AND ? AND (c.locked=1 OR d.id IS NOT NULL)"
        " GROUP BY c.chapter ORDER BY c.chapter",
        (start, end),
    ).fetchall()]
    canon = [dict(row) for row in conn.execute(
        "SELECT subject, predicate, object, valid_from_chapter FROM facts"
        " WHERE status='canon' AND valid_from_chapter<=?"
        " ORDER BY valid_from_chapter DESC, created_at DESC LIMIT 80",
        (source_chapter,),
    ).fetchall()]
    return {
        "source_chapter": source_chapter,
        "range": [start, end],
        "reason": reason,
        "original_plans": versions,
        "chapter_actuals": actuals,
        "alignment_report": report,
        "canon_facts": canon,
        "immutable_chapters": immutable,
    }


def render_replan_hints(context: dict[str, Any]) -> str:
    """Render the complete replan contract for the route generator."""
    return (
        "请基于以下偏航对账修订未来窗口。已经发生的正文与 Canon 不得改写；"
        "immutable_chapters 中的章节不得修改。新路线必须解释如何吸收实际新增内容，"
        "同时继续服务全书与当前卷目标。\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )


def _load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback

