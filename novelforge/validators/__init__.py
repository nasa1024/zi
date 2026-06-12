"""NovelForge MVP0 确定性 validators.

所有 validator 签名: (claims, world, conn) -> list[Issue]
无 LLM、无网络、无随机，输入固定→输出固定，可单测。
"""
from .types import Claim, ClaimType, Issue, WorldState, HARD_CLAIM_TYPES
from .power import validate_power_monotonicity
from .knowledge import validate_knowledge_edges
from .numeric import validate_numeric_conservation
from .items import validate_item_inventory
from .presence import refine_knowledge_claims, validate_event_visibility
from .claims import extract_claims_rule

ALL_VALIDATORS = [
    validate_power_monotonicity,
    validate_knowledge_edges,
    validate_numeric_conservation,
    validate_item_inventory,
    validate_event_visibility,
]

def run_all_validators(claims, world, conn):
    out = []
    for v in ALL_VALIDATORS:
        out.extend(v(claims, world, conn))
    return out

__all__ = [
    "Claim", "ClaimType", "Issue", "WorldState", "HARD_CLAIM_TYPES",
    "validate_power_monotonicity", "validate_knowledge_edges",
    "validate_numeric_conservation", "validate_item_inventory",
    "validate_event_visibility", "refine_knowledge_claims",
    "extract_claims_rule", "ALL_VALIDATORS", "run_all_validators",
]
