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
    """确定性硬校验：power / item / knowledge / presence（P2#12 接通）。

    历史 bug（修复于 P2#12）：import 名错（validate_item_conservation 不存在）+
    extract_claims_rule 缺 chapter/conn 参 + Issue.severity 域与 findings 不匹配，
    三个 validator 在管线里从未跑过。
    numeric validator 故意不接：数值 claims 噪声最大，原型期先观察。
    """
    if world is None:
        return []
    issues: list = []
    claims_by_id: dict = {}
    try:
        from ..validators import (
            extract_claims_rule, refine_knowledge_claims,
            validate_event_visibility, validate_item_inventory,
            validate_knowledge_edges, validate_power_monotonicity,
        )

        draft_text = ctx.workspace.get("draft_text", "")
        raw_claims = extract_claims_rule(draft_text, ctx.target_chapter, ctx.conn)
        # KNOWLEDGE：正则抽取的自由文本主语/信息词归一到账本（失败丢弃防误报）；
        # POWER：rank 词前文窗口绑定主语（无主语的 power claim 等于白跑）
        knowledge = refine_knowledge_claims(raw_claims, ctx.conn)
        power = _bind_power_subjects(
            [c for c in raw_claims if getattr(c.ctype, "value", c.ctype) == "power_level"],
            draft_text, ctx.conn)
        others = [c for c in raw_claims
                  if getattr(c.ctype, "value", c.ctype) not in ("power_level", "knowledge")]
        claims = knowledge + power + others
        claims_by_id = {c.claim_id: c for c in claims}

        for validate in (validate_power_monotonicity, validate_item_inventory,
                         validate_knowledge_edges, validate_event_visibility):
            try:
                issues.extend(validate(claims, world, ctx.conn))
            except Exception:
                continue   # 单个 validator 故障不拖累其余
    except ImportError:
        return []  # validators 包未找到时降级

    return [_issue_to_finding(i, claims_by_id) for i in issues]


def _bind_power_subjects(power_claims: list, draft_text: str, conn) -> list:
    """POWER claim 主语绑定：rank 词前 30 字窗口内最后出现的已知角色名。

    绑定不上的丢弃——没有主语的境界词可能在说任何人（招式名/他人/回忆），
    送 validator 只会误报。
    """
    if not power_claims or conn is None:
        return []
    try:
        names = {r["canonical_name"]: r["id"] for r in conn.execute(
            "SELECT id, canonical_name FROM entities WHERE entity_type='character'"
        ).fetchall()}
        for r in conn.execute(
                "SELECT a.alias, a.entity_id FROM entity_aliases a"
                " JOIN entities e ON e.id=a.entity_id"
                " WHERE e.entity_type='character'").fetchall():
            names.setdefault(r["alias"], r["entity_id"])
    except Exception:
        return []
    if not names:
        return []
    out = []
    for c in power_claims:
        off = c.span_offset or 0
        window = draft_text[max(0, off - 30):off]
        found = [(window.rfind(n), eid) for n, eid in names.items() if n in window]
        if not found:
            continue
        eid = max(found)[1]   # 离 rank 词最近的角色
        out.append(c.model_copy(update={"subject_entity": eid}))
    return out


# validator Issue.severity({critical,major,minor,info}) → finding severity({block,warn})
_SEVERITY_TO_FINDING = {"critical": "block"}


def _issue_to_finding(issue, claims_by_id: dict) -> dict:
    """validators.Issue → P1#8 finding。evidence 回查 claim.span（锚点补丁的锚点）。"""
    claim = claims_by_id.get(issue.claim_id)
    return {
        "severity": _SEVERITY_TO_FINDING.get(issue.severity, "warn"),
        "category": issue.code,
        "evidence": (claim.span if claim else "")[:300],
        "issue": issue.message[:500],
        "fix": (issue.suggested_fix or "")[:200],
        "repair_scope": "local",
        "source": "validator",
    }


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
{"category":"2.3-能力波动","severity":"warn|block","issue":"问题描述",
 "evidence":"逐字引用草稿原文片段","fix":"一句话修改建议","repair_scope":"local|structural"}
repair_scope 判定：OOC（人物根本性走形）/主线偏离/时间线结构性矛盾/视角混乱 → structural；
措辞、局部逻辑、称谓、数值等点状问题 → local。
severity 规则：仅当问题**明确违反上文给出的设定**（禁忌/境界/知情/数值）时用 block，其余用 warn。
evidence 必须逐字摘自草稿原文；没有原文证据的问题不要输出。
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
            from ..craft.findings import normalize_findings
            return normalize_findings(json.loads(text), draft_text, "llm_soft")
    except Exception:
        pass
    return []
