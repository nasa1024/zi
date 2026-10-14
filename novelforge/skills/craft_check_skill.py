"""CraftCheckSkill：网文工艺层检验（§05.8）。

8 项校验（部分确定性，部分 LLM 辅助）：
  1. value_shift       有无价值轴变化            block
  2. hook              章末有无钩子               block
  3. pacing            节奏是否符合规划           warn
  4. dialogue          对话区分度                 warn
  5. payoff            爽点兑现校验（防假爽点）   block
  6. flat_character    纸片人检测                 warn (LLM)
  7. beat_contract     beats 契约对齐             block
  8. hook_repeat       连续两章同型章尾钩子       warn (P1#7)

与 ContinuityCheckSkill 并行运行（Orchestrator 负责调度）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)
from ..control_plane.llm.tiers import ModelTier
from ..craft.payoff_binding import check_payoff_binding
from ..craft.language_quality import check_english_quality
from ..craft.voice import check_voice_distinctness


_CONTRACT = SkillContract(
    name="craft_check",
    version="1.0",
    trigger=SkillTrigger.CRAFT_CHECK,
    model_tier=ModelTier.MID,
    inputs=IOSpec(
        ["draft_text", "beats", "proposals", "pacing_state", "writing_profile"],
        "草稿+beats+提案+节拍状态+可选写作配置",
    ),
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
    fix: str = ""
    repair_scope: str = "local"
    category: str = ""
    source: str = "craft"


class CraftCheckSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        draft_text: str = ctx.workspace.get("draft_text", "")
        beats: list[dict] = ctx.workspace.get("beats", [])
        proposals: list[dict] = ctx.workspace.get("proposals", [])
        entities: list[dict] = (ctx.workspace.get("recall_pack") and
                                 ctx.workspace["recall_pack"].entities) or []
        writing_profile = _writing_profile(ctx)
        content_language = str(
            writing_profile.get("content_language") or writing_profile.get("language") or ""
        )
        english = content_language.lower().replace("_", "-").startswith("en")
        quality_rules = _mapping(writing_profile.get("quality_rules"))
        prompt_rules = _mapping(writing_profile.get("prompt_rules"))
        profile_requires_hook = _requires_hook(prompt_rules, quality_rules)

        issues: list[CraftIssue] = []

        # ① value_shift：至少一个 beat 有非空 value_axis
        issues += _check_value_shift(beats)

        # ② hook：末尾有 hook beat
        if not english or profile_requires_hook:
            issues += _check_hook(beats)

        # ③ pacing：与 pacing_state 比较
        pacing_state = ctx.workspace.get("pacing_state")
        if not english:
            issues += _check_pacing(beats, pacing_state)

        # ④ dialogue：对话区分度
        for vi in check_voice_distinctness(
            draft_text, entities, language=content_language if english else None,
        ):
            issues.append(CraftIssue(
                check="dialogue", severity=vi.severity, detail=vi.detail, span=vi.span,
            ))

        # ⑤ payoff：爽点兑现强制
        if not english or bool(quality_rules.get("requires_payoff_event")):
            for pi in check_payoff_binding(beats, proposals, ctx.target_chapter, ctx.conn):
                issues.append(CraftIssue(check="payoff", severity=pi.severity, detail=pi.detail))

        # ⑥ flat_character（LLM soft check）
        issues += _check_flat_character_llm(
            draft_text, ctx, language=content_language if english else None,
        )

        # ⑦ beat_contract：beats 条数与 beat_type 合规
        issues += _check_beat_contract(beats)

        # ⑧ hook_repeat：连续两章同型章尾钩子（P1#7，确定性零成本）
        if not english or profile_requires_hook:
            issues += _check_hook_repeat(ctx.conn, ctx.target_chapter)

        # ⑨ 英文 profile 确定性质量：每条 finding 都携带原文 evidence。
        if english:
            issues.extend(_english_findings_as_issues(
                check_english_quality(draft_text, writing_profile)
            ))

        # ⑩ v22 特性扩展检查（承诺账本 / 天降外挂 / 极性序列 / 爽点反应），
        # 见 craft/extensions.py：各自隔离、只读、默认 warn。
        from ..craft.extensions import run_craft_checks
        for raw in run_craft_checks(ctx):
            issues.append(CraftIssue(
                check=raw["check"], severity=raw["severity"], detail=raw["detail"],
                span=raw["span"], fix=raw["fix"], repair_scope=raw["repair_scope"],
                category=raw["category"],
            ))

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
    from ..planning.contract_fields import BEAT_TYPES
    valid_types = set(BEAT_TYPES)
    for i, b in enumerate(beats):
        bt = b.get("beat_type")
        if bt not in valid_types:
            issues.append(CraftIssue(check="beat_contract", severity="block",
                                      detail=f"beats[{i}].beat_type={bt!r} 不在合法集合 {valid_types}"))
    return issues


def _check_hook_repeat(conn, chapter: int) -> list[CraftIssue]:
    """P1#7：连续两章同型章尾钩子（oh-story 硬规则的 warn 化）。

    本章与上一章 hook_type 均为有效枚举（非 NULL/other）且相同 → warn。
    规划数据不全时静默跳过，不误报。
    """
    if conn is None or chapter < 2:
        return []
    try:
        rows = conn.execute(
            "SELECT chapter, hook_type FROM chapter_cards"
            " WHERE chapter IN (?, ?)", (chapter - 1, chapter),
        ).fetchall()
    except Exception:
        return []   # 老库无该列 → 跳过
    by_ch = {r["chapter"]: r["hook_type"] for r in rows}
    cur, prev = by_ch.get(chapter), by_ch.get(chapter - 1)
    if not cur or not prev or "other" in (cur, prev) or cur != prev:
        return []
    from ..craft.hooks import hook_label
    return [CraftIssue(
        check="hook_repeat", severity="warn",
        detail=f"连续两章使用同型章尾钩子（{hook_label(cur)}式，第{chapter-1}→{chapter}章），"
               f"建议本章换型防读者疲劳",
    )]


# ── LLM 软检查 ────────────────────────────────────────────────────────────────

_FLAT_SYSTEM = """\
你是网文工艺审稿员。检查草稿中是否存在"纸片人"问题：
角色行为只为推动情节，缺乏自主动机或真实情感反应。
输出 JSON 数组，每条：{"category":"flat_character","severity":"warn","issue":"问题描述",
"evidence":"逐字引用草稿原文片段","fix":"一句话修改建议"}
evidence 必须逐字摘自草稿原文，没有证据的问题不要输出。若无问题输出 []。只输出 JSON 数组。"""

_FLAT_SYSTEM_EN = """\
You are a fiction craft editor. Check whether characters act only as plot devices,
without autonomous motivation or a believable emotional response.
Return a JSON array. Each item must be:
{"category":"flat_character","severity":"warn","issue":"specific issue",
 "evidence":"verbatim excerpt from the draft","fix":"one-sentence revision advice"}
Evidence must be an exact excerpt. Return [] when there is no supported issue.
Return JSON only."""


def _check_flat_character_llm(
    draft_text: str,
    ctx: SkillContext,
    *,
    language: str | None = None,
) -> list[CraftIssue]:
    if not draft_text or len(draft_text) < 200:
        return []
    try:
        from ..control_plane.llm.provider import CacheHint, Message
        model_id = ctx.llm.model_for(ModelTier.MID)
        caps = ctx.llm._provider.capabilities(model_id)
        max_out = min(caps.max_tokens_out, 1536)  # 思考型模型推理计入预算，512 会截断 JSON
        # M1-⑥：跨章共享稳定前缀（前缀缓存命中）
        stable = ctx.workspace.get("stable_context", "")
        prefix = f"{stable}\n\n" if stable else ""
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=prefix + draft_text[:2000])],
            system=(_FLAT_SYSTEM_EN if str(language or "").lower().startswith("en")
                    else _FLAT_SYSTEM),
            max_tokens=max_out,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
        )
        raw = resp.text.strip()
        if raw.startswith("["):
            # P1#8：证据强制 + 宽容解析（无 evidence 的 finding 整条丢弃）
            from ..craft.findings import normalize_findings
            parsed = normalize_findings(json.loads(raw), draft_text, "craft_llm")
            return [
                CraftIssue(check="flat_character", severity=p["severity"],
                           detail=p["issue"], span=p["evidence"], fix=p["fix"],
                           repair_scope=p["repair_scope"])
                for p in parsed
            ]
    except Exception:
        pass
    return []


def _issue_dict(i: CraftIssue) -> dict:
    return {"check": i.check, "severity": i.severity, "detail": i.detail, "span": i.span,
            # P1#8 findings 字段（旧字段保留，只增不删）
            "category": i.category or f"craft.{i.check}",
            "issue": i.detail, "evidence": i.span,
            "fix": i.fix, "repair_scope": i.repair_scope, "source": i.source}


def _writing_profile(ctx: SkillContext) -> Mapping[str, Any]:
    value = ctx.workspace.get("writing_profile") or ctx.extra.get("writing_profile")
    if isinstance(value, Mapping):
        return value
    if ctx.conn is None:
        return {}
    from ..writing_profiles import get_project_writing_profile
    return get_project_writing_profile(ctx.conn, chapter=ctx.target_chapter)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _requires_hook(
    prompt_rules: Mapping[str, Any],
    quality_rules: Mapping[str, Any],
) -> bool:
    if quality_rules.get("requires_forward_hook") or quality_rules.get("requires_hook"):
        return True
    ending = str(prompt_rules.get("chapter_end") or quality_rules.get("chapter_end") or "").lower()
    return any(term in ending for term in ("hook", "forward", "cliffhanger"))


def _english_findings_as_issues(findings: list[dict]) -> list[CraftIssue]:
    return [
        CraftIssue(
            check=str(finding.get("check") or finding.get("category") or "english_quality"),
            severity="block" if finding.get("severity") == "block" else "warn",
            detail=str(finding.get("issue") or finding.get("detail") or ""),
            span=str(finding.get("evidence") or finding.get("span") or ""),
            fix=str(finding.get("fix") or ""),
            repair_scope=str(finding.get("repair_scope") or "local"),
            category=str(finding.get("category") or finding.get("check") or "english_quality"),
            source=str(finding.get("source") or "english_deterministic"),
        )
        for finding in findings
    ]
