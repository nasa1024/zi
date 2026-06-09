"""PromotionPolicy：纯函数晋升决策（§03.5 / §07.6）。

decide(cand, world, config) -> Route
decide_batch(candidates, world, config) -> GateDecision

所有副作用（DB 写入）在 gate.apply_gate_routes 执行；本模块无 IO。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .gate import Route
from ..contracts import FactCandidate


@dataclass
class GateDecision:
    routes: list[tuple[FactCandidate, Route]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for _, r in self.routes:
            c[r.value] = c.get(r.value, 0) + 1
        return c


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def _parse_proposal(cand: FactCandidate) -> dict:
    try:
        return json.loads(cand.proposal_json)
    except Exception:
        return {}


def _requires_human(prop: dict, cand: FactCandidate, config) -> bool:
    """§03.5 Step-1：require_human_for 规则列表逐项检查。"""
    rh: list[str] = getattr(config.governance, "require_human_for", [])
    if not rh:
        return False
    for rule in rh:
        if rule == "retcon" and prop.get("op") == "retcon":
            return True
        if rule == "power_system" and prop.get("fact_type") in ("power_system", "power_rank"):
            return True
        if rule == "knowledge_edge_change":
            facet = (prop.get("new") or {}).get("facet") or ""
            if prop.get("fact_type") == "knowledge" or facet == "knowledge":
                return True
        if rule == "high" and cand.risk_tier == "high":
            return True
        # 允许直接用 fact_type 名
        if rule == prop.get("fact_type"):
            return True
    return False


def _has_conflicts(
    cand: FactCandidate,
    world: Any,
    conflict_map: Optional[dict] = None,
) -> bool:
    """§03.5 Step-2：未解决冲突检查。

    优先用 conflict_map（由 Orchestrator 的 detect_conflict 填充）；
    fallback 到 world.conflict_map（MVP1 兼容）。
    """
    if conflict_map and cand.candidate_id in conflict_map:
        cset = conflict_map[cand.candidate_id]
        return bool(getattr(cset, "has_block", False))
    if world is None:
        return False
    legacy: dict = getattr(world, "conflict_map", {}) or {}
    return bool(legacy.get(cand.candidate_id))


def _evidence_strong(
    cand: FactCandidate,
    threshold: float = 0.7,
    conflict_set=None,
) -> bool:
    """MVP2：用 score_evidence 评分 >= threshold 且 risk_tier≠high。"""
    if cand.risk_tier == "high":
        return False
    try:
        from .conflict import score_evidence
        return score_evidence(cand, conflict_set=conflict_set) >= threshold
    except Exception:
        return bool(cand.evidence_refs)


def _is_low_risk_soft(cand: FactCandidate, prop: dict, config) -> bool:
    """hybrid 模式：low/medium risk + 非敏感 fact_type → 自动 COMMIT。"""
    max_risk = getattr(config.governance, "auto_promote_max_risk", "low")
    tier_order = {"low": 0, "medium": 1, "high": 2}
    if tier_order.get(cand.risk_tier, 99) > tier_order.get(max_risk, 0):
        return False
    sensitive = {"knowledge", "power_system", "power_rank"}
    if prop.get("fact_type") in sensitive:
        return False
    return True


# ── 公开入口 ──────────────────────────────────────────────────────────────────

class PromotionPolicy:
    @staticmethod
    def decide(
        cand: FactCandidate,
        world: Any,
        config,
        conflict_map: Optional[dict] = None,
    ) -> Route:
        """§03.5 三步纯函数决策。"""
        prop = _parse_proposal(cand)

        # Step 1: require_human_for 强制人审
        if _requires_human(prop, cand, config):
            return Route.REVIEW

        # Step 2: 未解决冲突 → HOLD 等冲突裁决
        if _has_conflicts(cand, world, conflict_map):
            return Route.HOLD

        # Step 3: 模式分叉
        mode = getattr(config.governance, "mode", "human_gate")
        if mode == "human_gate":
            return Route.REVIEW
        elif mode == "auto_promote":
            threshold = getattr(config.governance, "evidence_threshold", 0.7)
            if _evidence_strong(cand, threshold):
                return Route.COMMIT
            return Route.REVIEW
        else:  # hybrid
            if _is_low_risk_soft(cand, prop, config):
                return Route.COMMIT
            return Route.REVIEW

    @staticmethod
    def decide_batch(
        candidates: list[FactCandidate],
        world: Any,
        config,
        conflict_map: Optional[dict] = None,
    ) -> GateDecision:
        routes = [
            (c, PromotionPolicy.decide(c, world, config, conflict_map)) for c in candidates
        ]
        return GateDecision(routes=routes)
