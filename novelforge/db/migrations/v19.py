"""Migration v18 -> v19: trace fact candidates to their source L0 draft.

v18 databases may have been produced by the earlier conditional memory
bootstrap.  Re-running the idempotent v18 invariant first repairs those
databases before adding the candidate provenance column.
"""
from __future__ import annotations

import sqlite3

from . import column_exists, register, table_exists
from .v18 import migrate_v18


_FACT_CANDIDATES_V19 = """
CREATE TABLE IF NOT EXISTS fact_candidates (
    candidate_id TEXT PRIMARY KEY,
    proposal_json TEXT NOT NULL,
    op TEXT NOT NULL CHECK(op IN ('add','update','deprecate','retcon')),
    target_fact_id TEXT,
    fact_type TEXT NOT NULL,
    entity_id TEXT,
    proposed_object TEXT,
    risk_tier TEXT NOT NULL DEFAULT 'low'
        CHECK(risk_tier IN ('low','medium','high')),
    confidence REAL,
    evidence_strength REAL,
    evidence_refs TEXT,
    conflict_flags TEXT,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','pending_review','promoted','rejected','superseded')),
    source_chapter INTEGER,
    source_draft_id TEXT,
    source_skill TEXT,
    decided_by TEXT,
    decided_at TEXT,
    committed_revision_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(target_fact_id) REFERENCES facts(id),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    FOREIGN KEY(source_draft_id) REFERENCES draft_index(id),
    CHECK(proposal_json IS NULL OR json_valid(proposal_json)),
    CHECK(evidence_refs IS NULL OR json_valid(evidence_refs)),
    CHECK(conflict_flags IS NULL OR json_valid(conflict_flags))
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON fact_candidates(status);
CREATE INDEX IF NOT EXISTS idx_cand_risk ON fact_candidates(risk_tier);
CREATE INDEX IF NOT EXISTS idx_cand_chapter ON fact_candidates(source_chapter);
CREATE INDEX IF NOT EXISTS idx_cand_rank
    ON fact_candidates(status, evidence_strength DESC);
"""


@register("19")
def migrate_v19(conn: sqlite3.Connection) -> None:
    # 修复已经被旧版代码标记为 v18，但仍缺少部分 memory baseline
    # 或将旧 L1 误标 active 的数据库。v18 迁移本身完全幂等。
    migrate_v18(conn)

    if not table_exists(conn, "fact_candidates"):
        conn.executescript(_FACT_CANDIDATES_V19)
    elif not column_exists(conn, "fact_candidates", "source_draft_id"):
        conn.execute(
            "ALTER TABLE fact_candidates ADD COLUMN source_draft_id TEXT"
            " REFERENCES draft_index(id)"
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cand_source_draft"
        " ON fact_candidates(source_draft_id)"
    )
