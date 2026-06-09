"""Migration v4 → v5: volumes/branches + volume_no/branch_id/cold_start 列（§9.4）。"""
from __future__ import annotations
import sqlite3
from . import register


@register("5")
def migrate_v5(conn: sqlite3.Connection) -> None:
    """Add volumes/branches tables + new columns for multi-volume and cold-start."""

    # volumes 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS volumes (
            id              TEXT PRIMARY KEY,
            volume_no       INTEGER NOT NULL,
            title           TEXT NOT NULL,
            synopsis        TEXT,
            start_chapter   INTEGER,
            end_chapter     INTEGER,
            status          TEXT NOT NULL DEFAULT 'writing'
                                CHECK(status IN ('writing','completed','archived')),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(volume_no)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_volumes_no ON volumes(volume_no)")

    # branches 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id              TEXT PRIMARY KEY,
            branch_name     TEXT NOT NULL UNIQUE,
            fork_chapter    INTEGER NOT NULL,
            base_branch_id  TEXT,
            description     TEXT,
            status          TEXT NOT NULL DEFAULT 'active'
                                CHECK(status IN ('active','merged','abandoned')),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(base_branch_id) REFERENCES branches(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_fork ON branches(fork_chapter)")

    # facts: volume_no, branch_id
    facts_cols = [r[1] for r in conn.execute("PRAGMA table_info(facts)")]
    if "volume_no" not in facts_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN volume_no INTEGER")
    if "branch_id" not in facts_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN branch_id TEXT")

    # draft_index: volume_no
    di_cols = [r[1] for r in conn.execute("PRAGMA table_info(draft_index)")]
    if "volume_no" not in di_cols:
        conn.execute("ALTER TABLE draft_index ADD COLUMN volume_no INTEGER")

    # l1_atoms: cold_start
    la_cols = [r[1] for r in conn.execute("PRAGMA table_info(l1_atoms)")]
    if "cold_start" not in la_cols:
        conn.execute("ALTER TABLE l1_atoms ADD COLUMN cold_start INTEGER NOT NULL DEFAULT 0")

    # 索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_volume ON facts(volume_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_branch ON facts(branch_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_volume ON draft_index(volume_no)")
