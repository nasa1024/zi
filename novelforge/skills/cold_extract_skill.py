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
    version="1.2",
    trigger=SkillTrigger.MANUAL,
    model_tier=ModelTier.MID,
    inputs=IOSpec(["source_text", "chapter_no"], "已有章节正文 + 章节号"),
    outputs=IOSpec(["proposals"], "BibleChangeProposal 列表（全部为 staging）"),
    dod=[
        DoDCheck("has_proposals", "从正文中提取到至少 1 条提案"),
    ],
    read_scopes=["workspace"],
    write_scopes=["workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter", "writing_profile"],
    description="从已有正文中反向抽取世界状态变化，全部进 staging 待人工审核",
)

_SYSTEM = """\
你是 NovelForge 的信息抽取助手，负责从已有中文网文章节中识别**世界状态变化**。

## 任务
阅读给定的章节正文，提取出其中涉及的以下类型的事实变化：
- 人物境界突破、能力获得（fact_type: power_system）
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
    "fact_type": "power_system",
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

_EN_SYSTEM = """\
You are NovelForge's evidence-bound information extraction assistant. Extract
explicit world-state changes from an existing English-language fiction chapter.

## Fact types
- power or newly acquired ability (fact_type: power_system)
- character trait, appearance, or identity (fact_type: character_trait)
- relationship formed or broken (fact_type: relationship)
- item acquired or lost (fact_type: item)
- knowledge or intelligence transferred (fact_type: knowledge)
- consequential event (fact_type: event)
- world rule or setting fact (fact_type: world_rule)
- location fact (fact_type: location)
- numeric change (fact_type: numeric)
- project-level style fact explicitly stated in the source (fact_type: style)

## Output
Return only one JSON code block whose fence label is `proposals`. Keep the
stable schema keys and fact_type enum values exactly as shown below. Values and
evidence-derived prose must be natural English.

```proposals
[
  {
    "op": "add",
    "fact_type": "relationship",
    "entity": "Character name",
    "new": {
      "subject": "Character name",
      "predicate": "relationship",
      "object": "Trusted ally of Rowan",
      "detail": "The chapter explicitly establishes their alliance"
    },
    "valid_from_chapter": 1,
    "risk_tier": "medium"
  }
]
```

## Rules
- Extract only facts explicitly supported by the supplied chapter; never infer.
- Emit one proposal per changed attribute.
- risk_tier is low for local details, medium for character-arc implications,
  and high for world rules or major continuity changes.
- Return an empty array when there is no explicit state change.
- Do not translate names or source facts into Chinese.
- Output the code block only, with no commentary.
"""


class ColdExtractSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        source_text: str = ctx.workspace.get("source_text", "")
        chapter_no: int = ctx.workspace.get("chapter_no", ctx.target_chapter)
        profile = ctx.workspace.get("writing_profile")
        if not isinstance(profile, dict):
            if ctx.conn is not None:
                from ..writing_profiles import get_project_writing_profile

                profile = get_project_writing_profile(
                    ctx.conn, chapter=ctx.target_chapter,
                )
            else:
                from ..writing_profiles import normalize_writing_selection

                profile = normalize_writing_selection({"content_language": "zh-CN"})
        ctx.workspace["writing_profile"] = profile

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

        is_english = str(profile.get("content_language") or "").startswith("en")
        user_msg = (
            f"Source text for Chapter {chapter_no}:\n\n{source_text[:8000]}"
            if is_english else f"第 {chapter_no} 章正文如下：\n\n{source_text[:8000]}"
        )

        from ..control_plane.llm.provider import Message
        from ..writing_profiles import compile_system_prompt

        messages = [Message(role="user", content=user_msg)]
        system = compile_system_prompt(
            _SYSTEM, profile, english_system=_EN_SYSTEM,
        )
        if is_english:
            # English projects need value-level validation, not just an English
            # prompt.  A syntactically valid proposal whose entity/new/evidence
            # prose is Chinese must be retried at the stronger tier and must
            # never enter staging when every attempt fails.
            result = ctx.llm.generate_validated(
                ModelTier.MID,
                messages,
                parse=lambda text: _parse_proposals_validated(
                    text,
                    chapter_no,
                    content_language=profile["content_language"],
                    source_text=source_text,
                ),
                system=system,
                max_tokens=4096,
                max_tier=ModelTier.STRONG,
            )
            resp = result.response
            proposals = result.value if result.value is not None else []
            ctx.workspace["cold_extract_tier_used"] = result.tier_used
            ctx.workspace["cold_extract_escalated"] = result.escalated
            if result.value is None:
                ctx.workspace["degraded"] = True
                ctx.workspace["fallback_reason"] = (
                    "english_proposal_parse_or_language_validation_failed"
                )
        else:
            resp = ctx.llm.generate(
                ModelTier.MID,
                messages,
                system=system,
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
    return _parse_proposals_validated(text, chapter_no) or []


def _parse_proposals_validated(
    text: str,
    chapter_no: int,
    *,
    content_language: str | None = None,
    source_text: str = "",
) -> list[dict] | None:
    """Parse proposals for ``generate_validated``.

    ``None`` means malformed or wrong-language output and triggers another LLM
    attempt.  A real JSON ``[]`` remains a successful extraction with no facts.
    """
    m = re.search(r"```proposals\s*(.*?)\s*```", text, re.DOTALL)
    if not m:
        m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list) or not all(isinstance(p, dict) for p in data):
        return None

    from ..contracts import normalize_proposal_fact_type

    proposals: list[dict] = []
    try:
        for item in data:
            p = dict(item)
            p.setdefault("op", "add")
            p.setdefault("valid_from_chapter", chapter_no)
            p.setdefault("risk_tier", "low")
            p.update(normalize_proposal_fact_type(p))
            proposals.append(p)
    except (AttributeError, TypeError, ValueError):
        return None

    if str(content_language or "").startswith("en"):
        if not all(
            _english_proposal_values_match(
                p, content_language, source_text=source_text,
            )
            for p in proposals
        ):
            return None
    return proposals


_CJK_VALUE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _english_proposal_values_match(
    proposal: dict,
    content_language: str,
    *,
    source_text: str = "",
) -> bool:
    """Validate every user-visible string nested in an English proposal.

    Numeric and punctuation-only strings are language-neutral.  All
    letter-bearing values must match English, and CJK is rejected even when
    surrounded by otherwise English prose.
    """
    from ..text_metrics import language_matches

    def matches(value) -> bool:
        if isinstance(value, str):
            if not value.strip() or not any(char.isalpha() for char in value):
                return True
            if not _CJK_VALUE_RE.search(value) and language_matches(
                value, content_language,
            ):
                return True
            # English fiction may deliberately preserve a CJK proper noun or
            # evidence quote.  Allow only exact source-grounded values; newly
            # invented Chinese prose still fails validation and triggers retry.
            return bool(source_text and value.strip() in source_text)
        if isinstance(value, dict):
            return all(matches(child) for child in value.values())
        if isinstance(value, (list, tuple)):
            return all(matches(child) for child in value)
        return True

    return matches(proposal)
