"""VolumePlanSkill：卷级批量预规划（M4-④）。

输入（workspace）：
  volume_brief    {volume_no, title, synopsis, rolling_summary}
  plan_from / plan_to   规划章节范围（单次 ≤10 章，防截断）
  foreshadow_due  待回收伏笔列表 [{label, due_chapter}]
  stable_context  慢变设定（可选，与生成管线共享前缀）

输出（workspace）：
  chapter_plans   [{chapter, title, goal, hook_text, beats:[{beat_type, summary, value_axis}]}]

落库由 API 层负责（只覆盖 status='planned' 的章，保护既成事实）。
"""
from __future__ import annotations

import json
import re

from ..control_plane.llm.tiers import ModelTier
from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..craft.hooks import hook_menu
from ..writing_profiles import (
    compile_system_prompt,
    get_project_writing_profile,
    normalize_writing_selection,
)

_CONTRACT = SkillContract(
    name="volume_plan",
    version="1.0",
    trigger=SkillTrigger.MANUAL,
    model_tier=ModelTier.STRONG,
    inputs=IOSpec(["volume_brief", "plan_from", "plan_to", "foreshadow_due"],
                  "卷概要 + 规划范围 + 到期伏笔"),
    outputs=IOSpec(["chapter_plans"], "逐章章节卡（title/goal/hook + planned beats）"),
    dod=[
        DoDCheck("has_plans", "至少产出 1 章规划"),
        DoDCheck("hooks_present", "每章 hook_text 非空"),
    ],
    read_scopes=["volumes", "foreshadow", "chapter_summaries"],
    write_scopes=["workspace"],
    description="按卷大纲批量生成逐章细纲（章节卡 + planned beats），网文工艺字段显式化",
)

_SYSTEM = """\
你是 NovelForge 的卷规划师，为网文一卷生成逐章细纲（章节卡）。

## 输出格式
只输出一个 JSON 代码块：
```plans
[
  {
    "chapter": 12,
    "title": "章节名",
    "goal": "本章目标：必须含明确冲突或爽点",
    "arc_targets": ["本章服务的 story_arcs.id"],
    "volume_kr_targets": ["本章服务的 KR id"],
    "milestone_targets": ["本章服务的 arc_milestones.id"],
    "must_advance": ["本章必须推进的事件"],
    "must_resolve": ["本章必须解决的承诺"],
    "must_include_entities": ["必须登场或被明确涉及的实体"],
    "must_not_reveal": ["本章禁止提前揭示的信息"],
    "must_not_resolve": ["本章禁止提前解决的矛盾"],
    "forbidden_state_changes": ["禁止发生的状态变化"],
    "target_state": {"写完本章后应成立的故事状态": "值"},
    "success_criteria": ["可从正文举证的验收标准"],
    "hook_text": "章末悬念一句话（必填）",
    "target_emotion": "本章目标情绪词（如 紧张/扬眉吐气/悲怆）",
    "opening_hook_type": "章首钩子，从 __OPENING_HOOK_MENU__ 中选一",
    "hook_type": "章尾钩子，从 __ENDING_HOOK_MENU__ 中选一",
    "expectation_score": 4,
    "beats": [
      {"beat_type": "setup|turn|tension_point|payoff_beat|hook", "summary": "...", "value_axis": "如 安全→危机", "arc_id": null, "constraint_level": "soft|hard"}
    ]
  }
]
```

## 规划规则（网文工艺）
- 每章 goal 必须包含明确的冲突或爽点，禁止纯过渡章
- hook_text 必填：让读者点开下一章的钩子
- 相邻两章 hook_type 不得相同（钩子同质化让读者疲劳）；expectation_score 是章尾钩子期待度 1-5
- beats 每章 3-5 条，最后一条必须是 hook 类型
- 每章至少一条 hard beat；hard beat 绑定 arc_id 时必须使用上下文给出的真实 ID
- success_criteria 必须能验证 target_state，禁止项必须具体到可从正文举证
- 若给出"待回收伏笔"，必须在其到期章或之前安排回收（payoff_beat）
- 章节号严格按给定范围连续编号，不重不漏
只输出 plans 代码块，不要其他说明。
""".replace("__OPENING_HOOK_MENU__", hook_menu("opening")).replace(
    "__ENDING_HOOK_MENU__", hook_menu("ending")
)

_SYSTEM_EN = """\
You are zi's volume planner. Produce chapter cards for the requested range in
natural American English while following the locked writing profile.

Return only a `plans` JSON code block. Every item uses the stable keys chapter,
title, goal, arc_targets, volume_kr_targets, milestone_targets, must_advance,
must_resolve, must_include_entities, must_not_reveal, must_not_resolve,
forbidden_state_changes, target_state, success_criteria, hook_text,
target_emotion, opening_hook_type, hook_type, expectation_score, and beats.

Plan rules:
- Chapter numbers are consecutive and exactly match the requested range.
- Each chapter makes a profile-appropriate change: progress, clue, relationship
  movement, everyday reward, costly consequence, or another concrete turn.
- beats contain 3-5 items and finish with a hook; at least one beat is hard.
- Hooks must vary between adjacent chapters and fit the genre's reader promise.
- Use only real arc or milestone ids supplied by context.
- success_criteria and prohibitions must be observable in finished prose.
- Do not force a power fantasy payoff into every genre.
- Every user-visible value, including nested target_state values and beat
  summaries/value axes, must be natural English. Keep schema keys, enum values,
  and supplied ids unchanged.
"""


class VolumePlanSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        brief: dict = ctx.workspace.get("volume_brief", {})
        plan_from: int = ctx.workspace.get("plan_from", 1)
        plan_to: int = ctx.workspace.get("plan_to", plan_from)
        foreshadow_due: list[dict] = ctx.workspace.get("foreshadow_due", [])
        stable: str = ctx.workspace.get("stable_context", "")
        profile = ctx.workspace.get("writing_profile")
        if not isinstance(profile, dict):
            profile = (
                get_project_writing_profile(ctx.conn, chapter=ctx.target_chapter)
                if ctx.conn is not None else
                normalize_writing_selection({"content_language": "zh-CN"})
            )
        ctx.workspace["writing_profile"] = profile
        is_english = str(profile.get("content_language") or "").startswith("en")

        parts = []
        if stable:
            parts.append(stable)
        if is_english:
            parts.append(
                f"## Volume\n{brief.get('title') or 'Untitled'} (Volume {brief.get('volume_no', '?')})\n"
                f"Synopsis: {brief.get('synopsis') or '(none)'}"
            )
        else:
            parts.append(
                f"## 本卷\n《{brief.get('title', '未命名')}》（第 {brief.get('volume_no', '?')} 卷）\n"
                f"卷大纲：{brief.get('synopsis') or '（无）'}"
            )
        if brief.get("rolling_summary"):
            parts.append(
                f"## Volume So Far\n{brief['rolling_summary']}"
                if is_english else f"## 本卷至今\n{brief['rolling_summary']}"
            )
        if foreshadow_due:
            if is_english:
                fs = "; ".join(
                    f"{f.get('label')} (due by Chapter {f.get('due_chapter')})"
                    for f in foreshadow_due
                )
                parts.append(f"## Foreshadowing Due\n{fs}")
            else:
                fs = "、".join(
                    f"{f.get('label')}（第{f.get('due_chapter')}章到期）"
                    for f in foreshadow_due
                )
                parts.append(f"## 待回收伏笔\n{fs}")
        future = ctx.workspace.get("future_plan_context", "")
        if future:
            parts.append(future)
        hints = ctx.workspace.get("planning_hints", "")
        if hints:
            parts.append(
                f"## Author Guidance\n{hints}"
                if is_english else f"## 作者提示\n{hints}"
            )
        parts.append(
            f"## Planning Range\nChapters {plan_from}-{plan_to} ({plan_to - plan_from + 1} chapters)"
            if is_english else
            f"## 规划范围\n第 {plan_from} 章 至 第 {plan_to} 章（共 {plan_to - plan_from + 1} 章）"
        )
        user_msg = "\n\n".join(parts)

        from ..control_plane.llm.provider import Message
        validated = ctx.llm.generate_validated(
            ModelTier.STRONG,
            [Message(role="user", content=user_msg)],
            parse=lambda text: _parse_plans_validated(
                text,
                plan_from,
                plan_to,
                content_language=str(profile.get("content_language") or "zh-CN"),
            ),
            system=compile_system_prompt(_SYSTEM, profile, english_system=_SYSTEM_EN),
            max_tokens=8192,
            max_tier=ModelTier.STRONG,
            attempts_per_tier=2,
        )
        resp = validated.response
        plans = validated.value
        ctx.workspace["volume_plan_tier_used"] = validated.tier_used
        ctx.workspace["volume_plan_escalated"] = validated.escalated
        if plans is None:
            plans = _fallback_plans(
                brief,
                plan_from,
                plan_to,
                content_language=str(profile.get("content_language") or "zh-CN"),
            )
            reason = "volume_plan_parse_window_or_language_validation_failed"
            ctx.workspace["volume_plan_degraded"] = True
            ctx.workspace["volume_plan_fallback_reason"] = reason
            ctx.workspace["degraded"] = True
            ctx.workspace["fallback_reason"] = reason
        else:
            ctx.workspace["volume_plan_degraded"] = False
        ctx.workspace["raw_response"] = resp.text
        ctx.workspace["chapter_plans"] = plans

        dod = [
            DoDOutcome("has_plans", passed=bool(plans), detail=f"plans={len(plans)}"),
            DoDOutcome("hooks_present",
                       passed=bool(plans) and all(p.get("hook_text") for p in plans),
                       detail="逐章 hook_text 校验"),
        ]
        return SkillResult(
            skill_name="volume_plan",
            ok=bool(plans),
            payload={"chapter_plans": plans},
            dod_outcomes=dod,
            usage_summary=f"in={resp.usage.input} out={resp.usage.output}",
        )


_VALID_BEAT_TYPES = {"setup", "turn", "payoff_beat", "tension_point", "hook"}


def _parse_plans(
    text: str, plan_from: int, plan_to: int, *, content_language: str = "zh-CN",
) -> list[dict]:
    """提取 ```plans 块并解析；截断时逐步补 ] 抢救；过滤越界章节。"""
    parsed = _decode_plan_items(text)
    if parsed is None:
        return []

    from ..craft.hooks import normalize_hook_type

    plans: list[dict] = []
    for p in parsed:
        if not isinstance(p, dict):
            continue
        try:
            ch = int(p.get("chapter"))
        except (TypeError, ValueError):
            continue
        if not (plan_from <= ch <= plan_to):
            continue
        if content_language.startswith("en"):
            from ..text_metrics import language_matches
            visible = " ".join(
                str(p.get(key) or "") for key in ("title", "goal", "hook_text", "target_emotion")
            )
            if not language_matches(visible, content_language):
                continue
        beats = []
        for i, b in enumerate(p.get("beats") or []):
            if not isinstance(b, dict):
                continue
            bt = b.get("beat_type")
            if bt not in _VALID_BEAT_TYPES:
                continue
            beats.append({
                "seq": i + 1,
                "beat_type": bt,
                "summary": str(b.get("summary", ""))[:500],
                "value_axis": str(b.get("value_axis", ""))[:100] or None,
                "arc_id": str(b.get("arc_id"))[:100] if b.get("arc_id") else None,
                "constraint_level": (
                    b.get("constraint_level")
                    if b.get("constraint_level") in {"soft", "hard"}
                    else ("hard" if i == 0 or bt == "hook" else "soft")
                ),
            })
        if beats and not any(b["constraint_level"] == "hard" for b in beats):
            beats[0]["constraint_level"] = "hard"
        exp = p.get("expectation_score")
        try:
            exp = max(1, min(5, int(exp))) if exp is not None else None
        except (TypeError, ValueError):
            exp = None
        default_advance = (
            "Advance the chapter's stated objective."
            if content_language.startswith("en") else "推进本章目标"
        )
        default_success = (
            "The prose completes the stated goal and ends with a reason to continue."
            if content_language.startswith("en") else
            "完成本章 goal 并以章末钩子形成后续行动动力"
        )
        plans.append({
            "chapter": ch,
            "title": str(p.get("title", ""))[:100],
            "goal": str(p.get("goal", ""))[:1000],
            "arc_targets": _string_list(p.get("arc_targets")),
            "volume_kr_targets": _string_list(p.get("volume_kr_targets")),
            "milestone_targets": _string_list(p.get("milestone_targets")),
            "must_advance": _string_list(p.get("must_advance")) or [
                str(p.get("goal") or default_advance)[:500]
            ],
            "must_resolve": _string_list(p.get("must_resolve")),
            "must_include_entities": _string_list(p.get("must_include_entities")),
            "must_not_reveal": _string_list(p.get("must_not_reveal")),
            "must_not_resolve": _string_list(p.get("must_not_resolve")),
            "forbidden_state_changes": _string_list(p.get("forbidden_state_changes")),
            "target_state": p.get("target_state") if isinstance(p.get("target_state"), dict) else {},
            "success_criteria": _string_list(p.get("success_criteria")) or [
                default_success
            ],
            "hook_text": str(p.get("hook_text", ""))[:300],
            "target_emotion": (str(p.get("target_emotion"))[:50]
                               if p.get("target_emotion") else None),
            "opening_hook_type": normalize_hook_type(p.get("opening_hook_type"), "opening"),
            "hook_type": normalize_hook_type(p.get("hook_type"), "ending"),
            "expectation_score": exp,
            "beats": beats,
        })
    plans.sort(key=lambda x: x["chapter"])
    return plans


def _decode_plan_items(text: str) -> list | None:
    """Decode the raw array while retaining the legacy truncated-JSON rescue."""
    raw = ""
    m = re.search(r"```plans\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        m2 = re.search(r"```plans\s*(.*)", text, re.DOTALL)
        if m2:
            raw = m2.group(1)
        else:
            m3 = re.search(r"\[.*\]", text, re.DOTALL)
            raw = m3.group(0) if m3 else ""
    raw = raw.strip()
    if not raw:
        return None

    parsed = None
    for suffix in ("", "]", "}]", '"}]'):
        try:
            parsed = json.loads(raw + suffix)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, list):
        return None
    return parsed


_VISIBLE_PLAN_FIELDS = (
    "title",
    "goal",
    "must_advance",
    "must_resolve",
    "must_include_entities",
    "must_not_reveal",
    "must_not_resolve",
    "forbidden_state_changes",
    "target_state",
    "success_criteria",
    "hook_text",
    "target_emotion",
)
_CJK_VISIBLE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _parse_plans_validated(
    text: str,
    plan_from: int,
    plan_to: int,
    *,
    content_language: str = "zh-CN",
) -> list[dict] | None:
    """Return a complete, usable planning window or ``None`` for retry.

    The public ``_parse_plans`` remains intentionally tolerant for legacy
    callers.  Generation uses this stricter boundary so a syntactically valid
    partial window or a single wrong-language visible value cannot be applied.
    """
    raw_items = _decode_plan_items(text)
    expected = list(range(plan_from, plan_to + 1))
    if raw_items is None or not all(isinstance(item, dict) for item in raw_items):
        return None
    try:
        raw_chapters = [int(item.get("chapter")) for item in raw_items]
    except (TypeError, ValueError):
        return None
    if raw_chapters != expected:
        return None

    plans = _parse_plans(
        text, plan_from, plan_to, content_language=content_language,
    )
    if [plan.get("chapter") for plan in plans] != expected:
        return None

    for plan in plans:
        if not all(str(plan.get(field) or "").strip() for field in (
            "title", "goal", "hook_text",
        )):
            return None
        if not plan.get("must_advance") or not plan.get("success_criteria"):
            return None
        beats = plan.get("beats") or []
        if not (3 <= len(beats) <= 5):
            return None
        if beats[-1].get("beat_type") != "hook":
            return None
        if not any(beat.get("constraint_level") == "hard" for beat in beats):
            return None
        if not all(str(beat.get("summary") or "").strip() for beat in beats):
            return None
        if str(content_language).startswith("en") and not _english_plan_values_match(
            plan, content_language,
        ):
            return None
    return plans


def _english_plan_values_match(plan: dict, content_language: str) -> bool:
    """Validate every user-visible value while ignoring ids and schema enums."""
    from ..text_metrics import language_matches

    def matches(value, *, include_keys: bool = False) -> bool:
        if isinstance(value, str):
            if not value.strip() or not any(char.isalpha() for char in value):
                return True
            return not _CJK_VISIBLE_RE.search(value) and language_matches(
                value, content_language,
            )
        if isinstance(value, dict):
            values = list(value.values())
            if include_keys:
                values.extend(value.keys())
            return all(matches(child, include_keys=include_keys) for child in values)
        if isinstance(value, (list, tuple)):
            return all(matches(child, include_keys=include_keys) for child in value)
        return True

    for field in _VISIBLE_PLAN_FIELDS:
        if not matches(plan.get(field), include_keys=(field == "target_state")):
            return False
    return all(
        matches(beat.get("summary")) and matches(beat.get("value_axis"))
        for beat in (plan.get("beats") or [])
    )


def _fallback_plans(
    brief: dict,
    plan_from: int,
    plan_to: int,
    *,
    content_language: str,
) -> list[dict]:
    """Build a deterministic complete window in the project's language."""
    is_english = str(content_language).startswith("en")
    opening_types = ("suspense", "conflict", "anomaly", "action")
    ending_types = ("decision", "new_threat", "mystery", "relationship")
    plans: list[dict] = []
    for offset, chapter in enumerate(range(plan_from, plan_to + 1)):
        if is_english:
            title = f"Chapter {chapter}: A Consequence Takes Shape"
            goal = (
                f"Chapter {chapter} forces a concrete choice that advances the "
                "volume and changes the characters' available options."
            )
            must_advance = [
                "Advance the volume through one observable decision or consequence."
            ]
            target_state = {
                "chapter progress": "The chapter ends with a concrete change in the story state."
            }
            success = [
                "The finished prose shows the decision, its immediate result, and the next pressure."
            ]
            hook_text = "A new consequence demands action before the next opportunity closes."
            emotion = "mounting tension"
            beat_values = (
                ("setup", "Establish the immediate obstacle and what is at stake.",
                 "uncertainty to pressure", "hard"),
                ("tension_point", "Make inaction visibly more costly.",
                 "pressure to urgency", "soft"),
                ("turn", "A deliberate choice changes the available options.",
                 "urgency to consequence", "soft"),
                ("hook", "Reveal the consequence that drives the next chapter.",
                 "consequence to anticipation", "hard"),
            )
        else:
            title = f"第{chapter}章：后果初现"
            goal = f"第{chapter}章以一次明确选择推进本卷，并改变人物接下来的行动空间。"
            must_advance = ["通过可举证的选择或后果推进本卷。"]
            target_state = {"章节进展": "章末故事状态发生明确变化。"}
            success = ["正文呈现选择、即时结果与下一步压力。"]
            hook_text = "新的后果迫使人物在机会消失前采取行动。"
            emotion = "紧张递增"
            beat_values = (
                ("setup", "建立眼前阻碍与利害关系。", "不确定→压力", "hard"),
                ("tension_point", "让不行动的代价清晰上升。", "压力→紧迫", "soft"),
                ("turn", "一次主动选择改变可用方案。", "紧迫→后果", "soft"),
                ("hook", "揭示推动下一章的新后果。", "后果→期待", "hard"),
            )
        plans.append({
            "chapter": chapter,
            "title": title,
            "goal": goal,
            "arc_targets": [],
            "volume_kr_targets": [],
            "milestone_targets": [],
            "must_advance": must_advance,
            "must_resolve": [],
            "must_include_entities": [],
            "must_not_reveal": [],
            "must_not_resolve": [],
            "forbidden_state_changes": [],
            "target_state": target_state,
            "success_criteria": success,
            "hook_text": hook_text,
            "target_emotion": emotion,
            "opening_hook_type": opening_types[offset % len(opening_types)],
            "hook_type": ending_types[offset % len(ending_types)],
            "expectation_score": 3,
            "beats": [
                {
                    "seq": seq,
                    "beat_type": beat_type,
                    "summary": summary,
                    "value_axis": value_axis,
                    "arc_id": None,
                    "constraint_level": constraint_level,
                }
                for seq, (beat_type, summary, value_axis, constraint_level)
                in enumerate(beat_values, start=1)
            ],
        })
    return plans


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:500] for item in value if str(item).strip()]
