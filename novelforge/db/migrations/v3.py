"""Migration v2 → v3: Add source_fact_id to *_log tables (§10 R12/A4)."""
from __future__ import annotations
import sqlite3
from . import register


@register("3")
def migrate_v3(conn: sqlite3.Connection) -> None:
    """Add source_fact_id + FK + index to 6 World State *_log tables."""
    # Use IF NOT EXISTS guard via column check for idempotency
    tables = {
        "knowledge_edges": "idx_know_srcfact",
        "item_log": "idx_itemlog_srcfact",
        "numeric_facts": "idx_numf_srcfact",
        "timeline_events": "idx_tl_srcfact",
        "gimmick_usage_log": "idx_gimuse_srcfact",
        "gimmick_rules": "idx_gimrule_srcfact",
    }
    for tbl, idx in tables.items():
        # Check if column already exists
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
        if "source_fact_id" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source_fact_id TEXT REFERENCES facts(id)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {tbl}(source_fact_id)")
