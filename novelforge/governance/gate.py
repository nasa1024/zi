"""apply_gate_routes: side-effect executor for Gate routing decisions (§16.7 / R13.1).

Route.decide() remains a pure function; all side-effects land here.
policy_mode / actor come from ctx (runtime config), not from the candidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..ids import new_id
from ..world.projection import ProjectionError
from ..db.write import with_retry
from .commit import commit_canon, _insert_promotion_log


class Route(Enum):
    COMMIT = "commit"
    REVIEW = "review"
    HOLD = "hold"
    REJECT = "reject"


@dataclass
class GateOutcome:
    committed: list = field(default_factory=list)  # [(candidate_id, fact_id)]
    queued: list = field(default_factory=list)      # [candidate_id]
    held: list = field(default_factory=list)        # [candidate_id]
    rejected: list = field(default_factory=list)    # [candidate_id]


def apply_gate_routes(ctx, gate, chapter_meta: dict) -> GateOutcome:
    """对 decide_batch 产出的每个 (candidate, route) 执行副作用。
    批量天然可续跑：已 promoted/rejected 的候选不再是 'proposed'，重跑自动跳过（幂等）。"""
    pm, actor = ctx.policy_mode, ctx.actor
    committed, queued, held, rejected = [], [], [], []

    for cand, route in gate.routes:
        if cand.status != "proposed":  # 续跑幂等：已处理过的跳过
            continue

        if route is Route.COMMIT:
            try:
                fact_id = with_retry(
                    lambda c=cand: commit_canon(c, ctx.conn, policy_mode=pm, actor=actor)
                )
                committed.append((cand.candidate_id, fact_id))
            except ProjectionError as e:
                # 投影前置不满足 → 转人审，绝不静默吞（评审 E5）
                _enqueue_review(ctx, cand, reason=f"projection_failed: {e}", pm=pm, actor=actor)
                queued.append(cand.candidate_id)

        elif route is Route.REVIEW:
            _enqueue_review(ctx, cand, reason="policy_review", pm=pm, actor=actor)
            queued.append(cand.candidate_id)

        elif route is Route.HOLD:
            with ctx.conn:
                _insert_promotion_log(
                    ctx.conn,
                    candidate_id=cand.candidate_id,
                    fact_id=None,
                    entity_id=cand.entity_id,
                    decision="hold_staging",
                    policy_mode=pm,
                    risk_tier=cand.risk_tier,
                    reason="hold_staging",
                    actor=actor,
                    chapter=cand.source_chapter,
                )
            held.append(cand.candidate_id)  # 候选停留 'proposed'

        elif route is Route.REJECT:
            with ctx.conn:
                ctx.conn.execute(
                    "UPDATE fact_candidates SET status='rejected', decided_at=datetime('now')"
                    " WHERE candidate_id=?",
                    (cand.candidate_id,),
                )
                _insert_promotion_log(
                    ctx.conn,
                    candidate_id=cand.candidate_id,
                    fact_id=None,
                    entity_id=cand.entity_id,
                    decision="reject",
                    policy_mode=pm,
                    risk_tier=cand.risk_tier,
                    reason="reject",
                    actor=actor,
                    chapter=cand.source_chapter,
                )
            rejected.append(cand.candidate_id)

    return GateOutcome(committed=committed, queued=queued, held=held, rejected=rejected)


def _enqueue_review(ctx, cand, *, reason: str, pm: str, actor: str) -> None:
    with ctx.conn:
        ctx.conn.execute(
            "UPDATE fact_candidates SET status='pending_review' WHERE candidate_id=?",
            (cand.candidate_id,),
        )
        ctx.conn.execute(
            "INSERT INTO review_queue(id, candidate_id, priority, risk_tier, reason, status)"
            " VALUES(?,?,?,?,?, 'pending')",
            (new_id("rq"), cand.candidate_id, _priority(cand), cand.risk_tier, reason),
        )
        _insert_promotion_log(
            ctx.conn,
            candidate_id=cand.candidate_id,
            fact_id=None,
            entity_id=cand.entity_id,
            decision="enqueue_review",
            policy_mode=pm,
            risk_tier=cand.risk_tier,
            reason=reason,
            actor=actor,
            chapter=cand.source_chapter,
        )


def _priority(cand) -> int:
    return {"high": 10, "medium": 50, "low": 100}.get(cand.risk_tier, 100)
