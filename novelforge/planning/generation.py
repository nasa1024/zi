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
    return setting


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

    if os.environ.get("NOVELFORGE_PROVIDER", "").strip().lower() == "fake":
        return _fallback_recommendation(
            decision, "离线 FakeProvider：按候选评分与推荐顺序选择",
        )

    gateway = _gateway(project_id, db_path)
    if gateway is None:
        return _fallback_recommendation(
            decision, "未配置创作模型：按候选评分与推荐顺序选择",
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
    try:
        result = gateway.generate_validated(
            ModelTier.FAST,
            [Message(role="user", content=user)],
            parse=_parse_decision_recommendation,
            system=_DECISION_JUDGE_SYSTEM,
            max_tokens=500,
            max_tier=ModelTier.MID,
        )
        recommendation = result.value
        if recommendation is None or recommendation.option_index >= len(options):
            return _fallback_recommendation(
                decision, "模型裁决格式无效：按候选评分回退",
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
            f"模型裁决失败，已确定性回退：{type(exc).__name__}",
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
    user = (
        "## 项目设定\n" + json.dumps(setting, ensure_ascii=False, indent=2)
        + "\n\n## 任务\n生成 3 个全书方向候选；每个至少包含一条主线 arc、"
          "一条人物 arc，以及 3 个分布在未来章节的 milestones。"
        + (f"\n额外方向：{hints}" if hints else "")
    )
    values, meta = _run_llm_options(
        project_id=project_id, db_path=db_path, system=_STORY_SYSTEM, user=user,
        parser=_parse_option_list(StoryPlanPayload), max_tokens=5000,
    )
    plans = values or _fallback_story_plans(setting)
    options = []
    for index, plan in enumerate(plans[:3]):
        p = plan if isinstance(plan, StoryPlanPayload) else StoryPlanPayload.model_validate(plan)
        options.append(DecisionOptionDraft(
            title=p.title,
            summary=p.phase_summary or p.premise or "分阶段推进主线",
            payload={"plan": p.model_dump()},
            rationale=(
                "主线目标、人物成长与未来里程碑最均衡" if index == 0
                else "提供不同节奏和冲突重心"
            ),
            impact={
                "ending_direction": p.ending_direction,
                "arc_count": len(p.arcs),
                "milestone_count": sum(len(a.milestones) for a in p.arcs),
            },
            score=round(9.0 - index * 0.4, 1),
        ))
    return options, meta


def _fallback_story_plans(setting: dict[str, Any]) -> list[StoryPlanPayload]:
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
    plan_row = conn.execute(
        "SELECT title, premise, ending_direction, phase_summary FROM story_plans"
        " WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    plan = dict(plan_row) if plan_row else {}
    previous = conn.execute(
        "SELECT title, rolling_summary FROM volumes WHERE rolling_summary IS NOT NULL"
        " ORDER BY volume_no DESC LIMIT 1"
    ).fetchone()
    user = (
        f"## 全书方案\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n"
        + (f"## 上一卷已发生\n{previous['title']}：{previous['rolling_summary']}\n" if previous else "")
        + f"## 任务\n为第{next_no}卷（第{start}-{end}章）生成3个候选。"
        + (f"\n额外方向：{hints}" if hints else "")
    )
    values, meta = _run_llm_options(
        project_id=project_id, db_path=db_path, system=_VOLUME_SYSTEM, user=user,
        parser=_parse_option_list(VolumePlanPayload), max_tokens=3500,
    )
    volumes = values or _fallback_volume_plans(plan, start, end)
    options = []
    for index, volume in enumerate(volumes[:3]):
        v = volume if isinstance(volume, VolumePlanPayload) else VolumePlanPayload.model_validate(volume)
        # Provider occasionally ignores exact requested range; project contract wins.
        v.start_chapter, v.end_chapter = start, end
        options.append(DecisionOptionDraft(
            title=v.title,
            summary=v.synopsis,
            payload=v.model_dump(),
            rationale="与全书路线衔接最平衡" if index == 0 else "可选的卷节奏方案",
            impact={"chapter_range": [start, end], "key_results": v.key_results},
            score=round(8.8 - index * 0.3, 1),
        ))
    return options, meta


def _fallback_volume_plans(plan: dict[str, Any], start: int, end: int) -> list[VolumePlanPayload]:
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
opening_hook_type,hook_type,expectation_score,beats。每章 3-6 个 beats，最后一项必须是 hook，
并且至少一个 beat 的 constraint_level 为 hard。
beats 字段：seq,beat_type,summary,value_axis,arc_id,constraint_level。
opening_hook_type 只能来自：__OPENING_HOOK_MENU__；
hook_type 只能来自：__ENDING_HOOK_MENU__。
success_criteria 必须能验证 target_state；禁止项要明确到可从正文举证。
不得提前兑现 not_before 之后的里程碑。""".replace(
    "__OPENING_HOOK_MENU__", hook_menu("opening")
).replace("__ENDING_HOOK_MENU__", hook_menu("ending"))


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
    volume = conn.execute(
        "SELECT title, synopsis, objective, key_results, start_chapter, end_chapter FROM volumes"
        " WHERE start_chapter<=? AND (end_chapter IS NULL OR end_chapter>=?)"
        " ORDER BY volume_no LIMIT 1",
        (start_chapter, start_chapter),
    ).fetchone()
    context = plan_context or {}
    user = (
        f"## 当前卷\n{json.dumps(dict(volume) if volume else {}, ensure_ascii=False, indent=2)}\n"
        f"## 已发生事实与前情（严格截至第 {start_chapter - 1} 章）\n"
        f"{history_context or '(尚无已发生章节)'}\n"
        f"## 未来计划上下文\n{json.dumps(context, ensure_ascii=False, indent=2)}\n"
        f"## 任务\n从第{start_chapter}章起规划{horizon}章，生成3套可选路线。"
        + (f"\n额外方向：{hints}" if hints else "")
    )
    values, meta = _run_llm_options(
        project_id=project_id, db_path=db_path, system=_ROUTE_SYSTEM, user=user,
        parser=_parse_option_list(ChapterRoutePayload), max_tokens=10_000,
    )
    meta = {
        **meta,
        "history_as_of": max(0, start_chapter - 1),
        "history_chars": len(history_context or ""),
        "future_context": bool(context),
    }
    routes = values or _fallback_routes(dict(volume) if volume else {}, start_chapter, horizon)
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
            rationale="推进、铺垫和钩子最均衡" if index == 0 else "提供不同节奏倾向",
            impact={
                "chapter_range": [start_chapter, start_chapter + len(r.chapters) - 1],
                "chapter_count": len(r.chapters),
            },
            score=round(8.9 - index * 0.35, 1),
        ))
    return options, meta


def _fallback_routes(volume: dict[str, Any], start: int, horizon: int) -> list[ChapterRoutePayload]:
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
                "success_criteria": ["至少完成一项 must_advance", "章末形成下一步行动动力"],
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
