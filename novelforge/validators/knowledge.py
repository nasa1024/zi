"""§04.3.2 知情者越权检测 — validate_knowledge_edges.

Rules per §04 + §10 R11 (knowledge_edges has public_from_chapter, secrecy_level):
- For each knowledge claim with act in {reveal, reference, act_on}:
  - If entity does not have the info_key in its knowledge set → KNOWLEDGE_LEAK (critical)
  - Unless: info is already public (public_from_chapter IS NOT NULL AND <= chapter)
  - Unless: exempt_tags contains planted_misdirection or unreliable_narrator

Claim payload: {info_key: str, act: "reveal"/"reference"/"act_on"}
knowledge_edges columns: knower_entity_id, secret_key, knowledge_state, learned_chapter, public_from_chapter
"""
from __future__ import annotations
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import WorldState

from .types import Claim, ClaimType, Issue


EXEMPT_TAGS_KNOWLEDGE = {"planted_misdirection", "unreliable_narrator"}


def validate_knowledge_edges(
    claims: list, world: "WorldState", conn: sqlite3.Connection
) -> list[Issue]:
    """Check knowledge edge violations per §04.3.2."""
    issues = []

    for c in [c for c in claims if c.ctype == ClaimType.KNOWLEDGE]:
        ent = c.subject_entity
        info_key = c.payload.get("info_key", "")
        act = c.payload.get("act", "reference")
        if not info_key:
            continue

        # Check exempt tags first
        if set(c.exempt_tags) & EXEMPT_TAGS_KNOWLEDGE:
            continue

        # Get knowledge set for entity at this chapter
        known = world.knowledge_set(ent, c.chapter)
        if info_key in known:
            continue

        # Check if the info is public at this chapter
        if world.is_public(info_key, c.chapter):
            continue

        # Not known and not public → KNOWLEDGE_LEAK
        known_preview = sorted(known)[:6]
        issues.append(Issue(
            code="KNOWLEDGE_LEAK", severity="critical", kind="hard",
            claim_id=c.claim_id, chapter=c.chapter,
            message=(f"{ent} 在第{c.chapter}章{act}了信息「{info_key}」，"
                     f"但其知情集中缺失该边（从未习得）。"
                     f"已知: {known_preview}…"),
            evidence_refs=[c.claim_id],
            suggested_fix=f"若合理，请补一条 knowledge_edges({ent}, {info_key}, ≤{c.chapter})",
        ))

    return issues
