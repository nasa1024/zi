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


class PlannerSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        recall: object = ctx.workspace.get("recall_pack")
        chapter_goal: str = ctx.workspace.get("chapter_goal", "")

        # M1-⑥ 收尾：与 draft 同构——稳定前缀在前、本章规划任务在后，
        # 跨章的 planner 调用共享（system + stable）前缀吃缓存。
        stable = ctx.workspace.get("stable_context")
        dynamic = ctx.workspace.get("dynamic_context")
        if stable is None and recall is not None:
            _s = recall.to_stable_context_str()
            stable = f"## 世界设定（稳定）\n{_s}" if _s else ""
            dynamic = recall.to_dynamic_context_str()
        context_block = "\n\n".join(
            p for p in (
                stable or "",
                f"## 世界状态（当前）\n{dynamic}" if dynamic else "",
            ) if p
        ) or "(无召回上下文)"

        user_msg = (
            f"{context_block}\n\n"
            f"## 规划任务\n"
            f"当前章节：第 {ctx.target_chapter} 章\n"
            f"章节目标：{chapter_goal or '按情节自然发展'}"
        )

        from ..control_plane.llm.provider import CacheHint, Message
        model_id = ctx.llm.model_for(ModelTier.MID)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out, 4096)   # Planner 输出短，4096 够用
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=user_msg)],
            system=_SYSTEM,
            max_tokens=max_out,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
        )

        beats = _parse_beats(resp.text)
        ctx.workspace["beats"] = beats
        ctx.workspace["planner_raw"] = resp.text

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


def _parse_beats(text: str) -> list[dict]:
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
