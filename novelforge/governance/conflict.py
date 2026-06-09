"""冲突检测 + 证据打分 + 风险分级（§11.1-11.3）。

detect_conflict(cand, conn) -> ConflictSet
score_evidence(cand)        -> float  [0,1]
classify_risk(cand, config) -> "low" | "medium" | "high"
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..contracts import FactCandidate


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class ConflictItem:
    kind: str          # same_predicate_diff_value | interval_overlap | validator
    fact_id: str       # 与哪条 canon fact 冲突
    detail: str
    severity: str      # "block" | "warn"


@dataclass
class ConflictSet:
    items: list[ConflictItem] = field(default_factory=list)

    @property
    def has_block(self) -> bool:
        return any(c.severity == "block" for c in self.items)

    def to_json(self) -> str:
        return json.dumps([vars(c) for c in self.items], ensure_ascii=False)


# ── 冲突检测 ──────────────────────────────────────────────────────────────────

def detect_conflict(cand: FactCandidate, conn) -> ConflictSet:
    """三类冲突检测（§11.1）。"""
    cs = ConflictSet()
    try:
        prop = json.loads(cand.proposal_json)
    except Exception:
        return cs

    n = prop.get("new") or {}
    predicate = n.get("predicate") or prop.get("fact_type", "")
    proposed_object = n.get("object") or n.get("rank_name") or n.get("value") or ""
    entity_id = cand.entity_id
    fact_type = cand.fact_type
    cand_from = prop.get("valid_from_chapter", 0)
    cand_to = prop.get("valid_to_chapter")

    # (a) 同实体同谓词不同值
    if entity_id and predicate and proposed_object:
        rows = conn.execute(
            "SELECT id, object, valid_from_chapter, valid_to_chapter"
            " FROM facts"
            " WHERE status='canon' AND entity_id=? AND fact_type=? AND predicate=?"
            "   AND object <> ?",
            (entity_id, fact_type, predicate, proposed_object),
        ).fetchall()
        for r in rows:
            cs.items.append(ConflictItem(
                kind="same_predicate_diff_value",
                fact_id=r["id"],
                detail=f"existing={r['object']!r} vs proposed={proposed_object!r}",
                severity="block",
            ))

    # (b) valid 区间重叠冲突
    if entity_id and fact_type and cand_from is not None:
        rows = conn.execute(
            "SELECT id, valid_from_chapter, valid_to_chapter FROM facts"
            " WHERE status='canon' AND entity_id=? AND fact_type=? AND predicate=?"
            "   AND ? < COALESCE(valid_to_chapter, 2147483647)"
            "   AND valid_from_chapter < COALESCE(?, 2147483647)",
            (entity_id, fact_type, predicate, cand_from, cand_to),
        ).fetchall()
        for r in rows:
            cs.items.append(ConflictItem(
                kind="interval_overlap",
                fact_id=r["id"],
                detail=(
                    f"existing=[{r['valid_from_chapter']},{r['valid_to_chapter']}]"
                    f" overlaps proposed=[{cand_from},{cand_to}]"
                ),
                severity="warn",
            ))

    # (c) 复用确定性 validators
    cs.items.extend(_run_validators(cand, prop, conn))

    return cs


def _run_validators(cand: FactCandidate, prop: dict, conn) -> list[ConflictItem]:
    items: list[ConflictItem] = []
    try:
        from ..validators.power import validate_power_monotonicity
        from ..validators.claims import extract_claims_rule
        from ..validators.types import WorldState

        draft_text = (prop.get("new") or {}).get("object", "")
        if draft_text:
            claims = extract_claims_rule(draft_text)
            world = WorldState(as_of=prop.get("valid_from_chapter", 0), conn=conn)
            for issue in validate_power_monotonicity(claims, world):
                items.append(ConflictItem(
                    kind="validator",
                    fact_id=cand.target_fact_id or "",
                    detail=str(issue),
                    severity="block",
                ))
    except Exception:
        pass
    return items


# ── 证据打分 ──────────────────────────────────────────────────────────────────

def score_evidence(
    cand: FactCandidate,
    conflict_set: Optional["ConflictSet"] = None,
) -> float:
    """§11.2 证据强度分 [0,1]。

    0.6 * src_verifiable      # 出处可验
    0.2 * recurrence_norm     # 跨章复现（≥3 次 = 满分）
    0.2 * cross_consistency   # 与 canon 无冲突
    LLM confidence 不参与本分数（硬原则8）。
    """
    # src_verifiable：evidence_refs 非空视为可验证
    src = 1.0 if cand.evidence_refs else 0.0

    # recurrence_norm：evidence_refs 中引用数量估算（逗号分隔条数，上限3）
    refs = cand.evidence_refs or ""
    ref_count = len([r for r in refs.split(",") if r.strip()])
    recurrence = min(ref_count / 3.0, 1.0)

    # cross_consistency：有 block 级冲突则 0，否则 1
    has_conflict = bool(conflict_set and conflict_set.has_block)
    consistency = 0.0 if has_conflict else 1.0

    return round(0.6 * src + 0.2 * recurrence + 0.2 * consistency, 4)


# ── 风险分级 ──────────────────────────────────────────────────────────────────

_HIGH_TYPES = frozenset({"world_rule", "power_system", "power_rank", "numeric", "item"})
_MED_TYPES  = frozenset({"relationship", "knowledge"})
_LOW_TYPES  = frozenset({"style", "misc", "appearance"})


def classify_risk(cand: FactCandidate, config=None) -> str:
    """§11.3 风险分级规则表（自上而下首个命中）。"""
    ft = cand.fact_type or ""

    # Rule 1: 高风险 fact_type
    if ft in _HIGH_TYPES:
        return "high"

    # Rule 2: 命中 require_human_for 配置
    if config is not None:
        rh = getattr(getattr(config, "governance", None), "require_human_for", [])
        try:
            prop = json.loads(cand.proposal_json)
        except Exception:
            prop = {}
        if prop.get("op") in rh or ft in rh:
            return "high"

    # Rule 3: 关系或 op=retcon
    try:
        op = json.loads(cand.proposal_json).get("op", "add")
    except Exception:
        op = "add"
    if ft in _MED_TYPES or op == "retcon":
        return "medium"

    # Rule 4: 低风险类型
    if ft in _LOW_TYPES:
        return "low"

    # Rule 5: 其余 medium
    return "medium"
