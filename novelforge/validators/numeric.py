"""§04.3.5 数值守恒 — validate_numeric_conservation.

numeric_facts columns: entity_id, metric_key, value, unit, as_of_chapter, monotonic
Claim payload: {key: str, value: float, unit: str, op: "set"/"add"/"sub"}
"""
from __future__ import annotations
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import WorldState

from .types import Claim, ClaimType, Issue

EPS = 1e-9
# Simple unit conversion table (factor to normalize to a base unit)
# extend as needed; if units not found, return None (skip conversion)
UNIT_FACTORS: dict[tuple, float] = {
    ("里", "公里"): 0.5,
    ("公里", "里"): 2.0,
    ("km", "公里"): 1.0,
    ("公里", "km"): 1.0,
}


def units_convertible(u1: str, u2: str) -> bool:
    """True if we can convert between u1 and u2 (same unit or known pair)."""
    return u1 == u2 or (u1, u2) in UNIT_FACTORS or (u2, u1) in UNIT_FACTORS


def to_unit(value: float, from_unit: str, to_unit: str) -> float:
    """Convert value from from_unit to to_unit. Returns value unchanged if same unit."""
    if from_unit == to_unit:
        return value
    factor = UNIT_FACTORS.get((from_unit, to_unit))
    if factor is not None:
        return value * factor
    factor = UNIT_FACTORS.get((to_unit, from_unit))
    if factor is not None:
        return value / factor
    return value  # fallback: no conversion


def validate_numeric_conservation(
    claims: list, world: "WorldState", conn: sqlite3.Connection
) -> list[Issue]:
    """Check numeric conservation per §04.3.5."""
    issues = []

    for c in [c for c in claims if c.ctype == ClaimType.NUMERIC]:
        ent = c.subject_entity
        key = c.payload.get("key", "")
        unit = c.payload.get("unit", "")
        raw_val = c.payload.get("value")
        op = c.payload.get("op", "set")
        if not key or raw_val is None:
            continue

        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            continue

        base = world.numeric_state(ent, key)

        if base:
            if not units_convertible(base["unit"], unit):
                issues.append(Issue(
                    code="NUMERIC_UNIT_MISMATCH", severity="minor", kind="hard",
                    claim_id=c.claim_id, chapter=c.chapter,
                    message=f"{ent}.{key} 单位「{unit}」与既有「{base['unit']}」无法换算",
                    evidence_refs=[c.claim_id],
                ))
                continue
            v = to_unit(val, unit, base["unit"])
        else:
            v = val

        if op == "set" and base and abs(v - base["value"]) > EPS:
            issues.append(Issue(
                code="NUMERIC_CONTRADICTION", severity="major", kind="hard",
                claim_id=c.claim_id, chapter=c.chapter,
                message=(f"{ent}.{key} 声称={v}{unit}，"
                         f"与既有值 {base['value']}{base['unit']} 冲突"),
                evidence_refs=[c.claim_id],
            ))

        elif op in ("add", "sub"):
            cur = base["value"] if base else 0.0
            new_val = cur + v if op == "add" else cur - v
            if new_val < -EPS:
                issues.append(Issue(
                    code="NUMERIC_NEGATIVE_BALANCE", severity="major", kind="hard",
                    claim_id=c.claim_id, chapter=c.chapter,
                    message=(f"{ent}.{key} 余额将为负 "
                             f"({cur}{base['unit'] if base else unit} {op} {v} = {new_val:.2f})"),
                    evidence_refs=[c.claim_id],
                ))

    return issues
