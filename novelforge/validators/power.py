"""§04.3.1 境界单调性/越级检测 — validate_power_monotonicity.

Rules per §04 + §10 R12 (column names are authoritative from §02 schema):
1. Monotonicity: power must be non-decreasing unless change_type is a legal drop.
   Legal drop change_types: injury_drop, seal, self_cripple
2. Skip detection: adjacent rank_order gap > max_jump_threshold*100 without aid → POWER_LEVEL_SKIP
3. Claim consistency: claim rank must be >= latest legal historical rank → POWER_REGRESSION

Key design: rank_order comparison is PER SYSTEM (system_name), not global.
character_power_log columns: entity_id, system_name, rank_id, rank_order, change_chapter, change_type
power_ranks columns: id, system_name, rank_name, rank_order

The Claim payload for power_level:
  {rank_label: str, direction: "up"/"down"/"state", reason_tag?: str}
  rank_label is a rank_name like "金丹·初期"

To resolve rank_label to rank_order:
  Look up power_ranks WHERE system_name=? AND rank_name=?
  If entity has no history for that system, fall back to any matching rank_name across systems.
"""
from __future__ import annotations
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import WorldState

from .types import Claim, ClaimType, Issue

LEGAL_DOWN_TYPES = {"injury_drop", "seal", "self_cripple"}
MAX_JUMP_THRESHOLD = 1  # max big-boundary jumps per step (hundreds digit)


def validate_power_monotonicity(
    claims: list, world: "WorldState", conn: sqlite3.Connection
) -> list[Issue]:
    """Check power level monotonicity and skip per §04.3.1."""
    issues = []
    rank_map = world.rank_order_map  # {(system_name, rank_name): rank_order}

    for c in [c for c in claims if c.ctype == ClaimType.POWER_LEVEL]:
        ent = c.subject_entity
        rank_label = c.payload.get("rank_label", "")
        if not rank_label:
            continue

        # Resolve rank_label to rank_order (try to match by rank_name across all systems)
        matches = [(sn, rn, ro) for (sn, rn), ro in rank_map.items() if rn == rank_label]
        if not matches:
            issues.append(Issue(
                code="POWER_UNKNOWN_RANK", severity="major", kind="hard",
                claim_id=c.claim_id, chapter=c.chapter,
                message=f"未知境界标签「{rank_label}」，不在 power_ranks 枚举中",
                evidence_refs=[c.claim_id],
            ))
            continue

        cur_order = matches[0][2]  # Use first match's rank_order
        cur_system = matches[0][0]

        # Get entity's power history for this system
        history = [h for h in world.power_history(ent) if h["system_name"] == cur_system]
        if not history:
            continue  # First appearance, nothing to compare

        prev = history[-1]
        prev_order = prev["rank_order"]
        prev_chapter = prev["change_chapter"]

        # Check for illegal downward movement
        if cur_order < prev_order:
            reason_tag = c.payload.get("reason_tag", "")
            if reason_tag in LEGAL_DOWN_TYPES or set(c.exempt_tags) & {"intentional_power_drop"}:
                continue  # Legal drop, skip
            issues.append(Issue(
                code="POWER_REGRESSION", severity="critical", kind="hard",
                claim_id=c.claim_id, chapter=c.chapter,
                message=(f"{ent} 境界从 rank_order={prev_order}（第{prev_chapter}章）"
                         f"倒退至「{rank_label}」(rank_order={cur_order})，无合法跌境标注"),
                evidence_refs=[c.claim_id],
                suggested_fix="若为重伤跌境，请在草稿 payload 填 reason_tag=injury_drop",
            ))

        # Check for illegal skip (cur > prev by more than 1 big boundary)
        elif cur_order > prev_order:
            big_jump = (cur_order // 100) - (prev_order // 100)
            if big_jump > MAX_JUMP_THRESHOLD:
                # Check for breakthrough aid (gimmick or item)
                has_aid = _has_breakthrough_aid(conn, ent, c.chapter)
                if not has_aid and "intentional_leap" not in c.exempt_tags:
                    issues.append(Issue(
                        code="POWER_LEVEL_SKIP", severity="major", kind="hard",
                        claim_id=c.claim_id, chapter=c.chapter,
                        message=(f"{ent} 从 rank_order={prev_order} 直跳「{rank_label}」"
                                 f"(rank_order={cur_order})，跨越{big_jump}个大境界，"
                                 f"且无丹药/外挂/顿悟佐证"),
                        evidence_refs=[c.claim_id],
                        suggested_fix="补突破辅助（gimmick_usage_log 或 item_log 消耗记录）",
                    ))

    return issues


def _has_breakthrough_aid(conn: sqlite3.Connection, entity_id: str, chapter: int) -> bool:
    """Check if entity has gimmick usage or item consumption in this chapter that could explain breakthrough."""
    # Check gimmick usage at chapter
    row = conn.execute(
        "SELECT 1 FROM gimmick_usage_log WHERE user_entity_id=? AND use_chapter=? LIMIT 1",
        (entity_id, chapter),
    ).fetchone()
    if row:
        return True
    # Check item consumption (consume/destroy) at chapter
    row = conn.execute(
        "SELECT 1 FROM item_log WHERE (from_owner_id=? OR to_owner_id=?) "
        "AND change_chapter=? AND change_type IN ('consume','destroy') LIMIT 1",
        (entity_id, entity_id, chapter),
    ).fetchone()
    return row is not None
