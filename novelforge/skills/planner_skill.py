"""PlannerSkill：生成章节 beat sheet（§07.5 / §09）。

输入：world state + 近期 beats + story arc 目标
输出：workspace["beats"] = [{beat_type, summary, value_axis}]
"""
from __future__ import annotations

import json
import re

from ..control_plane.skill_base import DoDOutcome, Skill, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..control_plane.llm.tiers import ModelTier
from ..writing_profiles import compile_system_prompt, get_project_writing_profile


_CONTRACT = SkillContract(
    name="planner",
    version="1.0",
    trigger=SkillTrigger.CHAPTER_START,
    model_tier=ModelTier.MID,
    inputs=IOSpec(["recall_pack", "chapter_goal"], "召回包 + 本章目标提示"),
    outputs=IOSpec(["beats"], "beats 列表，每条含 beat_type/summary/value_axis"),
    dod=[
        DoDCheck("has_beats", "beats 列表非空"),
        DoDCheck("has_hook", "末尾含 hook beat"),
    ],
    read_scopes=["beats", "entities", "facts"],
    write_scopes=["workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter"],
    description="根据世界状态和近期情节生成下一章 beat sheet",
)

_SYSTEM = """\
你是 NovelForge 的策划助手。根据提供的召回上下文和章节目标，
生成本章节的 beat sheet。要求：
- 4~8 条 beats
- beat_type 限于 setup/turn/payoff_beat/tension_point/hook
- 末尾必须有一条 hook beat 作为章末悬念
- 以 JSON 数组输出，每条字段: beat_type, summary, value_axis
- value_axis: 用一个词描述本 beat 的价值轴变化（如"希望↑"、"权力↓"）
只输出 JSON 数组，不要任何额外说明。
"""

_SYSTEM_EN = """\
You are zi's chapter planner. Build a beat sheet that follows the supplied
world state, locked chapter contract, and writing profile.
- Return a JSON array with 4-8 beats.
- beat_type must be setup, turn, payoff_beat, tension_point, or hook.
- The final beat must be a hook, but its intensity and kind must fit the profile.
- Each item has beat_type, summary, and value_axis.
- Write every user-visible value in natural American English.
- Do not force a power payoff into genres whose profile calls for clues,
  relationships, low-pressure rewards, or costly consequences.
Return only the JSON array.
"""


class PlannerSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        recall: object = ctx.workspace.get("recall_pack")
        chapter_goal: str = ctx.workspace.get("chapter_goal", "")
        profile = ctx.workspace.get("writing_profile") or get_project_writing_profile(
            ctx.conn, chapter=ctx.target_chapter,
        )
        is_english = str(profile.get("content_language") or "").startswith("en")

        # M1-⑥ 收尾：与 draft 同构——稳定前缀在前、本章规划任务在后，
        # 跨章的 planner 调用共享（system + stable）前缀吃缓存。
        stable = ctx.workspace.get("stable_context")
        dynamic = ctx.workspace.get("dynamic_context")
        if stable is None and recall is not None:
            _s = recall.to_stable_context_str(profile["content_language"])
            if _s:
                stable = (
                    f"## Stable world context\n{_s}" if is_english
                    else f"## 世界设定（稳定）\n{_s}"
                )
            else:
                stable = ""
            dynamic = recall.to_dynamic_context_str(profile["content_language"])
        context_block = "\n\n".join(
            p for p in (
                stable or "",
                (
                    f"## Current world state\n{dynamic}" if is_english
                    else f"## 世界状态（当前）\n{dynamic}"
                ) if dynamic else "",
            ) if p
        ) or ("(No recalled context)" if is_english else "(无召回上下文)")
        future_plan = ctx.workspace.get("future_plan_context", "")

        if is_english:
            user_msg = (
                f"{context_block}\n\n"
                + (f"{future_plan}\n\n" if future_plan else "")
                + "## Planning Task\n"
                + f"Chapter: {ctx.target_chapter}\n"
                + f"Chapter goal: {chapter_goal or 'Advance the story naturally within the profile.'}"
            )
        else:
            user_msg = (
                f"{context_block}\n\n"
                + (f"{future_plan}\n\n" if future_plan else "")
                + f"## 规划任务\n"
                f"当前章节：第 {ctx.target_chapter} 章\n"
                f"章节目标：{chapter_goal or '按情节自然发展'}"
            )

        from ..control_plane.llm.provider import CacheHint, Message
        model_id = ctx.llm.model_for(ModelTier.MID)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out, 4096)   # Planner 输出短，4096 够用

        # P0#2（generate_validated 全场景覆盖）：MID 起跑，JSON 畸形/无 hook 等
        # 结构性失败自动升 STRONG 抢救，而非静默落进单条 setup 兜底。
        result = ctx.llm.generate_validated(
            ModelTier.MID,
            [Message(role="user", content=user_msg)],
            parse=lambda text: _parse_beats_strict(
                text, content_language=profile["content_language"]
            ),
            system=compile_system_prompt(_SYSTEM, profile, english_system=_SYSTEM_EN),
            max_tokens=max_out,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
            max_tier=ModelTier.STRONG,
        )
        resp = result.response
        beats = result.value if result.value is not None else _parse_beats(
            resp.text, content_language=profile["content_language"]
        )
        ctx.workspace["beats"] = beats
        ctx.workspace["planner_raw"] = resp.text
        ctx.workspace["planner_tier_used"] = result.tier_used
        ctx.workspace["planner_escalated"] = result.escalated

        dod = [
            DoDOutcome("has_beats", passed=bool(beats)),
            DoDOutcome("has_hook", passed=any(b.get("beat_type") == "hook" for b in beats)),
        ]

        return SkillResult(
            skill_name="planner",
            ok=all(d.passed for d in dod),
            payload={"beats": beats},
            dod_outcomes=dod,
            usage_summary=f"in={resp.usage.input} out={resp.usage.output}",
        )


def _parse_beats_strict(
    text: str, *, content_language: str = "zh-CN",
) -> list[dict] | None:
    """generate_validated 的 parse：结构合格返回 beats，否则 None 触发升档。

    合格 = 解析出非空 JSON 数组、每条含 beat_type/summary、末尾有 hook beat
    （与 DoD has_beats/has_hook 对齐——校验器拦截即视为该档失败）。
    """
    text_s = text.strip()
    data = None
    if text_s.startswith("["):
        try:
            data = json.loads(text_s)
        except json.JSONDecodeError:
            data = None
    if data is None:
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text_s, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, list) or not data:
        return None
    beats = [b for b in data if isinstance(b, dict)
             and b.get("beat_type") and b.get("summary")]
    if not beats:
        return None
    if not any(b.get("beat_type") == "hook" for b in beats):
        return None
    if content_language.startswith("en"):
        from ..text_metrics import language_matches
        if not language_matches(
            " ".join(str(b.get("summary") or "") for b in beats), content_language
        ):
            return None
    return beats


def _parse_beats(text: str, *, content_language: str = "zh-CN") -> list[dict]:
    if content_language.startswith("en"):
        strict = _parse_beats_strict(text, content_language=content_language)
        return strict or [
            {
                "beat_type": "setup",
                "summary": "Re-establish the immediate goal and its present obstacle.",
                "value_axis": "certainty to pressure",
            },
            {
                "beat_type": "hook",
                "summary": "End on a profile-appropriate choice, clue, cost, or new objective.",
                "value_axis": "answer to question",
            },
        ]
    text = text.strip()
    # 尝试直接解析 JSON 数组
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    # 从 markdown 代码块提取
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 最后兜底：返回单条 setup beat
    return [{"beat_type": "setup", "summary": text[:100], "value_axis": "未知"}]
