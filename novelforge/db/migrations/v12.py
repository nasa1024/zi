"""Migration v11 → v12: foreshadow 结算列（mention/advance 二分）+ foreshadow_log 审计表（P1#6）。

注意：历史上没有任何迁移创建过 foreshadow 表（它只在 schema.sql 基线里），
从 v4 一路迁上来的老库可能整表缺失——缺表时按 v12 形态整表创建，存在则补列。
"""
from __future__ import annotations
import sqlite3
from . import register, column_exists, table_exists

_FORESHADOW_V12 = """
CREATE TABLE IF NOT EXISTS foreshadow (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    description     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'planted'
                        CHECK(state IN ('planted','reinforced','misled','paid_off','overdue')),
    planted_chapter INTEGER NOT NULL,
    due_chapter     INTEGER,
    paid_off_chapter INTEGER,
    related_entity_id TEXT,
    importance      INTEGER NOT NULL DEFAULT 3,
    fact_id         TEXT,
    last_mentioned_chapter INTEGER,
    advance_count   INTEGER NOT NULL DEFAULT 0,
    last_advanced_chapter INTEGER,
    origin          TEXT NOT NULL DEFAULT 'manual',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fs_state ON foreshadow(state);
CREATE INDEX IF NOT EXISTS idx_fs_due   ON foreshadow(due_chapter);
"""

_FORESHADOW_LOG = """
CREATE TABLE IF NOT EXISTS foreshadow_log (
    id            TEXT PRIMARY KEY,
    foreshadow_id TEXT NOT NULL REFERENCES foreshadow(id),
    chapter       INTEGER NOT NULL,
    action        TEXT NOT NULL CHECK(action IN ('plant','mention','advance','payoff')),
    evidence      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fslog_fs ON foreshadow_log(foreshadow_id, chapter);
"""


@register("12")
def migrate_v12(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "foreshadow"):
        conn.executescript(_FORESHADOW_V12)
    else:
        if not column_exists(conn, "foreshadow", "last_mentioned_chapter"):
            conn.execute("ALTER TABLE foreshadow ADD COLUMN last_mentioned_chapter INTEGER")
        if not column_exists(conn, "foreshadow", "advance_count"):
            conn.execute("ALTER TABLE foreshadow ADD COLUMN advance_count INTEGER NOT NULL DEFAULT 0")
        if not column_exists(conn, "foreshadow", "last_advanced_chapter"):
            conn.execute("ALTER TABLE foreshadow ADD COLUMN last_advanced_chapter INTEGER")
        if not column_exists(conn, "foreshadow", "origin"):
            conn.execute("ALTER TABLE foreshadow ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
    conn.executescript(_FORESHADOW_LOG)
