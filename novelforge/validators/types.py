"""Shared types for NovelForge MVP0 validators."""
from __future__ import annotations
import sqlite3
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    POWER_LEVEL    = "power_level"
    KNOWLEDGE      = "knowledge"
    TIMELINE       = "timeline"
    LOCATION_MOVE  = "location_move"
    NUMERIC        = "numeric"
    ITEM_OWNERSHIP = "item_ownership"
    GIMMICK        = "gimmick"
    FORESHADOW     = "foreshadow"
    TONE           = "tone"
    MOTIVATION     = "motivation"
    SETTING_FUZZY  = "setting_fuzzy"


HARD_CLAIM_TYPES = {
    ClaimType.POWER_LEVEL, ClaimType.KNOWLEDGE, ClaimType.TIMELINE,
    ClaimType.LOCATION_MOVE, ClaimType.NUMERIC, ClaimType.ITEM_OWNERSHIP,
    ClaimType.GIMMICK, ClaimType.FORESHADOW,
}


class Claim(BaseModel):
    claim_id: str
    chapter: int
    ctype: ClaimType
    subject_entity: Optional[str] = None
    object_entity: Optional[str] = None
    span: str = ""
    span_offset: int = 0
    payload: dict = Field(default_factory=dict)
    exempt_tags: list = Field(default_factory=list)


class Issue(BaseModel):
    code: str
    severity: Literal["critical", "major", "minor", "info"]
    kind: Literal["hard", "soft"]
    claim_id: str
    chapter: int
    message: str
    evidence_refs: list = Field(default_factory=list)
    suggested_fix: Optional[str] = None


class PowerEntry(BaseModel):
    """A row from character_power_log."""
    entity_id: str
    system_name: str
    rank_id: str
    rank_order: int
    change_chapter: int
    change_type: str  # breakthrough|injury_drop|seal|unseal|init|self_cripple
    source_fact_id: Optional[str] = None


class KnowledgeEntry(BaseModel):
    """A row from knowledge_edges."""
    knower_entity_id: str
    secret_key: str
    knowledge_state: str
    learned_chapter: int
    public_from_chapter: Optional[int] = None
    secrecy_level: Optional[str] = None


class NumericEntry(BaseModel):
    """A row from numeric_facts."""
    entity_id: Optional[str]
    metric_key: str
    value: float
    unit: str
    as_of_chapter: int
    monotonic: str = "none"


class ItemOwnershipEntry(BaseModel):
    """A row from item_ownership."""
    item_entity_id: str
    owner_entity_id: Optional[str]
    quantity: int
    since_chapter: int


class WorldState:
    """Lightweight world state snapshot. Populated by replay_* queries from *_log tables."""

    def __init__(self, as_of: int, conn: sqlite3.Connection):
        self.as_of = as_of
        self._conn = conn
        self._power_cache: dict | None = None
        self._knowledge_cache: dict | None = None
        self._numeric_cache: dict | None = None
        self._item_cache: dict | None = None
        self._rank_order_cache: dict | None = None

    @property
    def rank_order_map(self) -> dict:
        """Returns {(system_name, rank_name): rank_order}."""
        if self._rank_order_cache is None:
            rows = self._conn.execute(
                "SELECT system_name, rank_name, rank_order FROM power_ranks"
            ).fetchall()
            self._rank_order_cache = {(r[0], r[1]): r[2] for r in rows}
        return self._rank_order_cache

    def power_history(self, entity_id: str) -> list:
        """Full character_power_log for entity, ordered by change_chapter."""
        rows = self._conn.execute(
            "SELECT entity_id, system_name, rank_id, rank_order, change_chapter, change_type "
            "FROM character_power_log "
            "WHERE entity_id=? AND change_chapter<=? ORDER BY change_chapter",
            (entity_id, self.as_of),
        ).fetchall()
        return [dict(r) for r in rows]

    def knowledge_set(self, entity_id: str, chapter: int) -> set:
        """Set of secret_keys known by entity up to chapter."""
        rows = self._conn.execute(
            "SELECT secret_key FROM knowledge_edges "
            "WHERE knower_entity_id=? AND learned_chapter<=?",
            (entity_id, chapter),
        ).fetchall()
        return {r[0] for r in rows}

    def is_public(self, secret_key: str, chapter: int) -> bool:
        """True if secret_key has public_from_chapter <= chapter (visible to all)."""
        row = self._conn.execute(
            "SELECT MIN(public_from_chapter) AS pf FROM knowledge_edges "
            "WHERE secret_key=? AND public_from_chapter IS NOT NULL",
            (secret_key,),
        ).fetchone()
        return row is not None and row[0] is not None and row[0] <= chapter

    def numeric_state(self, entity_id: Optional[str], metric_key: str) -> Optional[dict]:
        """Latest numeric fact for (entity_id, metric_key) as of self.as_of."""
        row = self._conn.execute(
            "SELECT value, unit FROM numeric_facts "
            "WHERE (entity_id IS ? OR entity_id=?) AND metric_key=? AND as_of_chapter<=? "
            "ORDER BY as_of_chapter DESC LIMIT 1",
            (entity_id, entity_id, metric_key, self.as_of),
        ).fetchone()
        return {"value": row[0], "unit": row[1]} if row else None

    def item_qty(self, owner_entity_id: str, item_entity_id: str) -> int:
        """Quantity of item held by owner, from item_ownership snapshot."""
        row = self._conn.execute(
            "SELECT quantity FROM item_ownership WHERE item_entity_id=? AND owner_entity_id=?",
            (item_entity_id, owner_entity_id),
        ).fetchone()
        return row[0] if row else 0

    def ever_owned(self, owner_entity_id: str, item_entity_id: str) -> bool:
        """True if owner ever held item (per item_log history)."""
        row = self._conn.execute(
            "SELECT 1 FROM item_log WHERE item_entity_id=? AND to_owner_id=? LIMIT 1",
            (item_entity_id, owner_entity_id),
        ).fetchone()
        return row is not None
