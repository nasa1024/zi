"""ChapterDraftSkill：生成章节草稿 + BibleChangeProposal 列表（§07.5）。

输出：
  workspace["draft_text"]    原始草稿正文
  workspace["proposals"]     list[BibleChangeProposal dict]
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


_CONTRACT = SkillContract(
    name="chapter_draft",
    version="1.0",
    trigger=SkillTrigger.CHAPTER_DRAFT,
    model_tier=ModelTier.STRONG,
    inputs=IOSpec(["recall_pack", "beats"], "召回包 + beat sheet"),
    outputs=IOSpec(["draft_text", "proposals"], "草稿正文 + BibleChangeProposal 列表"),
    dod=[
        DoDCheck("min_length", "草稿字数 ≥ 1000 字"),
        DoDCheck("has_proposals", "proposals 非空（至少 1 条）"),
    ],
    read_scopes=["entities", "facts", "beats"],
    write_scopes=["drafts", "fact_candidates", "workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter"],
    description="按 beat sheet 起草章节正文，同时抽取 BibleChangeProposal",
)

_SYSTEM = """\
你是 NovelForge 的创作助手，负责起草中文网文章节。

## 输出格式
请输出两个 JSON 代码块：

### 块1: 草稿
```draft
（章节正文，纯中文，≥ 1000 字）
```

### 块2: 提案
```proposals
[
  {
    "op": "add",
    "fact_type": "power_rank|knowledge|item|numeric|timeline|appearance|world_rule",
    "entity": "实体名或ID",
    "new": {
      "subject": "...", "predicate": "...", "object": "...",
      "facet": "power|knowledge|item|numeric|timeline",
      ...字段按 fact_type 不同
    },
    "valid_from_chapter": 章节号
  }
]
```

### 提案规则
- 只提取文中**明确发生**的世界状态变化（境界突破、知情、获得道具、数值变化、关键事件）
- 不写纯叙事、情绪描写的提案
- risk_tier: low(小细节) / medium(人物弧线变化) / high(世界规则/retcon)

只输出以上两个代码块，不要其他说明。
"""


class ChapterDraftSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        recall = ctx.workspace.get("recall_pack")
        beats: list[dict] = ctx.workspace.get("beats", [])
        chapter_goal: str = ctx.workspace.get("chapter_goal", "")
        target_chars: int = ctx.extra.get("draft_target_chars", 3000)

        # M1-⑥：稳定前缀（慢变设定）在前、动态状态居中、本章任务最后——
        # 同章 draft/check/revise 与多候选生成的 user 消息共享首段字节，
        # 吃 provider 前缀缓存。stable_context 由 orchestrator 构建（已含标题头）。
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
        beats_str = _fmt_beats(beats)

        user_msg = (
            f"{context_block}\n\n"
            f"## 本章任务\n"
            f"第 {ctx.target_chapter} 章\n"
            f"目标字数：约 {target_chars} 字\n"
            f"章节目标：{chapter_goal or '按情节发展'}\n\n"
            f"## Beat Sheet\n{beats_str}"
        )

        from ..control_plane.llm.provider import Message
        # 根据 provider 能力动态选 max_tokens：取模型上限的一半（留余量），最少 8192
        model_id = ctx.llm.model_for(ModelTier.STRONG)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out // 2, 32_000)   # 草稿阶段不需要超长输出
        max_out = max(max_out, 8192)
        resp = ctx.llm.generate(
            ModelTier.STRONG,
            [Message(role="user", content=user_msg)],
            system=_SYSTEM,
            max_tokens=max_out,
        )

        draft_text, proposals = _parse_output(resp.text, ctx.target_chapter)
        ctx.workspace["draft_text"] = draft_text
        ctx.workspace["proposals"] = proposals
        ctx.workspace["draft_raw"] = resp.text

        char_count = len(draft_text)
        dod = [
            DoDOutcome("min_length", passed=char_count >= 1000,
                       detail=f"字数={char_count}"),
            DoDOutcome("has_proposals", passed=bool(proposals),
                       detail=f"proposals={len(proposals)}"),
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


def _normalize_proposal(p: dict, chapter: int) -> dict:
    p.setdefault("valid_from_chapter", chapter)
    p.setdefault("op", "add")
    p.setdefault("new", {})
    return p
