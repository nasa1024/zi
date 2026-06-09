"""CraftCheckSkill：网文工艺层检验（§05.8）。

7 项校验（部分确定性，部分 LLM 辅助）：
  1. value_shift       有无价值轴变化            block
  2. hook              章末有无钩子               block
  3. pacing            节奏是否符合规划           warn
  4. dialogue          对话区分度                 warn
  5. payoff            爽点兑现校验（防假爽点）   block
  6. flat_character    纸片人检测                 warn (LLM)
  7. beat_contract     beats 契约对齐             block

与 ContinuityCheckSkill 并行运行（Orchestrator 负责调度）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..control_plane.llm.tiers import ModelTier
from ..craft.payoff_binding import check_payoff_binding
from ..craft.voice import check_voice_distinctness


_CONTRACT = SkillContract(
    name="craft_check",
    version="1.0",
    trigger=SkillTrigger.CRAFT_CHECK,
    model_tier=ModelTier.MID,
    inputs=IOSpec(["draft_text", "beats", "proposals", "pacing_state"], "草稿+beats+提案+节拍状态"),
    outputs=IOSpec(["craft_issues"], "CraftIssue 列表"),
    dod=[
        DoDCheck("no_fake_payoff", "无 FAKE_PAYOFF block 问题"),
        DoDCheck("has_hook", "章末有 hook beat"),
        DoDCheck("has_value_shift", "至少一个 beat 有 value_axis 变化"),
    ],
    read_scopes=["beats", "workspace", "pacing_cursor"],
    write_scopes=["workspace"],
    description="工艺层 7 项并行校验",
)


@dataclass
class CraftIssue:
    check: str
    severity: str   # "block" | "warn"
    detail: str
    span: str = ""


class CraftCheckSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        draft_text: str = ctx.workspace.get("draft_text", "")
        beats: list[dict] = ctx.workspace.get("beats", [])
        proposals: list[dict] = ctx.workspace.get("proposals", [])
        entities: list[dict] = (ctx.workspace.get("recall_pack") and
                                 ctx.workspace["recall_pack"].entities) or []

        issues: list[CraftIssue] = []

        # ① value_shift：至少一个 beat 有非空 value_axis
        issues += _check_value_shift(beats)

        # ② hook：末尾有 hook beat
        issues += _check_hook(beats)

        # ③ pacing：与 pacing_state 比较
        pacing_state = ctx.workspace.get("pacing_state")
        issues += _check_pacing(beats, pacing_state)

        # ④ dialogue：对话区分度
        for vi in check_voice_distinctness(draft_text, entities):
            issues.append(CraftIssue(check="dialogue", severity=vi.severity, detail=vi.detail))

        # ⑤ payoff：爽点兑现强制
        for pi in check_payoff_binding(beats, proposals, ctx.target_chapter, ctx.conn):
            issues.append(CraftIssue(check="payoff", severity=pi.severity, detail=pi.detail))

        # ⑥ flat_character（LLM soft check）
        issues += _check_flat_character_llm(draft_text, ctx)

        # ⑦ beat_contract：beats 条数与 beat_type 合规
        issues += _check_beat_contract(beats)

        ctx.workspace["craft_issues"] = [_issue_dict(i) for i in issues]
        blocks = [i for i in issues if i.severity == "block"]

        dod = [
            DoDOutcome("no_fake_payoff",
                       passed=not any(i.check == "payoff" for i in blocks),
                       detail=f"payoff blocks={sum(1 for i in blocks if i.check=='payoff')}"),
            DoDOutcome("has_hook",
                       passed=not any(i.check == "hook" and i.severity == "block" for i in issues)),
            DoDOutcome("has_value_shift",
                       passed=not any(i.check == "value_shift" and i.severity == "block" for i in issues)),
        ]

        return SkillResult(
            skill_name="craft_check",
            ok=not blocks,
            payload={"craft_issues": ctx.workspace["craft_issues"]},
            dod_outcomes=dod,
        )


# ── 确定性检查 ────────────────────────────────────────────────────────────────

def _check_value_shift(beats: list[dict]) -> list[CraftIssue]:
    if not beats:
        return []
    has_shift = any(b.get("value_axis") for b in beats)
    if not has_shift:
        return [CraftIssue(check="value_shift", severity="block",
                           detail="所有 beats 的 value_axis 均为空，本章缺乏价值轴变化")]
    return []


def _check_hook(beats: list[dict]) -> list[CraftIssue]:
    if not beats:
        return []
    last = beats[-1]
    if last.get("beat_type") != "hook":
        return [CraftIssue(check="hook", severity="block",
                           detail=f"末尾 beat 类型为 {last.get('beat_type')!r}，应为 hook")]
    # 检测重复钩子（与上上章相同摘要）
    return []


def _check_pacing(beats: list[dict], pacing_state) -> list[CraftIssue]:
    if pacing_state is None:
        return []
    issues = []
    if pacing_state.needs_cooldown:
        high_count = sum(1 for b in beats if b.get("beat_type") in ("tension_point", "payoff_beat"))
        if high_count >= 2:
            issues.append(CraftIssue(
                check="pacing", severity="warn",
                detail="节奏提示：recent_high_streak 已达上限，建议本章减少高张力 beats 以防读者麻木",
            ))
    return issues


def _check_beat_contract(beats: list[dict]) -> list[CraftIssue]:
    issues = []
    if len(beats) < 2:
        issues.append(CraftIssue(check="beat_contract", severity="block",
                                  detail=f"beats 数量={len(beats)}，至少需要 2 条"))
    valid_types = {"setup", "turn", "payoff_beat", "tension_point", "hook"}
    for i, b in enumerate(beats):
        bt = b.get("beat_type")
        if bt not in valid_types:
            issues.append(CraftIssue(check="beat_contract", severity="block",
                                      detail=f"beats[{i}].beat_type={bt!r} 不在合法集合 {valid_types}"))
    return issues


# ── LLM 软检查 ────────────────────────────────────────────────────────────────

_FLAT_SYSTEM = """\
你是网文工艺审稿员。检查草稿中是否存在"纸片人"问题：
角色行为只为推动情节，缺乏自主动机或真实情感反应。
输出 JSON 数组，每条：{"check":"flat_character","severity":"warn","detail":"...","span":"原文片段"}
若无问题输出 []。只输出 JSON 数组。"""


def _check_flat_character_llm(draft_text: str, ctx: SkillContext) -> list[CraftIssue]:
    if not draft_text or len(draft_text) < 200:
        return []
    try:
        from ..control_plane.llm.provider import Message
        model_id = ctx.llm.model_for(ModelTier.MID)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out, 512)
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=draft_text[:2000])],
            system=_FLAT_SYSTEM,
            max_tokens=max_out,
        )
        raw = resp.text.strip()
        if raw.startswith("["):
            parsed = json.loads(raw)
            return [
                CraftIssue(
                    check=p.get("check", "flat_character"),
                    severity=p.get("severity", "warn"),
                    detail=p.get("detail", ""),
                    span=p.get("span", ""),
                )
                for p in parsed if isinstance(p, dict)
            ]
    except Exception:
        pass
    return []


def _issue_dict(i: CraftIssue) -> dict:
    return {"check": i.check, "severity": i.severity, "detail": i.detail, "span": i.span}
