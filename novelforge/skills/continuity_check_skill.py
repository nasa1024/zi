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

# 结构化检错清单：5 大类 19 子类（ConStory-Bench, arXiv:2603.05890 实证分类——
# 长故事一致性错误集中于这些模式，且高发于叙事中段）
_SOFT_SYSTEM = """\
你是 NovelForge 的一致性审稿员。对照下方清单逐类检查章节草稿，只报告**有原文证据**的问题：

1 时间线与情节逻辑
  1.1 绝对时间矛盾（日期/时辰与前文冲突）  1.2 时长冲突（耗时与行程/事件不符）
  1.3 同步悖论（同一时刻身处两地/两事）    1.4 无因之果（结果缺少前文铺垫）
  1.5 因果违反（果先于因）                1.6 废弃情节线（挑明的线索无后续却被遗忘）
2 人物
  2.1 记忆矛盾（忘记/虚构亲历事件）        2.2 知识不一致（知道不该知道的事，对照"知情关系"）
  2.3 能力波动（能力无理由增减，对照"当前境界"）  2.4 遗忘特技（关键时刻不用已有能力且无解释）
3 世界与场景
  3.1 世界规则违反（对照"常驻禁忌/金手指规则"）  3.2 社会规范违反（礼制/称谓/阶层错乱）
  3.3 地理矛盾（位置/距离/方位与前文不符）
4 事实细节
  4.1 外貌不符  4.2 命名混淆（人名/地名/物名漂移）  4.3 数量偏差（对照"数值事实"）
5 叙事风格
  5.1 视角混乱（POV 漂移）  5.2 基调不一致  5.3 风格漂移（文风突变/超纲词汇）

输出 JSON 数组，每条：
{"type":"soft","subclass":"2.3-能力波动","severity":"warn|block","desc":"...","span":"引用原文片段"}
severity 规则：仅当问题**明确违反上文给出的设定**（禁忌/境界/知情/数值）时用 block，其余用 warn。
若无问题，输出 []。只输出 JSON 数组。
"""


def _run_soft_check(draft_text: str, proposals: list[dict], ctx: SkillContext) -> list[dict]:
    if not draft_text:
        return []
    try:
        from ..control_plane.llm.provider import CacheHint, Message
        model_id = ctx.llm.model_for(ModelTier.MID)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out, 2048)   # 软检查只需短列表
        # M1-⑥：跨章软检查共享稳定前缀（前缀缓存命中），设定语境也让软检查更准
        stable = ctx.workspace.get("stable_context", "")
        prefix = f"{stable}\n\n" if stable else ""
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=f"{prefix}草稿：\n\n{draft_text[:6000]}\n\n提案摘要：\n{json.dumps(proposals[:5], ensure_ascii=False)}")],
            system=_SOFT_SYSTEM,
            max_tokens=max_out,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
        )
        text = resp.text.strip()
        if text.startswith("["):
            return json.loads(text)
    except Exception:
        pass
    return []
