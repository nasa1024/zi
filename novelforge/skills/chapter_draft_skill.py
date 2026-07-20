"""ChapterDraftSkill：生成纯章节正文候选（§07.5）。

输出：
    workspace["draft_text"]    原始草稿正文
    workspace["proposals"]     兼容旧模型输出的可选提案；允许空列表
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..control_plane.skill_base import DoDOutcome, Skill, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..control_plane.llm.tiers import ModelTier
from ..writing_profiles import compile_system_prompt, get_project_writing_profile


_CONTRACT = SkillContract(
    name="chapter_draft",
    version="1.0",
    trigger=SkillTrigger.CHAPTER_DRAFT,
    model_tier=ModelTier.STRONG,
    inputs=IOSpec(["recall_pack", "beats"], "召回包 + beat sheet"),
    outputs=IOSpec(["draft_text", "proposals"], "草稿正文 + 可选兼容提案"),
    dod=[
        DoDCheck("min_length", "草稿达到项目 writing profile 的硬下限"),
        DoDCheck("language_match", "草稿主语言与项目 content_language 一致"),
        DoDCheck("proposal_output_valid", "无提案或合法 proposals 数组"),
    ],
    read_scopes=["entities", "facts", "beats"],
    write_scopes=["drafts", "fact_candidates", "workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter"],
    description="按完整计划契约与 beat sheet 起草章节正文",
)

_SYSTEM = """\
你是 NovelForge 的创作助手，负责起草中文网文章节。

## 输出格式
请只输出一个草稿代码块：

```draft
（章节正文，纯中文，≥ 1000 字）
```

正文完成后系统会只对最终胜出稿抽取实际事件、状态变化与提案；候选阶段不要输出 proposals。
只输出以上代码块，不要其他说明。
"""

_SYSTEM_EN = """\
You are zi's fiction drafting assistant. Write one complete English-language
chapter that follows the locked world state, chapter contract, beat sheet, and
writing profile.

Return only one code block:
```draft
(the complete chapter in natural American English)
```

Stay in the configured point of view and tense. Use English dialogue
punctuation and paragraphing. Do not add commentary or proposals; state-change
extraction runs only after a final candidate is selected. Do not imitate a
named author or copy wording from a style reference.
"""


class ChapterDraftSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        recall = ctx.workspace.get("recall_pack")
        beats: list[dict] = ctx.workspace.get("beats", [])
        chapter_goal: str = ctx.workspace.get("chapter_goal", "")
        target_chars: int = ctx.extra.get("draft_target_chars", 3000)
        profile = ctx.workspace.get("writing_profile") or get_project_writing_profile(
            ctx.conn, chapter=ctx.target_chapter,
        )
        language = str(profile.get("content_language") or "zh-CN")
        is_english = language.startswith("en")
        quality_rules = profile.get("quality_rules") or {}
        target_range = quality_rules.get("chapter_word_range") or [1500, 2500]
        target_words = int(ctx.extra.get("draft_target_words") or sum(target_range) / 2)

        # M1-⑥：稳定前缀（慢变设定）在前、动态状态居中、本章任务最后——
        # 同章 draft/check/revise 与多候选生成的 user 消息共享首段字节，
        # 吃 provider 前缀缓存。stable_context 由 orchestrator 构建（已含标题头）。
        stable = ctx.workspace.get("stable_context")
        dynamic = ctx.workspace.get("dynamic_context")
        if stable is None and recall is not None:
            _s = recall.to_stable_context_str(language)
            if _s:
                stable = (
                    f"## Stable world context\n{_s}" if is_english
                    else f"## 世界设定（稳定）\n{_s}"
                )
            else:
                stable = ""
            dynamic = recall.to_dynamic_context_str(language)
        context_block = "\n\n".join(
            p for p in (
                stable or "",
                (
                    f"## Current world state\n{dynamic}" if is_english
                    else f"## 世界状态（当前）\n{dynamic}"
                ) if dynamic else "",
            ) if p
        ) or ("(No recalled context)" if is_english else "(无召回上下文)")
        beats_str = _fmt_beats(beats)

        # Prompt 顺序固定为：稳定世界 → 当前状态 → 计划契约 → 文风 →
        # 本章任务/Beat Sheet。同章多候选间字节一致，前缀缓存不受影响。
        anchor_block = ctx.workspace.get("style_anchor_block", "")
        contract_block = (
            ctx.workspace.get("chapter_contract_context")
            or ctx.workspace.get("future_guardrails", "")
        )
        if is_english:
            user_msg = (
                f"{context_block}\n\n"
                + (f"{contract_block}\n\n" if contract_block else "")
                + (f"{anchor_block}\n\n" if anchor_block else "")
                + "## Chapter Task\n"
                + f"Chapter {ctx.target_chapter}\n"
                + f"Target length: {target_range[0]}-{target_range[1]} words "
                  f"(aim for about {target_words})\n"
                + f"Chapter goal: {chapter_goal or 'Advance the story according to the profile.'}\n\n"
                + f"## Beat Sheet\n{beats_str}"
            )
        else:
            user_msg = (
                f"{context_block}\n\n"
                + (f"{contract_block}\n\n" if contract_block else "")
                + (f"{anchor_block}\n\n" if anchor_block else "")
                + f"## 本章任务\n"
                f"第 {ctx.target_chapter} 章\n"
                f"目标字数：约 {target_chars} 字\n"
                f"章节目标：{chapter_goal or '按情节发展'}\n\n"
                f"## Beat Sheet\n{beats_str}"
            )

        from ..control_plane.llm.provider import CacheHint, Message
        # 根据 provider 能力动态选 max_tokens：取模型上限的一半（留余量），最少 8192
        model_id = ctx.llm.model_for(ModelTier.STRONG)
        caps = ctx.llm._provider.capabilities(model_id)
        if is_english:
            # A safe upper bound derived from the requested word range.  English
            # prose usually needs fewer than two output tokens per word, with a
            # modest allowance for formatting and model variance.
            desired = int(target_range[1] * 1.8) + 1024
            max_out = min(caps.max_tokens_out, max(2048, desired), 32_000)
        else:
            max_out = min(caps.max_tokens_out // 2, 32_000)
            max_out = max(min(caps.max_tokens_out, 8192), max_out)

        # P0#2（generate_validated 全场景覆盖）：草稿完全无产出（接口畸形/截断成
        # 空文）、语言错误或未达到 profile 硬下限时在同档重采样一次。把这些
        # 硬约束放进 parser，避免“有文本但 DoD 失败”的稿件绕过 validated 重试。
        from ..text_metrics import count_content_units, language_matches
        hard_min = int(quality_rules.get("hard_min_words") or 1000)

        def _parse_draft(text: str):
            d, p = _parse_output(text, ctx.target_chapter)
            if not d.strip():
                return None
            # Preserve the legacy Chinese single-call behavior.  English P0
            # introduces locked word/language gates and therefore validates
            # them before accepting a generated response.
            if is_english:
                if count_content_units(d, language) < hard_min:
                    return None
                if not language_matches(d, language):
                    return None
            return d, p, text

        result = ctx.llm.generate_validated(
            ModelTier.STRONG,
            [Message(role="user", content=user_msg)],
            parse=_parse_draft,
            system=compile_system_prompt(_SYSTEM, profile, english_system=_SYSTEM_EN),
            max_tokens=max_out,
            # M3-①: 多候选时由 orchestrator 注入不同温度制造多样性
            temperature=ctx.extra.get("temperature", 1.0),
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
            max_tier=ModelTier.STRONG,
            attempts_per_tier=2,
        )
        resp = result.response
        if result.value is not None:
            draft_text, proposals, raw_text = result.value
        else:
            draft_text, proposals = _parse_output(resp.text, ctx.target_chapter)
            raw_text = resp.text
        ctx.workspace["draft_text"] = draft_text
        ctx.workspace["proposals"] = proposals
        ctx.workspace["draft_raw"] = raw_text
        ctx.workspace["draft_tier_used"] = result.tier_used

        content_count = count_content_units(draft_text, language)
        language_ok = language_matches(draft_text, language)
        proposal_output_valid = _proposal_output_valid(raw_text)
        dod = [
            DoDOutcome(
                "min_length", passed=content_count >= hard_min,
                detail=(
                    f"words={content_count}; hard_min={hard_min}"
                    if is_english else f"字数={content_count}"
                ),
            ),
            DoDOutcome(
                "language_match", passed=language_ok,
                detail=f"expected={language}",
            ),
            DoDOutcome("proposal_output_valid", passed=proposal_output_valid,
                       detail=f"proposals={len(proposals)}; valid={proposal_output_valid}"),
        ]

        return SkillResult(
            skill_name="chapter_draft",
            ok=all(d.passed for d in dod),
            payload={"draft_text": draft_text, "proposals": proposals},
            dod_outcomes=dod,
            usage_summary=f"in={resp.usage.input} out={resp.usage.output}",
        )


def _fmt_beats(beats: list[dict]) -> str:
    if not beats:
        return "(无 beats)"
    lines = []
    for i, b in enumerate(beats, 1):
        bt = b.get("beat_type", "?")
        sm = b.get("summary", "")
        va = b.get("value_axis", "")
        lines.append(f"  {i}. [{bt}] {sm} (value_axis: {va})")
    return "\n".join(lines)


def _parse_output(text: str, chapter: int) -> tuple[str, list[dict]]:
    # ── 提取 draft 块 ─────────────────────────────────────────────────────────
    draft_text = ""
    dm = re.search(r"```draft\s*(.*?)\s*```", text, re.DOTALL)
    if dm:
        draft_text = dm.group(1).strip()
    else:
        # 兜底：去掉代码块后剩余文本当草稿
        draft_text = re.sub(r"```.*?```", "", text, flags=re.DOTALL).strip()

    # ── 提取 proposals 块 ─────────────────────────────────────────────────────
    proposals: list[dict] = []

    # 优先：完整闭合的 ```proposals ... ```
    pm = re.search(r"```proposals\s*(.*?)\s*```", text, re.DOTALL)
    if pm:
        proposals = _try_parse_proposals(pm.group(1), chapter)
    else:
        # 兜底：块被截断（stop=length）——找 ```proposals 后的所有内容尝试解析
        pm2 = re.search(r"```proposals\s*(.*)", text, re.DOTALL)
        if pm2:
            proposals = _try_parse_proposals(pm2.group(1).strip(), chapter)

    return draft_text, proposals


def _try_parse_proposals(raw: str, chapter: int) -> list[dict]:
    """解析 proposals JSON；截断时逐步补全 ] 尝试抢救。"""
    raw = raw.strip()
    # 正常解析
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [_normalize_proposal(p, chapter) for p in parsed if isinstance(p, dict)]
        if isinstance(parsed, dict):
            return [_normalize_proposal(parsed, chapter)]
    except json.JSONDecodeError:
        pass
    # 截断抢救：逐步补 ]
    for suffix in ("]", "}]", "}]}"):
        try:
            parsed = json.loads(raw + suffix)
            if isinstance(parsed, list):
                return [_normalize_proposal(p, chapter) for p in parsed if isinstance(p, dict)]
        except json.JSONDecodeError:
            continue
    return []


def _proposal_output_valid(text: str) -> bool:
    """The pure-draft format and an explicit empty proposal list are valid."""
    match = re.search(r"```proposals\s*(.*?)\s*```", text, re.DOTALL)
    if match is None:
        # New-format draft candidates intentionally omit proposal extraction.
        return "```proposals" not in text
    try:
        value = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return False
    return isinstance(value, (list, dict))


def _normalize_proposal(p: dict, chapter: int) -> dict:
    from ..contracts import normalize_proposal_fact_type

    p = normalize_proposal_fact_type(p)
    p.setdefault("valid_from_chapter", chapter)
    p.setdefault("op", "add")
    p.setdefault("new", {})
    return p
