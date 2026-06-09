"""ColdExtractSkill：从已有正文中反向抽取 BibleChangeProposal（§9.4 冷启动）。

输入：
  workspace["source_text"]   已有章节正文（str）
  workspace["chapter_no"]    章节号（int）

输出：
  workspace["proposals"]     list[BibleChangeProposal dict]
  workspace["raw_response"]  LLM 原始输出

所有结果进 staging（fact_candidates），永不自动 canon，由人工审核。
"""
from __future__ import annotations

import json
import re

from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..control_plane.llm.tiers import ModelTier


_CONTRACT = SkillContract(
    name="cold_extract",
    version="1.0",
    trigger=SkillTrigger.MANUAL,
    model_tier=ModelTier.MID,
    inputs=IOSpec(["source_text", "chapter_no"], "已有章节正文 + 章节号"),
    outputs=IOSpec(["proposals"], "BibleChangeProposal 列表（全部为 staging）"),
    dod=[
        DoDCheck("has_proposals", "从正文中提取到至少 1 条提案"),
    ],
    read_scopes=["workspace"],
    write_scopes=["workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter"],
    description="从已有正文中反向抽取世界状态变化，全部进 staging 待人工审核",
)

_SYSTEM = """\
你是 NovelForge 的信息抽取助手，负责从已有中文网文章节中识别**世界状态变化**。

## 任务
阅读给定的章节正文，提取出其中涉及的以下类型的事实变化：
- 人物境界突破、能力获得（fact_type: power_system / power_rank）
- 人物性格/外貌/身份变化（fact_type: character_trait）
- 人物关系建立/破裂（fact_type: relationship）
- 道具获得/失去（fact_type: item）
- 知识/情报传递（fact_type: knowledge）
- 关键事件（fact_type: event）
- 世界规则/设定（fact_type: world_rule）
- 地点信息（fact_type: location）
- 数值变化（fact_type: numeric）
- 风格/文风设定（fact_type: style）

## 输出格式
只输出一个 JSON 代码块：

```proposals
[
  {
    "op": "add",
    "fact_type": "power_rank",
    "entity": "人物名",
    "new": {
      "subject": "人物名",
      "predicate": "修炼境界",
      "object": "筑基期",
      "detail": "第X章突破"
    },
    "valid_from_chapter": 章节号,
    "risk_tier": "low"
  }
]
```

## 提取规则
- 只提取**文中明确描写**的信息，不要推断
- 每个实体的每个属性变化单独一条提案
- risk_tier: low（细节）/ medium（人物弧线）/ high（世界规则/重大retcon）
- 若无任何明确的世界状态变化，返回空数组 []
- 只输出上述代码块，不要其他解释
"""


class ColdExtractSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        source_text: str = ctx.workspace.get("source_text", "")
        chapter_no: int = ctx.workspace.get("chapter_no", ctx.target_chapter)

        if not source_text.strip():
            ctx.workspace["proposals"] = []
            return SkillResult(
                skill_name="cold_extract",
                ok=False,
                payload={"proposals": []},
                dod_outcomes=[DoDOutcome("has_proposals", passed=False,
                                        detail="source_text 为空")],
                usage_summary="skipped",
            )

        user_msg = f"第 {chapter_no} 章正文如下：\n\n{source_text[:8000]}"

        from ..control_plane.llm.provider import Message
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=user_msg)],
            system=_SYSTEM,
            max_tokens=4096,
        )

        proposals = _parse_proposals(resp.text, chapter_no)
        ctx.workspace["proposals"] = proposals
        ctx.workspace["raw_response"] = resp.text

        dod = [
            DoDOutcome("has_proposals", passed=bool(proposals),
                       detail=f"proposals={len(proposals)}"),
        ]
        return SkillResult(
            skill_name="cold_extract",
            ok=True,
            payload={"proposals": proposals},
            dod_outcomes=dod,
            usage_summary=f"in={resp.usage.input} out={resp.usage.output}",
        )


def _parse_proposals(text: str, chapter_no: int) -> list[dict]:
    m = re.search(r"```proposals\s*(.*?)\s*```", text, re.DOTALL)
    if not m:
        m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for p in data:
                p.setdefault("op", "add")
                p.setdefault("valid_from_chapter", chapter_no)
                p.setdefault("risk_tier", "low")
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return []
