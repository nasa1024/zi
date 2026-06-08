"""§04.3.6 道具库存 — validate_item_inventory.

item_ownership columns: item_entity_id, owner_entity_id, quantity, since_chapter
item_log columns: item_entity_id, from_owner_id, to_owner_id, quantity_delta, change_chapter, change_type
Claim payload: {item: str, op: "gain"/"lose"/"consume"/"transfer", qty: int, counterparty?: str}
"""
from __future__ import annotations
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import WorldState

from .types import Claim, ClaimType, Issue


def validate_item_inventory(
    claims: list, world: "WorldState", conn: sqlite3.Connection
) -> list[Issue]:
    """Check item ownership consistency per §04.3.6."""
    issues = []

    for c in [c for c in claims if c.ctype == ClaimType.ITEM_OWNERSHIP]:
        owner = c.subject_entity
        item = c.payload.get("item", "")
        op = c.payload.get("op", "gain")
        qty = c.payload.get("qty", 1)
        if not item:
            continue

        # Resolve item_entity_id: try entities by canonical_name
        item_row = conn.execute(
            "SELECT id FROM entities WHERE canonical_name=? AND entity_type='item'",
            (item,),
        ).fetchone()
        item_entity_id = item_row[0] if item_row else item

        # Resolve owner_entity_id: try entities by canonical_name (any entity_type)
        owner_row = conn.execute(
            "SELECT id FROM entities WHERE canonical_name=?",
            (owner,),
        ).fetchone()
        owner_entity_id = owner_row[0] if owner_row else owner

        if op in ("lose", "consume", "transfer"):
            held = world.item_qty(owner_entity_id, item_entity_id)
            if held < qty:
                ever = world.ever_owned(owner_entity_id, item_entity_id)
                code = "ITEM_DOUBLE_SPEND" if ever and held == 0 else "ITEM_NOT_OWNED"
                issues.append(Issue(
                    code=code, severity="major", kind="hard",
                    claim_id=c.claim_id, chapter=c.chapter,
                    message=(f"{owner} 试图 {op} {qty}×「{item}」，"
                             f"但当前持有仅 {held}"),
                    evidence_refs=[c.claim_id],
                    suggested_fix="补 item_log 获得记录，或修正情节",
                ))

    return issues
