"""Migration v16 -> v17: durable Agent replay metadata.

LangGraph can replay a node after a process failure.  Domain writes therefore
need stable source keys so replay can reuse existing decisions/plans instead of
silently duplicating them.
"""
from __future__ import annotations

import sqlite3

from . import column_exists, register


@register("17")
def migrate_v17(conn: sqlite3.Connection) -> None:
    if not column_exists(conn, "volumes", "source_decision_id"):
        conn.execute("ALTER TABLE volumes ADD COLUMN source_decision_id TEXT")

    # Non-unique indexes remain safe for already-used local v16 databases that
    # may contain replay duplicates. Application-level guards perform the
    # idempotent lookup before every write.
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_volumes_source_decision
            ON volumes(source_decision_id);
        CREATE INDEX IF NOT EXISTS idx_story_plans_source_decision
            ON story_plans(source_decision_id);
        CREATE INDEX IF NOT EXISTS idx_decision_sets_pending_scope
            ON decision_sets(session_id, stage, scope_type, scope_id, status);
    """)
