"""Build the author-only future planning context for a target chapter.

Historical truth and future intent must never be conflated:

* `memory.recall.gather_hard_context(as_of=N-1)` says what has happened.
* `gather_plan_context(N)` says what the author intends to happen next.

The second channel may constrain writing but must not be projected into canon.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


def _json(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
        return value
    except (TypeError, json.JSONDecodeError):
        return fallback


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


@dataclass
class PlanContext:
    target_chapter: int
    story_plan: dict[str, Any] | None = None
    current_volume: dict[str, Any] | None = None
    current_contract: dict[str, Any] | None = None
    lookahead: list[dict[str, Any]] = field(default_factory=list)
    active_arcs: list[dict[str, Any]] = field(default_factory=list)
    due_milestones: list[dict[str, Any]] = field(default_factory=list)
    forbidden_reveals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_locked_contract(self) -> bool:
        return bool(
            self.current_contract
            and self.current_contract.get("locked")
            and self.current_contract.get("beats")
        )

    @property
    def selected_beats(self) -> list[dict[str, Any]]:
        if not self.current_contract:
            return []
        return list(self.current_contract.get("beats") or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_chapter": self.target_chapter,
            "story_plan": self.story_plan,
            "current_volume": self.current_volume,
            "current_contract": self.current_contract,
            "lookahead": self.lookahead,
            "active_arcs": self.active_arcs,
            "due_milestones": self.due_milestones,
            "forbidden_reveals": self.forbidden_reveals,
        }

    def render_for_planner(self) -> str:
        parts: list[str] = []
        if self.story_plan:
            parts.append(
                "## 全书方向（未来计划，不是已发生事实）\n"
                f"- 方案：{self.story_plan.get('title', '')}\n"
                f"- 终局方向：{self.story_plan.get('ending_direction') or '未指定'}\n"
                f"- 阶段路线：{self.story_plan.get('phase_summary') or '未指定'}"
            )
        if self.current_volume:
            parts.append(
                "## 当前卷目标\n"
                f"《{self.current_volume.get('title', '')}》："
                f"{self.current_volume.get('synopsis') or ''}\n"
                f"Objective：{self.current_volume.get('objective') or '未指定'}"
            )
        if self.current_contract:
            beats = "\n".join(
                f"  {b.get('seq')}. [{b.get('beat_type')}] {b.get('summary')}"
                for b in self.current_contract.get("beats") or []
            )
            parts.append(
                "## 当前章已选契约\n"
                f"标题：{self.current_contract.get('title') or '未命名'}\n"
                f"目标：{self.current_contract.get('goal') or ''}\n"
                f"节拍：\n{beats or '(尚未选择 beats)'}"
            )
        if self.active_arcs:
            lines = [
                f"- [{arc.get('arc_type', 'other')}] {arc.get('title', '')}："
                f"{arc.get('objective') or arc.get('summary') or ''}"
                f"（第{arc.get('start_chapter', '?')}-{arc.get('end_chapter') or '?'}章，"
                f"优先级{arc.get('priority', 3)}）"
                for arc in self.active_arcs
            ]
            parts.append("## 活跃情节弧\n" + "\n".join(lines))
        if self.lookahead:
            lines = [
                f"- 第{x['chapter']}章：{x.get('goal') or x.get('summary') or ''}"
                + (f"；钩子：{x['hook_text']}" if x.get("hook_text") else "")
                for x in self.lookahead
            ]
            parts.append("## 后续路线（只用于铺垫，不得提前兑现）\n" + "\n".join(lines))
        if self.due_milestones:
            lines = [
                f"- {m['title']}：{m['summary']}（窗口 "
                f"{m.get('due_start_chapter') or '?'}-{m.get('due_end_chapter') or '?'} 章）"
                for m in self.due_milestones
            ]
            parts.append("## 活跃里程碑\n" + "\n".join(lines))
        if self.forbidden_reveals:
            lines = [
                f"- 第 {m['not_before_chapter']} 章前禁止兑现：{m['title']}（{m['summary']}）"
                for m in self.forbidden_reveals
            ]
            parts.append("## 禁止提前揭示/兑现\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def render_guardrails_for_draft(self) -> str:
        parts: list[str] = []
        if self.lookahead:
            parts.append(
                "后续 1-5 章将继续推进："
                + "；".join(
                    f"第{x['chapter']}章 {x.get('goal') or x.get('summary') or ''}"
                    for x in self.lookahead[:5]
                )
            )
        if self.forbidden_reveals:
            parts.append(
                "本章禁止提前兑现："
                + "；".join(
                    f"{m['title']}（不得早于第{m['not_before_chapter']}章）"
                    for m in self.forbidden_reveals[:8]
                )
            )
        if self.due_milestones:
            parts.append(
                "本章应服务的长期里程碑："
                + "；".join(m["title"] for m in self.due_milestones[:8])
            )
        if not parts:
            return ""
        return (
            "## 未来剧情护栏（作者视角；绝不能写成已经发生）\n"
            + "\n".join(f"- {p}" for p in parts)
        )


def _chapter_contract(conn: sqlite3.Connection, chapter: int) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT chapter, title, goal, summary, hook_text, status, target_emotion,"
            " opening_hook_type, hook_type, expectation_score, plan_version, locked,"
            " source_decision_id FROM chapter_cards WHERE chapter=?",
            (chapter,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    value = dict(row)
    value["beats"] = _rows(
        conn,
        "SELECT seq, beat_type, summary, value_axis, arc_id, constraint_level,"
        " source_decision_id FROM beats WHERE chapter=? AND status='planned' ORDER BY seq",
        (chapter,),
    )
    return value


def gather_plan_context(
    conn: sqlite3.Connection,
    chapter: int,
    *,
    lookahead_chapters: int = 5,
) -> PlanContext:
    """Return future author intent for `chapter` without weakening as-of truth guards."""
    plan_rows = _rows(
        conn,
        "SELECT * FROM story_plans WHERE status='active' ORDER BY version DESC LIMIT 1",
    )
    story_plan = plan_rows[0] if plan_rows else None
    if story_plan:
        story_plan["constraints"] = _json(story_plan.pop("constraints_json", "{}"), {})

    volume_rows = _rows(
        conn,
        "SELECT volume_no, title, synopsis, objective, key_results, start_chapter, end_chapter"
        " FROM volumes WHERE start_chapter IS NOT NULL AND start_chapter<=?"
        " AND (end_chapter IS NULL OR end_chapter>=?) ORDER BY volume_no LIMIT 1",
        (chapter, chapter),
    )
    current_volume = volume_rows[0] if volume_rows else None
    if current_volume:
        current_volume["key_results"] = _json(current_volume.get("key_results"), [])

    current_contract = _chapter_contract(conn, chapter)
    lookahead: list[dict[str, Any]] = []
    for row in _rows(
        conn,
        "SELECT chapter, title, goal, summary, hook_text, target_emotion, locked"
        " FROM chapter_cards WHERE chapter>? AND chapter<=? AND status='planned'"
        " ORDER BY chapter",
        (chapter, chapter + max(0, lookahead_chapters)),
    ):
        row["beats"] = _rows(
            conn,
            "SELECT seq, beat_type, summary, value_axis, arc_id, constraint_level"
            " FROM beats WHERE chapter=? AND status='planned' ORDER BY seq",
            (row["chapter"],),
        )
        lookahead.append(row)

    active_arcs = _rows(
        conn,
        "SELECT id, arc_type, title, summary, objective, start_chapter, end_chapter,"
        " priority, status, constraints_json FROM story_arcs"
        " WHERE status IN ('planned','active') AND start_chapter<=?"
        " AND (end_chapter IS NULL OR end_chapter>=?) ORDER BY priority DESC, start_chapter",
        (chapter + lookahead_chapters, chapter),
    )
    for arc in active_arcs:
        arc["constraints"] = _json(arc.pop("constraints_json", "{}"), {})

    milestone_rows = _rows(
        conn,
        "SELECT m.*, a.title AS arc_title FROM arc_milestones m"
        " JOIN story_arcs a ON a.id=m.arc_id"
        " WHERE m.status IN ('planned','active','deferred')"
        " AND (m.due_start_chapter IS NULL OR m.due_start_chapter<=?)"
        " AND (m.due_end_chapter IS NULL OR m.due_end_chapter>=?)"
        " ORDER BY COALESCE(m.due_end_chapter, 999999), a.priority DESC",
        (chapter + lookahead_chapters, chapter - 1),
    )
    for milestone in milestone_rows:
        milestone["target_state"] = _json(milestone.pop("target_state_json", "{}"), {})

    forbidden = _rows(
        conn,
        "SELECT m.*, a.title AS arc_title FROM arc_milestones m"
        " JOIN story_arcs a ON a.id=m.arc_id"
        " WHERE m.status IN ('planned','active','deferred')"
        " AND m.not_before_chapter>? ORDER BY m.not_before_chapter LIMIT 20",
        (chapter,),
    )
    for milestone in forbidden:
        milestone["target_state"] = _json(milestone.pop("target_state_json", "{}"), {})

    return PlanContext(
        target_chapter=chapter,
        story_plan=story_plan,
        current_volume=current_volume,
        current_contract=current_contract,
        lookahead=lookahead,
        active_arcs=active_arcs,
        due_milestones=milestone_rows,
        forbidden_reveals=forbidden,
    )
