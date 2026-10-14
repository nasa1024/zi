"""Generate structured story/volume/chapter-route decision options.

LLM output is validated into Pydantic contracts.  Missing credentials or invalid
output falls back to deterministic, usable choices so the decision workflow can
always be exercised locally and in tests.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from ..control_plane.llm.provider import Message
from ..control_plane.llm.runtime import build_project_gateway
from ..control_plane.llm.tiers import ModelTier
from ..craft.hooks import hook_menu
from ..db.connection import get_meta
from ..writing_profiles import (
    compile_system_prompt,
    get_project_writing_profile,
    profile_prompt_block,
)
from .models import (
    ChapterRoutePayload,
    DecisionRecommendation,
    DecisionOptionDraft,
    StoryPlanPayload,
    VolumePlanPayload,
)


T = TypeVar("T", bound=BaseModel)


def _extract_json(text: str) -> Any:
    source = text.strip()
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        pass
    for pattern in (r"```(?:json)?\s*(\[.*?\])\s*```", r"```(?:json)?\s*(\{.*?\})\s*```"):
        match = re.search(pattern, source, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    left = min((i for i in (source.find("["), source.find("{")) if i >= 0), default=-1)
    if left >= 0:
        right = max(source.rfind("]"), source.rfind("}"))
        if right > left:
            try:
                return json.loads(source[left:right + 1])
            except json.JSONDecodeError:
                pass
    return None


def _parse_option_list(model: type[T]) -> Callable[[str], list[T] | None]:
    def parse(text: str) -> list[T] | None:
        raw = _extract_json(text)
        if isinstance(raw, dict):
            raw = raw.get("options")
        if not isinstance(raw, list) or len(raw) < 2:
            return None
        try:
            return [model.model_validate(item.get("payload", item)) for item in raw[:5]]
        except (ValidationError, TypeError, AttributeError):
            return None
    return parse


def _gateway(project_id: str, db_path: str):
    """Return a configured gateway or None when no creative provider is available."""
    return build_project_gateway(
        project_id, db_path, allow_missing=True,
    )


def _next_profile_chapter(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(chapter), 0) + 1 AS chapter FROM draft_index"
        ).fetchone()
        return max(1, int(row["chapter"]))
    except (sqlite3.Error, TypeError, ValueError):
        return 1


def _setting_context(conn: sqlite3.Connection, *, genre: str, project_name: str) -> dict[str, Any]:
    keys = (
        "premise", "world_setting", "power_system", "protagonist",
        "main_conflict", "opening_hook", "target_audience", "tone",
    )
    setting = {key: get_meta(conn, key) for key in keys}
    setting["genre"] = genre
    setting["project_name"] = project_name
    rows = conn.execute(
        "SELECT rank_name FROM power_ranks ORDER BY rank_order"
    ).fetchall()
    setting["power_ranks"] = [row["rank_name"] for row in rows]
    profile = get_project_writing_profile(
        conn, chapter=_next_profile_chapter(conn),
    )
    setting["writing_profile"] = {
        "content_language": profile["content_language"],
        "target_market": profile["target_market"],
        "publication_format": profile["publication_format"],
        "primary_genre": profile.get("primary_genre"),
        "story_tags": profile.get("story_tags") or [],
        "profile_id": profile["writing_profile_id"],
        "profile_version": profile["writing_profile_version"],
        "prompt_rules": profile["prompt_rules"],
    }
    return setting


def _profile_metadata(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "content_language": profile["content_language"],
        "writing_profile_id": profile["writing_profile_id"],
        "writing_profile_version": profile["writing_profile_version"],
        "prompt_version": profile["prompt_version"],
    }


def _visible_text(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, dict):
        return " ".join(_visible_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_visible_text(item) for item in value)
    return value if isinstance(value, str) else ""


def _language_valid(values: list[BaseModel] | None, profile: dict) -> bool:
    if not values or not str(profile.get("content_language") or "").startswith("en"):
        return bool(values)
    from ..text_metrics import language_matches

    return language_matches(" ".join(_visible_text(value) for value in values), profile["content_language"])


def _run_llm_options(
    *,
    project_id: str,
    db_path: str,
    system: str,
    user: str,
    parser,
    max_tokens: int,
) -> tuple[list[BaseModel] | None, dict[str, Any]]:
    gateway = _gateway(project_id, db_path)
    if gateway is None:
        return None, {"degraded": True, "tier_used": None, "reason": "no_llm_key"}
    try:
        result = gateway.generate_validated(
            ModelTier.MID,
            [Message(role="user", content=user)],
            parse=parser,
            system=system,
            max_tokens=max_tokens,
            max_tier=ModelTier.STRONG,
        )
        if result.value:
            return list(result.value), {
                "degraded": False,
                "tier_used": result.tier_used,
                "reason": None,
            }
        return None, {
            "degraded": True,
            "tier_used": result.tier_used,
            "reason": "invalid_llm_output",
        }
    except Exception as exc:
        return None, {
            "degraded": True,
            "tier_used": None,
            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


_DECISION_JUDGE_SYSTEM = """\
你是小说创作决策裁判。请从给定候选中选择最符合当前长期路线、已发生事实、
作者提示和风险约束的一项。只输出 JSON 对象：
{"option_index": 0, "reason": "不超过120字的选择理由"}。
option_index 必须是候选数组中的合法下标。不要输出解释文字或 markdown。"""

_DECISION_JUDGE_SYSTEM_EN = """\
You are a fiction-planning decision judge. Choose the option that best fits the
long-term route, established facts, author guidance, locked writing profile, and
risk constraints. Return JSON only:
{"option_index": 0, "reason": "brief reason under 120 words"}.
option_index must be a valid zero-based index into the supplied options.
"""


def _parse_decision_recommendation(text: str) -> DecisionRecommendation | None:
    raw = _extract_json(text)
    if not isinstance(raw, dict):
        return None
    try:
        return DecisionRecommendation.model_validate(raw)
    except ValidationError:
        return None


def _fallback_recommendation(
    decision: dict[str, Any], reason: str,
) -> tuple[str, dict[str, Any]]:
    options = list(decision.get("options") or [])
    if not options:
        raise ValueError("decision has no options")
    recommended_id = decision.get("recommended_option_id")
    ranked = sorted(
        options,
        key=lambda option: (
            option.get("score") is not None,
            float(option.get("score") or 0.0),
            option.get("id") == recommended_id,
            -int(option.get("option_index") or 0),
        ),
        reverse=True,
    )
    selected = ranked[0]
    return selected["id"], {
        "source": "deterministic_fallback",
        "option_index": selected["option_index"],
        "reason": reason,
        "tier_used": None,
    }


def recommend_decision_option(
    decision: dict[str, Any],
    *,
    project_id: str,
    db_path: str,
    state_context: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run a compact FAST-tier judge for the user-facing "AI decide" action."""
    options = list(decision.get("options") or [])
    if not options:
        raise ValueError("decision has no options")

    profile: dict[str, Any] | None = None
    profile_conn: sqlite3.Connection | None = None
    try:
        profile_conn = sqlite3.connect(db_path)
        profile_conn.row_factory = sqlite3.Row
        profile_chapter = 1
        if state_context and state_context.get("current_chapter") is not None:
            try:
                profile_chapter = max(1, int(state_context["current_chapter"]))
            except (TypeError, ValueError):
                profile_chapter = 1
        profile = get_project_writing_profile(
            profile_conn, chapter=profile_chapter,
        )
    except (OSError, sqlite3.Error):
        profile = None
    finally:
        if profile_conn is not None:
            profile_conn.close()
    english = bool(
        profile and str(profile.get("content_language") or "").startswith("en")
    )

    if os.environ.get("NOVELFORGE_PROVIDER", "").strip().lower() == "fake":
        return _fallback_recommendation(
            decision,
            (
                "Offline FakeProvider: selected by option score and recommendation order"
                if english else "离线 FakeProvider：按候选评分与推荐顺序选择"
            ),
        )

    gateway = _gateway(project_id, db_path)
    if gateway is None:
        return _fallback_recommendation(
            decision,
            (
                "No creative model is configured; selected by option score and recommendation order"
                if english else "未配置创作模型：按候选评分与推荐顺序选择"
            ),
        )

    compact_options = [
        {
            "option_index": int(option.get("option_index") or index),
            "title": option.get("title"),
            "summary": option.get("summary"),
            "rationale": option.get("rationale"),
            "impact": option.get("impact") or {},
            "score": option.get("score"),
            "action": (option.get("payload") or {}).get("action"),
        }
        for index, option in enumerate(options)
    ]
    user = json.dumps({
        "stage": decision.get("stage"),
        "question": decision.get("question"),
        "decision_context": decision.get("context") or {},
        "agent_state": state_context or {},
        "options": compact_options,
    }, ensure_ascii=False, indent=2)
    system = (
        compile_system_prompt(
            _DECISION_JUDGE_SYSTEM,
            profile or {},
            english_system=_DECISION_JUDGE_SYSTEM_EN,
        )
        if english else _DECISION_JUDGE_SYSTEM
    )
    try:
        result = gateway.generate_validated(
            ModelTier.FAST,
            [Message(role="user", content=user)],
            parse=_parse_decision_recommendation,
            system=system,
            max_tokens=500,
            max_tier=ModelTier.MID,
        )
        recommendation = result.value
        if recommendation is None or recommendation.option_index >= len(options):
            return _fallback_recommendation(
                decision,
                (
                    "The model returned an invalid decision; selected deterministically"
                    if english else "模型裁决格式无效：按候选评分回退"
                ),
            )
        selected = options[recommendation.option_index]
        return selected["id"], {
            "source": "llm_judge",
            "option_index": recommendation.option_index,
            "reason": recommendation.reason[:300],
            "tier_used": result.tier_used,
        }
    except Exception as exc:
        return _fallback_recommendation(
            decision,
            (
                f"Model decision failed; selected deterministically: {type(exc).__name__}"
                if english else f"模型裁决失败，已确定性回退：{type(exc).__name__}"
            ),
        )


_STORY_SYSTEM = """\
你是长篇小说总策划。根据项目设定生成 3 个明显不同、但都能长期连载的故事方案。
候选必须按推荐度从高到低排列，第一项是你认为最适合当前项目的方案。
只输出 JSON 数组。每项字段：
title, premise, ending_direction, phase_summary, constraints, arcs。
arcs 每项字段：arc_type, title, summary, objective, start_chapter, end_chapter,
priority, constraints, milestones。
milestones 每项字段：title, summary, due_start_chapter, due_end_chapter,
not_before_chapter, target_state。target_state 必须包含 keywords 数组，供程序检测提前兑现。
未来计划是作者意图，不得描述成已经发生的 canon。不要输出解释文字。"""

_STORY_SYSTEM_EN = """\
You are zi's long-form story architect. Generate three materially different,
serial-sustainable story plans, ordered from strongest recommendation to third.
Return only a JSON array. Each item uses title, premise, ending_direction,
phase_summary, constraints, and arcs. Each arc uses arc_type, title, summary,
objective, start_chapter, end_chapter, priority, constraints, and milestones.
Each milestone uses title, summary, due_start_chapter, due_end_chapter,
not_before_chapter, and target_state; target_state includes a keywords array.
Write all user-visible values in natural American English. Plans are authorial
intent, never already-established canon. Tailor progression, clue, relationship,
comfort, or consequence beats to the selected profile rather than forcing one
universal power-fantasy rhythm.
"""


def generate_story_options(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    db_path: str,
    genre: str,
    project_name: str,
    hints: str | None = None,
) -> tuple[list[DecisionOptionDraft], dict[str, Any]]:
    setting = _setting_context(conn, genre=genre, project_name=project_name)
    profile = get_project_writing_profile(
        conn, chapter=_next_profile_chapter(conn),
    )
    is_english = profile["content_language"].startswith("en")
    if is_english:
        user = (
            profile_prompt_block(profile)
            + "\n\n## Project Setting\n" + json.dumps(setting, ensure_ascii=False, indent=2)
            + "\n\n## Task\nGenerate three whole-story directions. Each needs at least "
              "one main arc, one character arc, and three milestones spread across future chapters."
            + (f"\nAdditional direction: {hints}" if hints else "")
        )
    else:
        user = (
            profile_prompt_block(profile)
            + "\n\n## 项目设定\n" + json.dumps(setting, ensure_ascii=False, indent=2)
            + "\n\n## 任务\n生成 3 个全书方向候选；每个至少包含一条主线 arc、"
              "一条人物 arc，以及 3 个分布在未来章节的 milestones。"
            + (f"\n额外方向：{hints}" if hints else "")
        )
    values, meta = _run_llm_options(
        project_id=project_id, db_path=db_path,
        system=compile_system_prompt(_STORY_SYSTEM, profile, english_system=_STORY_SYSTEM_EN), user=user,
        parser=_parse_option_list(StoryPlanPayload), max_tokens=5000,
    )
    meta = {**meta, "writing_profile": _profile_metadata(profile)}
    if values and not _language_valid(values, profile):
        values = None
        meta = {**meta, "degraded": True, "reason": "wrong_output_language"}
    plans = values or _fallback_story_plans(setting, profile=profile)
    options = []
    for index, plan in enumerate(plans[:3]):
        p = plan if isinstance(plan, StoryPlanPayload) else StoryPlanPayload.model_validate(plan)
        options.append(DecisionOptionDraft(
            title=p.title,
            summary=p.phase_summary or p.premise or (
                "Advance the main plot in clear stages" if is_english else "分阶段推进主线"
            ),
            payload={"plan": p.model_dump()},
            rationale=(
                ("Best balance of main objective, character growth, and future milestones"
                 if index == 0 else "A distinct emphasis on pace and conflict")
                if is_english else
                ("主线目标、人物成长与未来里程碑最均衡" if index == 0
                 else "提供不同节奏和冲突重心")
            ),
            impact={
                "ending_direction": p.ending_direction,
                "arc_count": len(p.arcs),
                "milestone_count": sum(len(a.milestones) for a in p.arcs),
            },
            score=round(9.0 - index * 0.4, 1),
        ))
    return options, meta


def _fallback_story_plans(
    setting: dict[str, Any], *, profile: dict | None = None,
) -> list[StoryPlanPayload]:
    if profile and str(profile.get("content_language") or "").startswith("en"):
        premise = setting.get("premise") or "A protagonist enters a changing world and must choose how to grow within it."
        conflict = setting.get("main_conflict") or "The protagonist faces escalating opposition without abandoning their central values."
        protagonist = setting.get("protagonist") or "the protagonist"
        opening = setting.get("opening_hook") or "An urgent disruption forces the protagonist to act."
        templates = [
            ("Earned Ascent", "Build through visible, costly stages of progress", "A local victory exposes the true endgame threat", "progress"),
            ("The Hidden Pattern", "Let fair clues repeatedly revise what the characters believe", "The protagonist uncovers the truth and chooses what order should replace it", "truth"),
            ("Bonds Under Pressure", "Make alliances and relationships alter the external conflict", "The protagonist builds a coalition capable of changing the world's direction", "alliance"),
        ]
        out: list[StoryPlanPayload] = []
        for index, (title, phase, ending, keyword) in enumerate(templates):
            middle, end = 8 + index * 2, 20 + index * 4
            out.append(StoryPlanPayload.model_validate({
                "title": title, "premise": premise, "ending_direction": ending,
                "phase_summary": f"{opening} {phase}. The long arc resolves this conflict: {conflict}",
                "constraints": {
                    "preserve": ["the protagonist's core motive", "world-rule continuity"],
                    "interaction_note": "Future plans are not established canon.",
                },
                "arcs": [
                    {"arc_type": "main", "title": f"{keyword.title()} Arc", "summary": conflict,
                     "objective": f"Let {protagonist} complete the first meaningful {keyword} turn.",
                     "start_chapter": 1, "end_chapter": end, "priority": 5,
                     "milestones": [
                         {"title": "Forced to Act", "summary": opening,
                          "due_start_chapter": 1, "due_end_chapter": 3,
                          "not_before_chapter": 1, "target_state": {"keywords": []}},
                         {"title": "First Earned Result", "summary": "A costly choice produces observable movement.",
                          "due_start_chapter": middle - 2, "due_end_chapter": middle,
                          "not_before_chapter": middle - 2, "target_state": {"keywords": [keyword]}},
                         {"title": "Volume Turn", "summary": "The stage problem resolves while a larger conflict becomes visible.",
                          "due_start_chapter": end - 2, "due_end_chapter": end,
                          "not_before_chapter": end - 2, "target_state": {"keywords": ["consequence"]}},
                     ]},
                    {"arc_type": "character", "title": "Agency and Identity", "summary": f"{protagonist} moves from reaction to chosen responsibility.",
                     "objective": "External movement must change the protagonist's values or relationships.",
                     "start_chapter": 1, "end_chapter": end, "priority": 4,
                     "milestones": [{"title": "First Deliberate Choice", "summary": "The protagonist accepts a decision with a real cost.",
                                      "due_start_chapter": 5, "due_end_chapter": 10,
                                      "not_before_chapter": 5, "target_state": {"keywords": ["choice"]}}]},
                ],
            }))
        return out
    premise = setting.get("premise") or f"围绕{setting.get('genre', '通用')}世界展开成长冒险"
    conflict = setting.get("main_conflict") or "主角在不断升级的阻力中寻找自己的道路"
    protagonist = setting.get("protagonist") or "尚未定名的主角"
    opening = setting.get("opening_hook") or "一次突发事件迫使主角进入更大的世界"
    templates = [
        ("稳健成长线", "以阶段胜利逐步抬高世界尺度", "阶段性胜利后发现真正终局威胁", "成长"),
        ("谜团反转线", "围绕隐藏真相不断改写人物认知", "主角揭开真相并主动选择新的秩序", "真相"),
        ("阵营争霸线", "通过势力冲突与联盟背叛扩大格局", "主角建立或重塑决定世界走向的阵营", "阵营"),
    ]
    out: list[StoryPlanPayload] = []
    for index, (title, phase, ending, keyword) in enumerate(templates):
        start = 1
        mid = 8 + index * 2
        end = 20 + index * 4
        out.append(StoryPlanPayload.model_validate({
            "title": title,
            "premise": premise,
            "ending_direction": ending,
            "phase_summary": f"{opening}；{phase}；最终解决：{conflict}",
            "constraints": {
                "preserve": ["主角核心动机", "力量体系连续性"],
                "interaction_note": "未来计划不得作为已发生事实写入 canon",
            },
            "arcs": [
                {
                    "arc_type": "main", "title": f"{keyword}主线", "summary": conflict,
                    "objective": f"让{protagonist}完成第一阶段的{keyword}突破",
                    "start_chapter": start, "end_chapter": end, "priority": 5,
                    "milestones": [
                        {"title": "被迫入局", "summary": opening,
                         "due_start_chapter": 1, "due_end_chapter": 3,
                         "not_before_chapter": 1, "target_state": {"keywords": []}},
                        {"title": "首次阶段胜利", "summary": "主角通过代价换取可见成长",
                         "due_start_chapter": mid - 2, "due_end_chapter": mid,
                         "not_before_chapter": mid - 2, "target_state": {"keywords": [keyword]}},
                        {"title": "卷末转折", "summary": "阶段问题解决，但更大冲突被揭示",
                         "due_start_chapter": end - 2, "due_end_chapter": end,
                         "not_before_chapter": end - 2, "target_state": {"keywords": ["真相"]}},
                    ],
                },
                {
                    "arc_type": "character", "title": "主角内在成长",
                    "summary": f"{protagonist}从被动应对转为主动承担选择",
                    "objective": "外部升级必须同步推动价值观变化",
                    "start_chapter": 1, "end_chapter": end, "priority": 4,
                    "milestones": [
                        {"title": "第一次主动选择", "summary": "主角主动承担一次有代价的决定",
                         "due_start_chapter": 5, "due_end_chapter": 10,
                         "not_before_chapter": 5, "target_state": {"keywords": ["选择"]}},
                    ],
                },
            ],
        }))
    return out


_VOLUME_SYSTEM = """\
你是长篇小说卷纲策划。根据已选全书方案和当前已发生摘要，生成 3 个下一卷候选。
候选必须按推荐度从高到低排列，第一项是你认为最适合当前项目的方案。
只输出 JSON 数组；每项字段：title, synopsis, objective, key_results,
start_chapter, end_chapter。synopsis 必须写清起点、核心冲突、卷末落点与下卷钩子。
不要把未发生的计划写成 canon。"""

_VOLUME_SYSTEM_EN = """\
You are zi's long-form volume architect. From the active whole-story plan and
the summary of events that actually happened, generate three next-volume
options ordered by recommendation. Return only a JSON array with title,
synopsis, objective, key_results, start_chapter, and end_chapter. Each synopsis
states the opening condition, central pressure, volume-end outcome, and bridge
to the following volume. Write natural American English, follow the selected
profile, and never describe future intent as established canon.
"""


def generate_volume_options(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    db_path: str,
    volume_span: int = 20,
    hints: str | None = None,
) -> tuple[list[DecisionOptionDraft], dict[str, Any]]:
    next_no = int(conn.execute("SELECT COALESCE(MAX(volume_no),0)+1 AS n FROM volumes").fetchone()["n"])
    max_end = conn.execute("SELECT MAX(end_chapter) AS e FROM volumes").fetchone()["e"] or 0
    max_draft = conn.execute("SELECT MAX(chapter) AS c FROM draft_index").fetchone()["c"] or 0
    start = max(int(max_end) + 1, int(max_draft) + 1, 1)
    end = start + max(5, volume_span) - 1
    profile = get_project_writing_profile(conn, chapter=start)
    is_english = profile["content_language"].startswith("en")
    plan_row = conn.execute(
        "SELECT title, premise, ending_direction, phase_summary FROM story_plans"
        " WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    plan = dict(plan_row) if plan_row else {}
    previous = conn.execute(
        "SELECT title, rolling_summary FROM volumes WHERE rolling_summary IS NOT NULL"
        " ORDER BY volume_no DESC LIMIT 1"
    ).fetchone()
    if is_english:
        user = (
            profile_prompt_block(profile)
            + f"\n\n## Whole-Story Plan\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n"
            + (f"## Previous Volume: Actual Events\n{previous['title']}: {previous['rolling_summary']}\n" if previous else "")
            + f"## Task\nGenerate three options for Volume {next_no} (Chapters {start}-{end})."
            + (f"\nAdditional direction: {hints}" if hints else "")
        )
    else:
        user = (
            profile_prompt_block(profile)
            + f"\n\n## 全书方案\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n"
            + (f"## 上一卷已发生\n{previous['title']}：{previous['rolling_summary']}\n" if previous else "")
            + f"## 任务\n为第{next_no}卷（第{start}-{end}章）生成3个候选。"
            + (f"\n额外方向：{hints}" if hints else "")
        )
    values, meta = _run_llm_options(
        project_id=project_id, db_path=db_path,
        system=compile_system_prompt(_VOLUME_SYSTEM, profile, english_system=_VOLUME_SYSTEM_EN), user=user,
        parser=_parse_option_list(VolumePlanPayload), max_tokens=3500,
    )
    meta = {**meta, "writing_profile": _profile_metadata(profile)}
    if values and not _language_valid(values, profile):
        values = None
        meta = {**meta, "degraded": True, "reason": "wrong_output_language"}
    volumes = values or _fallback_volume_plans(plan, start, end, profile=profile)
    options = []
    for index, volume in enumerate(volumes[:3]):
        v = volume if isinstance(volume, VolumePlanPayload) else VolumePlanPayload.model_validate(volume)
        # Provider occasionally ignores exact requested range; project contract wins.
        v.start_chapter, v.end_chapter = start, end
        options.append(DecisionOptionDraft(
            title=v.title,
            summary=v.synopsis,
            payload=v.model_dump(),
            rationale=(
                ("Best bridge from the whole-story plan" if index == 0 else "An alternate volume rhythm")
                if is_english else
                ("与全书路线衔接最平衡" if index == 0 else "可选的卷节奏方案")
            ),
            impact={"chapter_range": [start, end], "key_results": v.key_results},
            score=round(8.8 - index * 0.3, 1),
        ))
    return options, meta


def _fallback_volume_plans(
    plan: dict[str, Any], start: int, end: int, *, profile: dict | None = None,
) -> list[VolumePlanPayload]:
    if profile and str(profile.get("content_language") or "").startswith("en"):
        base = plan.get("phase_summary") or plan.get("premise") or "The protagonist enters a new stage and meets stronger resistance."
        variants = [
            ("Arrival and Foothold", "establish the cast, rules, and one attainable promise", "earn a place in the next stage"),
            ("Pressure Rising", "bring the opposing force forward and let repeated choices create movement", "win the stage confrontation at a visible cost"),
            ("The Pattern Emerges", "use fair clues, misdirection, and information gaps to pull the main line", "reveal a local truth that opens a larger unknown"),
        ]
        return [VolumePlanPayload(
            title=title,
            synopsis=f"{base} This volume will {focus}; by its end, the characters {ending}, leaving a concrete bridge forward.",
            objective=ending,
            key_results=["Establish the volume's central pressure", "Complete one profile-appropriate change", ending],
            start_chapter=start, end_chapter=end,
        ) for title, focus, ending in variants]
    base = plan.get("phase_summary") or plan.get("premise") or "主角进入新阶段并面对更强阻力"
    variants = [
        ("启程与立足", "稳健建立人物、规则和首个可兑现目标", "完成立足并获得下一阶段资格"),
        ("冲突升温", "提前强化对手压力，以连续选择推动成长", "赢下阶段对抗但付出明确代价"),
        ("谜团展开", "以线索、误导和信息差牵引主线", "揭开局部真相并制造更大的未知"),
    ]
    return [VolumePlanPayload(
        title=title,
        synopsis=f"{base}。本卷采用{focus}；卷末{ending}，同时留下下一卷钩子。",
        objective=ending,
        key_results=["建立本卷核心冲突", "完成一次可验证成长", ending],
        start_chapter=start,
        end_chapter=end,
    ) for title, focus, ending in variants]


_ROUTE_SYSTEM = """\
你是小说滚动细纲策划。根据历史事实、卷纲、活跃情节弧和未来里程碑，生成 3 套
未来 5 章路线。只输出 JSON 数组；每项字段 route_title, route_summary, chapters。
候选必须按推荐度从高到低排列。历史事实是已经发生的真相；未来计划只是作者意图，
不得混淆二者，也不得为了迁就旧计划而否定已发生事实。
chapters 每项字段 chapter,title,goal,arc_targets,volume_kr_targets,milestone_targets,
must_advance,must_resolve,must_include_entities,must_not_reveal,must_not_resolve,
forbidden_state_changes,target_state,success_criteria,hook_text,target_emotion,
opening_hook_type,hook_type,expectation_score,polarity,turning_point,promise_actions,beats。
每章 3-6 个 beats，最后一项必须是 hook，并且至少一个 beat 的 constraint_level 为 hard。
beats 字段：seq,beat_type,summary,value_axis,arc_id,constraint_level,witnesses；
beat_type 取 setup/turn/payoff_beat/tension_point/aftermath/hook（aftermath=大爽点后的余韵）；
payoff_beat 的 witnesses 列出必须作出可见反应的在场角色。
polarity 为本章转向（+ 趋近目标 / - 远离目标 / ± 得失参半），同向不得连续超过 3 章；
turning_point 为 action 或 revelation；promise_actions 为
[{"promise_id"或"label","action":"plant|remind|advance|payoff"}]，长线承诺须定期提醒。
opening_hook_type 只能来自：__OPENING_HOOK_MENU__；
hook_type 只能来自：__ENDING_HOOK_MENU__。
success_criteria 必须能验证 target_state；禁止项要明确到可从正文举证。
不得提前兑现 not_before 之后的里程碑。""".replace(
    "__OPENING_HOOK_MENU__", hook_menu("opening")
).replace("__ENDING_HOOK_MENU__", hook_menu("ending"))

_ROUTE_SYSTEM_EN = """\
You are zi's rolling chapter-route architect. Using only actual history as
canon, plus the selected volume, active arcs, and future milestones as intent,
generate three alternative routes for the requested future window. Return only
a JSON array with route_title, route_summary, and chapters. Chapter items keep
the supplied stable contract keys and contain 3-6 beats, ending in a hook with
at least one hard beat. Beat types are setup, turn, payoff_beat, tension_point,
aftermath (the cooldown after a big payoff), and hook; payoff beats list the
witnesses who must visibly react. Each chapter also sets polarity ("+", "-" or
"±"; never more than 3 chapters in one direction), turning_point ("action" or
"revelation"), and promise_actions ([{"promise_id" or "label", "action":
"plant|remind|advance|payoff"}]). Write all visible values in natural American English.
Respect not_before boundaries, vary hook types, and make each chapter deliver a
profile-appropriate change instead of imposing the same payoff pattern on every genre.
"""


def generate_chapter_route_options(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    db_path: str,
    start_chapter: int,
    horizon: int = 5,
    plan_context: dict[str, Any] | None = None,
    history_context: str | None = None,
    hints: str | None = None,
) -> tuple[list[DecisionOptionDraft], dict[str, Any]]:
    horizon = max(1, min(10, horizon))
    profile = get_project_writing_profile(conn, chapter=start_chapter)
    is_english = profile["content_language"].startswith("en")
    volume = conn.execute(
        "SELECT title, synopsis, objective, key_results, start_chapter, end_chapter FROM volumes"
        " WHERE start_chapter<=? AND (end_chapter IS NULL OR end_chapter>=?)"
        " ORDER BY volume_no LIMIT 1",
        (start_chapter, start_chapter),
    ).fetchone()
    context = plan_context or {}
    if is_english:
        user = (
            profile_prompt_block(profile)
            + f"\n\n## Current Volume\n{json.dumps(dict(volume) if volume else {}, ensure_ascii=False, indent=2)}\n"
            + f"## Actual History (strictly through Chapter {start_chapter - 1})\n"
            + f"{history_context or '(No completed chapters yet.)'}\n"
            + f"## Future Plan Context\n{json.dumps(context, ensure_ascii=False, indent=2)}\n"
            + f"## Task\nPlan {horizon} chapters beginning with Chapter {start_chapter}; return three routes."
            + (f"\nAdditional direction: {hints}" if hints else "")
        )
    else:
        user = (
            profile_prompt_block(profile)
            + f"\n\n## 当前卷\n{json.dumps(dict(volume) if volume else {}, ensure_ascii=False, indent=2)}\n"
            + f"## 已发生事实与前情（严格截至第 {start_chapter - 1} 章）\n"
            + f"{history_context or '(尚无已发生章节)'}\n"
            + f"## 未来计划上下文\n{json.dumps(context, ensure_ascii=False, indent=2)}\n"
            + f"## 任务\n从第{start_chapter}章起规划{horizon}章，生成3套可选路线。"
            + (f"\n额外方向：{hints}" if hints else "")
        )
    values, meta = _run_llm_options(
        project_id=project_id, db_path=db_path,
        system=compile_system_prompt(_ROUTE_SYSTEM, profile, english_system=_ROUTE_SYSTEM_EN), user=user,
        parser=_parse_option_list(ChapterRoutePayload), max_tokens=10_000,
    )
    meta = {
        **meta,
        "writing_profile": _profile_metadata(profile),
        "history_as_of": max(0, start_chapter - 1),
        "history_chars": len(history_context or ""),
        "future_context": bool(context),
    }
    if values and not _language_valid(values, profile):
        values = None
        meta = {**meta, "degraded": True, "reason": "wrong_output_language"}
    routes = values or _fallback_routes(
        dict(volume) if volume else {}, start_chapter, horizon, profile=profile,
    )
    options = []
    for index, route in enumerate(routes[:3]):
        r = route if isinstance(route, ChapterRoutePayload) else ChapterRoutePayload.model_validate(route)
        # Re-anchor chapter numbers to the requested rolling window.
        for offset, chapter in enumerate(r.chapters[:horizon]):
            chapter.chapter = start_chapter + offset
        r.chapters = r.chapters[:horizon]
        options.append(DecisionOptionDraft(
            title=r.route_title,
            summary=r.route_summary,
            payload=r.model_dump(),
            rationale=(
                ("Best balance of movement, setup, and continuation hook"
                 if index == 0 else "A distinct pacing emphasis")
                if is_english else
                ("推进、铺垫和钩子最均衡" if index == 0 else "提供不同节奏倾向")
            ),
            impact={
                "chapter_range": [start_chapter, start_chapter + len(r.chapters) - 1],
                "chapter_count": len(r.chapters),
            },
            score=round(8.9 - index * 0.35, 1),
        ))
    return options, meta


def _fallback_routes(
    volume: dict[str, Any], start: int, horizon: int, *, profile: dict | None = None,
) -> list[ChapterRoutePayload]:
    if profile and str(profile.get("content_language") or "").startswith("en"):
        synopsis = volume.get("synopsis") or "Advance the volume's central pressure"
        variants = [
            ("Measured Advance", "Complete one concrete objective per chapter while pressure rises", "anticipation"),
            ("Pressure Forward", "Bring character conflict forward and let choices carry visible costs", "tension"),
            ("Clue by Clue", "Use fair information gaps and discoveries to sustain momentum", "curiosity"),
        ]
        routes: list[ChapterRoutePayload] = []
        for variant_index, (title, summary, emotion) in enumerate(variants):
            chapters = []
            for offset in range(horizon):
                chapter, phase = start + offset, offset + 1
                goal = f"Complete step {phase} of '{synopsis[:80]}'; {summary}."
                hook = f"End Chapter {chapter} with a new choice, clue, cost, or objective that demands action."
                chapters.append({
                    "chapter": chapter, "title": f"Step {phase}: {title}", "goal": goal,
                    "arc_targets": [], "volume_kr_targets": [], "milestone_targets": [],
                    "must_advance": [f"Complete step {phase} of the volume's central pressure"],
                    "must_resolve": [], "must_include_entities": [],
                    "must_not_reveal": ["the final truth and ultimate hidden identity"],
                    "must_not_resolve": ["the volume's final conflict"],
                    "forbidden_state_changes": [],
                    "target_state": {"route_step": phase, "momentum": "advanced"},
                    "success_criteria": ["Complete at least one must_advance item", "Create a concrete reason to continue"],
                    "hook_text": hook, "target_emotion": emotion,
                    "opening_hook_type": "conflict" if variant_index == 1 else "suspense",
                    "hook_type": "cliffhanger" if variant_index != 0 else "promise",
                    "expectation_score": min(5, 3 + (offset % 3)),
                    "beats": [
                        {"seq": 1, "beat_type": "setup", "summary": "Carry forward the situation and state the immediate objective.", "value_axis": "uncertainty to intent", "constraint_level": "hard"},
                        {"seq": 2, "beat_type": "tension_point", "summary": "Introduce an obstacle or meaningful information gap.", "value_axis": "control to pressure", "constraint_level": "soft"},
                        {"seq": 3, "beat_type": "turn", "summary": "The viewpoint character makes a choice that advances the long route.", "value_axis": "reaction to agency", "constraint_level": "hard"},
                        {"seq": 4, "beat_type": "hook", "summary": hook, "value_axis": "answer to new question", "constraint_level": "hard"},
                    ],
                })
            routes.append(ChapterRoutePayload(route_title=title, route_summary=summary, chapters=chapters))
        return routes
    synopsis = volume.get("synopsis") or "推进当前卷核心冲突"
    variants = [
        ("稳健推进", "每章推进一个明确目标，逐步抬高阻力", "期待"),
        ("冲突加速", "更早引爆人物冲突，以代价换取更强节奏", "紧张"),
        ("悬念优先", "通过线索与信息差驱动读者继续阅读", "疑惑"),
    ]
    routes: list[ChapterRoutePayload] = []
    for variant_index, (title, summary, emotion) in enumerate(variants):
        chapters = []
        for offset in range(horizon):
            chapter = start + offset
            phase = offset + 1
            goal = f"围绕“{synopsis[:60]}”完成第{phase}步；{summary}"
            hook = f"第{chapter}章末出现一个迫使主角继续行动的新问题"
            chapters.append({
                "chapter": chapter,
                "title": f"第{phase}步·{title}",
                "goal": goal,
                "arc_targets": [],
                "volume_kr_targets": [],
                "milestone_targets": [],
                "must_advance": [f"完成当前卷核心冲突的第{phase}步"],
                "must_resolve": [],
                "must_include_entities": [],
                "must_not_reveal": ["终局真相与最终幕后身份"],
                "must_not_resolve": ["当前卷终局冲突"],
                "forbidden_state_changes": [],
                "target_state": {"route_step": phase, "momentum": "advanced"},
                "success_criteria": ["至少完成一项本章必须推进的内容", "章末形成下一步行动动力"],
                "hook_text": hook,
                "target_emotion": emotion,
                "opening_hook_type": "conflict" if variant_index == 1 else "suspense",
                "hook_type": "cliffhanger" if variant_index != 0 else "promise",
                "expectation_score": min(5, 3 + (offset % 3)),
                "beats": [
                    {"seq": 1, "beat_type": "setup", "summary": "承接上章局势并明确本章短期目标",
                     "value_axis": "目标清晰↑", "constraint_level": "hard"},
                    {"seq": 2, "beat_type": "tension_point", "summary": "出现阻碍或信息差",
                     "value_axis": "压力↑", "constraint_level": "soft"},
                    {"seq": 3, "beat_type": "turn", "summary": "主角作出推进长期路线的选择",
                     "value_axis": "主动性↑", "constraint_level": "hard"},
                    {"seq": 4, "beat_type": "hook", "summary": hook,
                     "value_axis": "期待↑", "constraint_level": "hard"},
                ],
            })
        routes.append(ChapterRoutePayload(
            route_title=title,
            route_summary=summary,
            chapters=chapters,
        ))
    return routes
