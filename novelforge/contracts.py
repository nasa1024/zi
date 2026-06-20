"""Core shared contracts for the NovelForge pipeline.

Models shared across world/, governance/, validators/:
  StateTransition — deterministic state change from Draft layer
  BibleChangeProposal — LLM-produced fact diff proposal
  FactCandidate — fact_candidates row as an object
  RunContext — runtime config/ctx passed into gate routing
  OptimisticLockError — versioned-write conflict (retried by with_retry)
  HARD_FACET — fact_type → facet name mapping (shared constant)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OptimisticLockError(Exception):
    """facts.version 版本冲突（并发写保护）。由 with_retry 重试。"""


# fact_type → hard-state facet. gimmick splits further by new['facet'] key.
HARD_FACET: dict[str, str] = {
    "power_system": "power",
    "knowledge": "knowledge",
    "item": "item",
    "numeric": "numeric",
    "event": "timeline",
}


FACT_TYPE_ALIASES: dict[str, str] = {
    # Older prompts/tests used these names, but facts.fact_type has a strict
    # CHECK whitelist. Normalize at the boundary and preserve the original
    # semantic class in new.canon_kind.
    "power_rank": "power_system",
    "timeline": "event",
    "appearance": "misc",
    "entity": "misc",
    "foreshadow": "misc",
    "foreshadowing": "misc",
}


def canonical_fact_type(fact_type: str | None) -> str:
    ft = (fact_type or "misc").strip() or "misc"
    return FACT_TYPE_ALIASES.get(ft, ft)


def normalize_proposal_fact_type(proposal: dict) -> dict:
    """Return a copy with proposal['fact_type'] normalized to schema-legal names.

    This is deliberately tiny and deterministic: it does not validate the
    proposal, it only maps historical/LLM-facing aliases onto the §10/DDL
    vocabulary so staging rows cannot later fail only at commit time.
    """
    p = dict(proposal or {})
    original = (p.get("fact_type") or "misc").strip() or "misc"
    canonical = canonical_fact_type(original)
    p["fact_type"] = canonical
    if canonical != original:
        new = dict(p.get("new") or {})
        new.setdefault("canon_kind", original)
        if original == "power_rank":
            new.setdefault("facet", "power")
        elif original == "timeline":
            new.setdefault("facet", "timeline")
        p["new"] = new
    return p


def normalize_bible_change_proposal(prop: "BibleChangeProposal") -> "BibleChangeProposal":
    return BibleChangeProposal.model_validate(normalize_proposal_fact_type(prop.model_dump()))


class StateTransition(BaseModel):
    """Deterministic state change emitted by Draft/Skill layers."""

    model_config = ConfigDict(populate_by_name=True)

    entity_id: str
    facet: str  # power / knowledge / item / numeric / timeline / gimmick_rule / gimmick_use
    from_value: Optional[str] = Field(default=None, alias="from")
    to_value: str = Field(alias="to")
    at_chapter: int
    kind: Optional[str] = None
    evidence_span: Optional[str] = None
    payload: dict = Field(default_factory=dict)


class BibleChangeProposal(BaseModel):
    """LLM-produced diff proposal for a single canon fact change."""

    op: str  # add / update / deprecate / retcon
    fact_type: str
    entity: Optional[str] = None
    new: Optional[dict] = None
    valid_from_chapter: int = 0
    target_fact_id: Optional[str] = None  # required for update / deprecate / retcon


@dataclass
class FactCandidate:
    """fact_candidates DB 行包装为属性访问对象。"""

    candidate_id: str
    entity_id: Optional[str]
    fact_type: str
    proposal_json: str
    status: str
    risk_tier: str
    source_chapter: int
    target_fact_id: Optional[str] = None
    evidence_refs: Optional[str] = None


@dataclass
class RunContext:
    """运行期上下文，由 PipelineManager 构造后传入 apply_gate_routes。"""

    conn: sqlite3.Connection
    policy_mode: str  # human_gate / auto_promote
    actor: str        # 'human:<name>' | 'system:pipeline' | 'system:auto'
