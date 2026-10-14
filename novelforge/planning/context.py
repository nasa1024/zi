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


_POLARITY_LABELS = {
    "zh": {"+": "+（趋近主角目标）", "-": "-（远离目标/受挫）", "±": "±（得失参半/代价换取）"},
    "en": {"+": "+ (toward the goal)", "-": "- (away from the goal)",
           "±": "± (a gain with a cost)"},
}
_TURNING_LABELS = {
    "zh": {"action": "行动（角色的选择与行动造成转折）", "revelation": "揭示（新信息造成转折）"},
    "en": {"action": "action (a character's choice causes the turn)",
           "revelation": "revelation (new information causes the turn)"},
}
_PROMISE_ACTION_LABELS = {
    "zh": {"plant": "埋设", "remind": "提醒", "advance": "推进", "payoff": "兑现"},
    "en": {"plant": "plant", "remind": "remind", "advance": "advance", "payoff": "pay off"},
}


def _contract_turn_lines(contract: dict[str, Any], lang: str) -> list[str]:
    """v22: render the chapter's planned turn (polarity + turning point)."""
    lines: list[str] = []
    polarity = contract.get("polarity")
    if polarity in _POLARITY_LABELS[lang]:
        label = _POLARITY_LABELS[lang][polarity]
        lines.append(f"- 本章转向：{label}" if lang == "zh" else f"- Turn direction: {label}")
    turning = contract.get("turning_point")
    if turning in _TURNING_LABELS[lang]:
        label = _TURNING_LABELS[lang][turning]
        lines.append(f"- 转折方式：{label}" if lang == "zh" else f"- Turning point: {label}")
    return lines


def _promise_action_lines(contract: dict[str, Any], lang: str) -> list[str]:
    """v22: render the chapter's planned operations on reader promises."""
    out: list[str] = []
    labels = _PROMISE_ACTION_LABELS[lang]
    for item in _as_list(contract.get("promise_actions"))[:12]:
        if not isinstance(item, dict) or item.get("action") not in labels:
            continue
        name = item.get("label") or item.get("promise_id")
        if not name:
            continue
        promise_id = item.get("promise_id")
        line = f"{labels[item['action']]}：{name}" if lang == "zh" else f"{labels[item['action']]}: {name}"
        if promise_id and item.get("label"):
            line += f"（{promise_id}）" if lang == "zh" else f" ({promise_id})"
        if item.get("note"):
            line += f" — {item['note']}"
        out.append(line)
    return out


def _beat_rows(conn: sqlite3.Connection, columns: str, table_sql: str, params=()) -> list[dict]:
    """Select beats with the v22 ``witnesses_json`` column, decoded to ``witnesses``.

    Pre-v22 databases (never migrated) fall back to the legacy column set.
    """
    try:
        rows = [
            dict(r) for r in conn.execute(
                f"SELECT {columns}, witnesses_json {table_sql}", params,
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return _rows(conn, f"SELECT {columns} {table_sql}", params)
    for row in rows:
        row["witnesses"] = _json(row.pop("witnesses_json", None), [])
    return rows


_CONTRACT_LIST_FIELDS = (
    "arc_targets",
    "volume_kr_targets",
    "milestone_targets",
    "must_advance",
    "must_resolve",
    "must_include_entities",
    "must_not_reveal",
    "must_not_resolve",
    "forbidden_state_changes",
    "success_criteria",
)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        parsed = _json(value, None)
        if isinstance(parsed, list):
            return parsed
        return [value] if value.strip() else []
    return [value]


def _bullet_block(title: str, values: Any, *, limit: int = 12) -> str:
    items = [str(item).strip() for item in _as_list(values) if str(item).strip()]
    if not items:
        return ""
    return title + "\n" + "\n".join(f"- {item}" for item in items[:limit])


def _kr_text(item: Any) -> str:
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("title") or item.get("id") or "").strip()
        status = str(item.get("status") or "pending")
        return f"[{status}] {text}" if text else ""
    return str(item).strip()


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

    def render_for_planner(self, language: str = "zh-CN") -> str:
        if str(language).startswith("en"):
            return _render_for_planner_en(self)
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

    def render_contract_for_draft(self, language: str = "zh-CN") -> str:
        """Render the immutable story anchors and the complete chapter contract.

        This is deliberately separate from historical recall: everything in
        this block is author intent, never evidence that an event has already
        happened.  The renderer also understands v16 cards, so older projects
        degrade to the fields they actually possess.
        """
        if str(language).startswith("en"):
            return _render_contract_for_draft_en(self)
        parts: list[str] = []

        if self.story_plan:
            story_lines = [
                f"- 核心前提：{self.story_plan.get('premise') or '未指定'}",
                f"- 终局方向：{self.story_plan.get('ending_direction') or '未指定'}",
            ]
            if self.story_plan.get("phase_summary"):
                story_lines.append(f"- 阶段路线：{self.story_plan['phase_summary']}")
            constraints = self.story_plan.get("constraints") or {}
            if constraints:
                story_lines.append(
                    "- 全书约束：" + json.dumps(constraints, ensure_ascii=False, separators=(",", ":"))
                )
            parts.append("## 全书不变锚点（未来计划，不是已发生事实）\n" + "\n".join(story_lines))

        if self.current_volume:
            volume_lines = [
                f"- 卷名：《{self.current_volume.get('title') or '未命名'}》",
                f"- Objective：{self.current_volume.get('objective') or '未指定'}",
            ]
            synopsis = self.current_volume.get("synopsis")
            if synopsis:
                volume_lines.append(f"- 卷纲：{synopsis}")
            krs = [_kr_text(item) for item in _as_list(self.current_volume.get("key_results"))]
            krs = [item for item in krs if item]
            if krs:
                volume_lines.append("- Key Results：\n  - " + "\n  - ".join(krs[:12]))
            parts.append("## 当前卷契约\n" + "\n".join(volume_lines))

        contract = self.current_contract or {}
        if contract:
            chapter_lines = [
                f"- 章节：第 {contract.get('chapter') or self.target_chapter} 章",
                f"- 标题：{contract.get('title') or '未命名'}",
                f"- 核心目标：{contract.get('goal') or contract.get('summary') or '未指定'}",
            ]
            if contract.get("target_emotion"):
                chapter_lines.append(f"- 目标情绪：{contract['target_emotion']}")
            if contract.get("hook_text"):
                chapter_lines.append(f"- 章末承诺：{contract['hook_text']}")
            chapter_lines.extend(_contract_turn_lines(contract, "zh"))
            parts.append("## 本章硬契约\n" + "\n".join(chapter_lines))

            required_blocks = [
                _bullet_block("### 必须推进", contract.get("must_advance")),
                _bullet_block("### 承诺操作（伏笔埋设/提醒/推进/兑现）",
                              _promise_action_lines(contract, "zh")),
                _bullet_block("### 必须解决", contract.get("must_resolve")),
                _bullet_block("### 必须出场实体", contract.get("must_include_entities")),
                _bullet_block("### 服务的情节弧", contract.get("arc_targets")),
                _bullet_block("### 服务的卷 KR", contract.get("volume_kr_targets")),
                _bullet_block("### 服务的里程碑", contract.get("milestone_targets")),
            ]
            required = [block for block in required_blocks if block]
            if required:
                parts.append("\n\n".join(required))

            forbidden_blocks = [
                _bullet_block("### 禁止揭示", contract.get("must_not_reveal")),
                _bullet_block("### 禁止解决", contract.get("must_not_resolve")),
                _bullet_block("### 禁止状态变化", contract.get("forbidden_state_changes")),
            ]
            forbidden = [block for block in forbidden_blocks if block]
            if forbidden:
                parts.append("\n\n".join(forbidden))

            target_state = contract.get("target_state") or {}
            if target_state:
                parts.append(
                    "### 完成后目标状态\n```json\n"
                    + json.dumps(target_state, ensure_ascii=False, indent=2)[:2400]
                    + "\n```"
                )
            success = _bullet_block("### 成功标准", contract.get("success_criteria"))
            if success:
                parts.append(success)

        if self.active_arcs:
            lines = []
            for arc in self.active_arcs[:12]:
                lines.append(
                    f"- {arc.get('id') or '?'} · {arc.get('title') or '未命名'}："
                    f"{arc.get('objective') or arc.get('summary') or ''}"
                )
            parts.append("## 当前活跃情节弧\n" + "\n".join(lines))

        guardrails = self.render_guardrails_for_draft()
        if guardrails:
            parts.append(guardrails)

        return "\n\n".join(parts)

    def render_guardrails_for_draft(self, language: str = "zh-CN") -> str:
        if str(language).startswith("en"):
            return _render_guardrails_for_draft_en(self)
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


def _render_for_planner_en(context: PlanContext) -> str:
    parts: list[str] = []
    if context.story_plan:
        parts.append(
            "## Whole-story direction (future intent, not established fact)\n"
            f"- Plan: {context.story_plan.get('title', '')}\n"
            f"- Ending direction: {context.story_plan.get('ending_direction') or 'Not specified'}\n"
            f"- Phase route: {context.story_plan.get('phase_summary') or 'Not specified'}"
        )
    if context.current_volume:
        parts.append(
            "## Current volume objective\n"
            f"{context.current_volume.get('title', '')}: "
            f"{context.current_volume.get('synopsis') or ''}\n"
            f"Objective: {context.current_volume.get('objective') or 'Not specified'}"
        )
    if context.current_contract:
        beats = "\n".join(
            f"  {beat.get('seq')}. [{beat.get('beat_type')}] {beat.get('summary')}"
            for beat in context.current_contract.get("beats") or []
        )
        parts.append(
            "## Selected chapter contract\n"
            f"Title: {context.current_contract.get('title') or 'Untitled'}\n"
            f"Goal: {context.current_contract.get('goal') or ''}\n"
            f"Beats:\n{beats or '(No beats selected yet)'}"
        )
    if context.active_arcs:
        lines = [
            f"- [{arc.get('arc_type', 'other')}] {arc.get('title', '')}: "
            f"{arc.get('objective') or arc.get('summary') or ''} "
            f"(Chapters {arc.get('start_chapter', '?')}-{arc.get('end_chapter') or '?'}, "
            f"priority {arc.get('priority', 3)})"
            for arc in context.active_arcs
        ]
        parts.append("## Active story arcs\n" + "\n".join(lines))
    if context.lookahead:
        lines = [
            f"- Chapter {item['chapter']}: {item.get('goal') or item.get('summary') or ''}"
            + (f"; hook: {item['hook_text']}" if item.get("hook_text") else "")
            for item in context.lookahead
        ]
        parts.append(
            "## Upcoming route (seed only; do not resolve early)\n" + "\n".join(lines)
        )
    if context.due_milestones:
        lines = [
            f"- {item['title']}: {item['summary']} (window "
            f"{item.get('due_start_chapter') or '?'}-{item.get('due_end_chapter') or '?'})"
            for item in context.due_milestones
        ]
        parts.append("## Active milestones\n" + "\n".join(lines))
    if context.forbidden_reveals:
        lines = [
            f"- Do not reveal or resolve before Chapter {item['not_before_chapter']}: "
            f"{item['title']} ({item['summary']})"
            for item in context.forbidden_reveals
        ]
        parts.append("## Forbidden early reveals or resolutions\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _render_contract_for_draft_en(context: PlanContext) -> str:
    parts: list[str] = []
    if context.story_plan:
        story_lines = [
            f"- Core premise: {context.story_plan.get('premise') or 'Not specified'}",
            f"- Ending direction: {context.story_plan.get('ending_direction') or 'Not specified'}",
        ]
        if context.story_plan.get("phase_summary"):
            story_lines.append(f"- Phase route: {context.story_plan['phase_summary']}")
        constraints = context.story_plan.get("constraints") or {}
        if constraints:
            story_lines.append(
                "- Whole-story constraints: "
                + json.dumps(constraints, ensure_ascii=False, separators=(",", ":"))
            )
        parts.append(
            "## Immutable story anchors (future intent, not established fact)\n"
            + "\n".join(story_lines)
        )

    if context.current_volume:
        volume_lines = [
            f"- Volume: {context.current_volume.get('title') or 'Untitled'}",
            f"- Objective: {context.current_volume.get('objective') or 'Not specified'}",
        ]
        if synopsis := context.current_volume.get("synopsis"):
            volume_lines.append(f"- Synopsis: {synopsis}")
        key_results = [
            text for item in _as_list(context.current_volume.get("key_results"))
            if (text := _kr_text(item))
        ]
        if key_results:
            volume_lines.append("- Key results:\n  - " + "\n  - ".join(key_results[:12]))
        parts.append("## Current volume contract\n" + "\n".join(volume_lines))

    contract = context.current_contract or {}
    if contract:
        chapter_lines = [
            f"- Chapter: {contract.get('chapter') or context.target_chapter}",
            f"- Title: {contract.get('title') or 'Untitled'}",
            f"- Core goal: {contract.get('goal') or contract.get('summary') or 'Not specified'}",
        ]
        if contract.get("target_emotion"):
            chapter_lines.append(f"- Target emotion: {contract['target_emotion']}")
        if contract.get("hook_text"):
            chapter_lines.append(f"- Ending promise: {contract['hook_text']}")
        chapter_lines.extend(_contract_turn_lines(contract, "en"))
        parts.append("## Locked chapter contract\n" + "\n".join(chapter_lines))

        required = [
            _bullet_block("### Must advance", contract.get("must_advance")),
            _bullet_block("### Promise actions (plant / remind / advance / pay off)",
                          _promise_action_lines(contract, "en")),
            _bullet_block("### Must resolve", contract.get("must_resolve")),
            _bullet_block("### Required entities", contract.get("must_include_entities")),
            _bullet_block("### Story arcs served", contract.get("arc_targets")),
            _bullet_block("### Volume key results served", contract.get("volume_kr_targets")),
            _bullet_block("### Milestones served", contract.get("milestone_targets")),
        ]
        required = [block for block in required if block]
        if required:
            parts.append("\n\n".join(required))

        forbidden = [
            _bullet_block("### Must not reveal", contract.get("must_not_reveal")),
            _bullet_block("### Must not resolve", contract.get("must_not_resolve")),
            _bullet_block("### Forbidden state changes", contract.get("forbidden_state_changes")),
        ]
        forbidden = [block for block in forbidden if block]
        if forbidden:
            parts.append("\n\n".join(forbidden))

        if target_state := contract.get("target_state") or {}:
            parts.append(
                "### Target state after completion\n```json\n"
                + json.dumps(target_state, ensure_ascii=False, indent=2)[:2400]
                + "\n```"
            )
        if success := _bullet_block("### Success criteria", contract.get("success_criteria")):
            parts.append(success)

    if context.active_arcs:
        lines = [
            f"- {arc.get('id') or '?'} · {arc.get('title') or 'Untitled'}: "
            f"{arc.get('objective') or arc.get('summary') or ''}"
            for arc in context.active_arcs[:12]
        ]
        parts.append("## Currently active story arcs\n" + "\n".join(lines))

    if guardrails := _render_guardrails_for_draft_en(context):
        parts.append(guardrails)
    return "\n\n".join(parts)


def _render_guardrails_for_draft_en(context: PlanContext) -> str:
    lines: list[str] = []
    if context.lookahead:
        lines.append(
            "The next 1-5 chapters continue: "
            + "; ".join(
                f"Chapter {item['chapter']} {item.get('goal') or item.get('summary') or ''}"
                for item in context.lookahead[:5]
            )
        )
    if context.forbidden_reveals:
        lines.append(
            "Do not resolve early: "
            + "; ".join(
                f"{item['title']} (not before Chapter {item['not_before_chapter']})"
                for item in context.forbidden_reveals[:8]
            )
        )
    if context.due_milestones:
        lines.append(
            "Long-term milestones this chapter should serve: "
            + "; ".join(item["title"] for item in context.due_milestones[:8])
        )
    if not lines:
        return ""
    return (
        "## Future-story guardrails (author intent; never state as already happened)\n"
        + "\n".join(f"- {line}" for line in lines)
    )


def _chapter_contract(conn: sqlite3.Connection, chapter: int) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT * FROM chapter_cards WHERE chapter=?", (chapter,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    value = dict(row)
    # v20 projects project the expanded contract into *_json columns.  Older
    # databases simply omit them and continue with the v16 card fields.
    for name in _CONTRACT_LIST_FIELDS:
        raw = value.pop(f"{name}_json", None)
        value[name] = _json(raw, value.get(name) or []) if raw is not None else _as_list(value.get(name))
    raw_state = value.pop("target_state_json", None)
    if raw_state is not None:
        value["target_state"] = _json(raw_state, {})
    else:
        value["target_state"] = value.get("target_state") or {}

    # The append-only version payload is authoritative when present.  This
    # overlay also repairs a temporarily stale projection after interrupted
    # maintenance without teaching callers about storage internals.
    version_id = value.get("current_plan_version_id")
    version_beats: list[dict[str, Any]] = []
    if version_id:
        version_rows = _rows(
            conn,
            "SELECT payload_json FROM chapter_plan_versions WHERE id=? LIMIT 1",
            (version_id,),
        )
        if version_rows:
            payload = _json(version_rows[0].get("payload_json"), {})
            if isinstance(payload, dict):
                value.update(payload)
        version_beats = _beat_rows(
            conn,
            "seq, beat_type, summary, value_axis, arc_id, constraint_level",
            "FROM chapter_plan_version_beats WHERE plan_version_id=? ORDER BY seq",
            (version_id,),
        )
    value["beats"] = version_beats or _beat_rows(
        conn,
        "seq, beat_type, summary, value_axis, arc_id, constraint_level, source_decision_id",
        "FROM beats WHERE chapter=? AND status='planned' ORDER BY seq",
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
        row["beats"] = _beat_rows(
            conn,
            "seq, beat_type, summary, value_axis, arc_id, constraint_level",
            "FROM beats WHERE chapter=? AND status='planned' ORDER BY seq",
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
