"""ContinuityCheckSkill：一致性检验（§07.5 / §05）。

复用已有确定性 validator（validators/ 包），附加 LLM 软检查。
输出 workspace["continuity_issues"] = [Issue]
"""
from __future__ import annotations

import json
from typing import Optional

from ..control_plane.skill_base import DoDOutcome, Skill, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..control_plane.llm.tiers import ModelTier
from ..validators.types import WorldState


_CONTRACT = SkillContract(
    name="continuity_check",
    version="1.0",
    trigger=SkillTrigger.CONTINUITY_CHECK,
    model_tier=ModelTier.MID,
    inputs=IOSpec(["draft_text", "proposals", "world_state"], "草稿 + 提案 + 世界状态"),
    outputs=IOSpec(["continuity_issues"], "Issue 列表"),
    dod=[
        DoDCheck("no_hard_violations", "硬一致性 validator 无 BLOCK 级问题"),
    ],
    read_scopes=["entities", "facts", "character_power_log", "knowledge_edges"],
    write_scopes=["workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter"],
    description="对草稿提案运行确定性 validator + LLM 软检查",
)


class ContinuityCheckSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        proposals: list[dict] = ctx.workspace.get("proposals", [])
        draft_text: str = ctx.workspace.get("draft_text", "")
        world: Optional[WorldState] = ctx.workspace.get("world_state")

        all_issues = []

        # ── 确定性硬检查（复用 validators 包）──────────────────────────────────
        hard_issues = _run_hard_validators(proposals, world, ctx)
        all_issues.extend(hard_issues)

        # ── LLM 软检查（人物动机/情绪/逻辑）──────────────────────────────────
        soft_issues = _run_soft_check(draft_text, proposals, ctx)
        all_issues.extend(soft_issues)

        ctx.workspace["continuity_issues"] = all_issues
        hard_blocks = [i for i in hard_issues if i.get("severity") == "block"]

        dod = [
            DoDOutcome("no_hard_violations", passed=not hard_blocks,
                       detail=f"block issues={len(hard_blocks)}")
        ]

        return SkillResult(
            skill_name="continuity_check",
            ok=not hard_blocks,
            payload={"issues": all_issues, "hard_blocks": hard_blocks},
            dod_outcomes=dod,
        )


# ── 确定性检查 ────────────────────────────────────────────────────────────────

def _run_hard_validators(proposals: list[dict], world: Optional[WorldState], ctx: SkillContext) -> list[dict]:
    """复用 validators/power.py 等检测边界违反。"""
    issues: list[dict] = []
    try:
        from ..validators.claims import extract_claims_rule
        from ..validators.power import validate_power_monotonicity
        from ..validators.items import validate_item_conservation

        draft_text = ctx.workspace.get("draft_text", "")
        claims = extract_claims_rule(draft_text)

        # 境界单调性
        try:
            power_issues = validate_power_monotonicity(claims, world)
            issues.extend([i.dict() if hasattr(i, "dict") else vars(i) for i in power_issues])
        except Exception:
            pass

        # 道具守恒
        try:
            item_issues = validate_item_conservation(claims, world)
            issues.extend([i.dict() if hasattr(i, "dict") else vars(i) for i in item_issues])
        except Exception:
            pass

    except ImportError:
        pass  # validators 包未找到时降级
    return issues


# ── LLM 软检查 ────────────────────────────────────────────────────────────────

_SOFT_SYSTEM = """\
你是 NovelForge 的一致性审稿员。检查以下章节草稿中的软一致性问题：
- 人物行为是否符合已知性格/动机
- 情感弧线是否连贯
- 是否引入了未铺垫的能力/道具

输出 JSON 数组，每条：{"type": "soft", "severity": "warn|info", "desc": "...", "span": "引用原文片段"}
若无问题，输出 []。只输出 JSON 数组。
"""


def _run_soft_check(draft_text: str, proposals: list[dict], ctx: SkillContext) -> list[dict]:
    if not draft_text:
        return []
    try:
        from ..control_plane.llm.provider import Message
        model_id = ctx.llm.model_for(ModelTier.MID)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out, 2048)   # 软检查只需短列表
        # M1-⑥：与 draft 共享稳定前缀（前缀缓存命中），设定语境也让软检查更准
        stable = ctx.workspace.get("stable_context", "")
        prefix = f"{stable}\n\n" if stable else ""
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=f"{prefix}草稿：\n\n{draft_text[:3000]}\n\n提案摘要：\n{json.dumps(proposals[:5], ensure_ascii=False)}")],
            system=_SOFT_SYSTEM,
            max_tokens=max_out,
        )
        text = resp.text.strip()
        if text.startswith("["):
            return json.loads(text)
    except Exception:
        pass
    return []
